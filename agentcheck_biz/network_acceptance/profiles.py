"""Frozen denominator: every supported core profile runs exactly five times."""

REPETITIONS = 5


def profiles():
    result = []
    for target in ("ticket", "gitea"):
        for scenario, business, evidence, coverage, count in (
            ("P00_transparent", "PASS", "PASS", "transparent", 1),
            ("P01_reject", "ERROR", "PASS", "covered", 0),
            ("P02_delay", "ERROR", "PASS", "covered", 1),
            ("P03_drop", "ERROR", "PASS", "covered", 1),
            ("P04_miss", "INCONCLUSIVE", "INCONCLUSIVE", "not_covered", 1),
        ):
            result.append(dict(id=f"{target}_{scenario}", kind="proxy", target=target,
                scenario=scenario, expected=dict(business_status=business, evidence_status=evidence,
                    coverage=coverage, resource_count=count, tool_calls=1)))
    for label, target, version, fault, business, count, calls in (
        ("A_unsafe_control", "ticket", "unsafe", False, "PASS", 1, 1),
        ("B_unsafe_loss_retry", "ticket", "unsafe", True, "FAIL", 2, 2),
        ("C_fixed_loss_retry", "ticket", "fixed", True, "PASS", 1, 2),
        ("Gitea_api_visible_loss", "gitea", None, True, "ERROR", 1, 1),
    ):
        result.append(dict(id=label, kind="proxy", target=target, scenario="commit_loss",
            version=version, fault=fault, expected=dict(business_status=business, evidence_status="PASS",
                coverage="covered" if fault else "transparent", resource_count=count, tool_calls=calls)))
    for label, maximum, business, calls in (("verify_first", 6, "PASS", 2), ("budget_exhausted", 1, "INCONCLUSIVE", 1)):
        result.append(dict(id="recovery_" + label, kind="recovery", target="ticket", maximum=maximum,
            expected=dict(business_status=business, evidence_status="PASS", coverage="covered", resource_count=1, tool_calls=calls)))
    for point, maximum in (("before_service", 6), ("before_commit", 6), ("after_commit", 6), ("after_commit", 1)):
        result.append(dict(id=f"crash_{point}_{maximum}", kind="crash", target="ticket", point=point, maximum=maximum,
            expected=dict(business_status="INCONCLUSIVE" if maximum == 1 else "PASS", evidence_status="PASS",
                coverage="covered", resource_count=1, tool_calls=1 if maximum == 1 else 3 if point == "before_commit" else 2)))
    return result


def support_matrix():
    return dict(schema_version=1, repetitions=REPETITIONS, profiles=profiles(),
        supported={
            "Ticket": ["before-forward rejection", "after-response delay/drop", "SQLite independently confirmed commit then disconnect",
                       "unsafe duplicate and server idempotency repair", "query-before-retry and bounded budget",
                       "owned service termination before entry / before commit / after commit, original database restart"],
            "Gitea": ["real HTTP proxy rejection/delay/drop", "independent authenticated GET visibility then disconnect",
                      "fresh official pinned Gitea process, data directory, owner and repository for every repetition"],
        }, unsupported=[
            "Gitea internal transaction commit/rollback barriers, service crash or idempotent retry guarantees",
            "packet-level TCP loss, DNS/TLS faults, kernel/network partitions, remote host clock synchronization",
            "machine power loss, filesystem durability/fsync claims, distributed production deployments",
            "persistent agent checkpoints/leases across agent restart (D26-D30)",
            "model/provider behavior and stochastic clients",
        ], historical_only=["Other D23 policy branches retain their original evidence and regression tests; not part of the five-run core"],
        correlation="run_id + operation_id + attempt_id + call_id/request_id + nonce/acknowledgement and per-process sequence; clocks are supplementary single-host checks",
        model_requests=0)


def plan():
    return [dict(slot=f"{p['id']}:{n}", profile=p["id"], repetition=n, kind=p["kind"],
                 target=p["target"], expected=p["expected"])
            for n in range(1, REPETITIONS + 1) for p in profiles()]


def aggregate(slots):
    """Never remove missing, unexpected, or not-covered slots from denominators."""
    distribution = {}
    for row in slots:
        group = distribution.setdefault(row["profile"], dict(planned=0, executed=0, exceptions=0, not_started=0,
            accepted=0, covered=0, transparent=0, not_covered=0, business_counts={}, signatures={}))
        group["planned"] += 1
        state = row.get("state", "not_started")
        group["executed"] += state != "not_started"
        group["exceptions"] += state == "exception"
        group["not_started"] += state == "not_started"
        observed = row.get("observed", {})
        accepted = state == "completed" and all(observed.get(k) == v for k, v in row["expected"].items())
        group["accepted"] += accepted
        coverage = observed.get("coverage", "not_covered")
        group[coverage if coverage in {"covered", "transparent"} else "not_covered"] += 1
        business = observed.get("business_status", "MISSING")
        group["business_counts"][business] = group["business_counts"].get(business, 0) + 1
        signature = "/".join(str(observed.get(k, "MISSING")) for k in
            ("business_status", "evidence_status", "coverage", "resource_count", "tool_calls"))
        group["signatures"][signature] = group["signatures"].get(signature, 0) + 1
    keys = ("planned", "executed", "exceptions", "not_started", "accepted", "covered", "transparent", "not_covered")
    totals = {k: sum(g[k] for g in distribution.values()) for k in keys}
    return dict(status="PASS" if totals["planned"] > 0 and totals["accepted"] == totals["planned"] else "FAIL",
                totals=totals, distributions=distribution)
