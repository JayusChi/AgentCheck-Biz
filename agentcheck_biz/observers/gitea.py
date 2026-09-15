"""Complete, scoped, all-state API observation; failures never become zero rows."""

from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import httpx

from agentcheck_biz.adapters.contracts import Observation


def normalize_repository(row):
    value = {key: row[key] for key in ("id", "full_name", "private", "description")}
    if (type(value["id"]) is not int or value["id"] <= 0 or type(value["private"]) is not bool
            or not isinstance(value["full_name"], str) or not isinstance(value["description"], str)):
        raise ValueError("Malformed Gitea repository observation")
    return value


def normalize_issue(row, repository):
    value = {key: row[key] for key in ("id", "number", "title", "body", "state")}
    if (any(type(value[key]) is not int or value[key] <= 0 for key in ("id", "number"))
            or any(not isinstance(value[key], str) for key in ("title", "body"))
            or value["state"] not in {"open", "closed"} or row.get("pull_request") is not None
            or row["repository"]["id"] != repository["id"]
            or row["repository"]["full_name"] != repository["full_name"]
            or urlsplit(row["url"]).path != f"/api/v1/repos/{repository['full_name']}/issues/{value['number']}"):
        raise ValueError("Malformed, pull-request or foreign-repository Issue")
    return {**value, "repository_id": repository["id"], "repository": repository["full_name"]}


class GiteaObserver:
    def __init__(self, environment, case):
        self.environment, self.case = environment, case

    def observe(self, context):
        source = "gitea-api-readonly"
        try:
            if self.environment.repository is None:
                raise ValueError("Gitea repository was not allocated")
            client = self.environment.observer_client
            repository_response = client.request("GET", self.environment.repo_path)
            repository = normalize_repository(repository_response["body"])
            if repository != self.environment.repository:
                raise ValueError("Observed repository identity differs from the isolated target")
            rows, pages, details, total = [], [], [], None
            page_size = self.case["limits"]["page_size"]
            path = self.environment.repo_path + "/issues"
            for page in range(1, self.case["limits"]["max_pages"] + 1):
                response = client.request("GET", path, params={"state": "all", "type": "issues", "limit": page_size, "page": page})
                items, headers = response["body"], response["headers"]
                if not isinstance(items, list) or len(items) > page_size:
                    raise ValueError("Malformed or oversized Issue page")
                declared = headers["x-total-count"]
                if not isinstance(declared, str) or not declared.isdecimal():
                    raise ValueError("Missing or malformed Issue total count")
                if total is None:
                    total = int(declared)
                if int(declared) != total:
                    raise ValueError("Issue collection changed during pagination")
                batch = [normalize_issue(item, repository) for item in items]
                rows.extend(batch)
                if (len({row["id"] for row in rows}) != len(rows)
                        or len({row["number"] for row in rows}) != len(rows) or len(rows) > total):
                    raise ValueError("Duplicate or inconsistent Issue pages")
                pages.append({"page": page, "evidence": response["evidence"], "ids": [row["id"] for row in batch]})
                link = headers.get("link") or ""
                links = httpx.Response(200, headers={"link": link}).links
                next_url = links.get("next", {}).get("url")
                if len(rows) == total:
                    if next_url:
                        raise ValueError("Unexpected next page after the declared total")
                    break
                if not batch or not next_url:
                    raise ValueError("Issue pagination ended before the declared total")
                parsed = urlsplit(next_url)
                query = parse_qs(parsed.query)
                if (f"{parsed.scheme}://{parsed.netloc}" != client.origin or parsed.path != path
                        or query.get("page") != [str(page + 1)] or query.get("limit") != [str(page_size)]
                        or query.get("state") != ["all"] or query.get("type") != ["issues"]):
                    raise ValueError("Foreign, looping or scope-changing Issue next-page link")
            else:
                raise ValueError("Issue pagination budget exhausted before completeness")
            # Explicit per-resource GETs provide independent create/close confirmation.
            for row in rows:
                detail = client.request("GET", path + f"/{row['number']}")
                if normalize_issue(detail["body"], repository) != row:
                    raise ValueError("Issue list and independent detail observation disagree")
                details.append(detail["evidence"])
            data = {"run_id": context.run_id, "repository": repository, "repository_evidence": repository_response["evidence"],
                    "issues": sorted(rows, key=lambda row: row["number"]), "detail_evidence": details,
                    "pagination": {"state": "all", "type": "issues", "page_size": page_size, "total_count": total,
                                   "pages": pages, "complete": True}}
            return Observation(source, datetime.now(timezone.utc).isoformat(), context.run_id,
                               context.operation_id, context.attempt_id, True, data)
        except Exception as error:
            return Observation.failure(context, source, error)
