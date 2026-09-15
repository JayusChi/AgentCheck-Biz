"""Exercise HTTP -> real child runner -> saved report, plus failure boundaries."""

import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.api.business import BusinessRuns, create_business_router, ROOT
from agentcheck_biz.reports import save_json


class BusinessAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def client(self, **kwargs):
        app = FastAPI()
        app.include_router(create_business_router(self.root, **kwargs))
        return TestClient(app)

    def wait(self, client, run_id):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            response = client.get(f"/api/business/runs/{run_id}")
            self.assertEqual(response.status_code, 200, response.text)
            detail = response.json()
            if detail["job"]["status"] not in {"queued", "running"}:
                return detail
            time.sleep(.05)
        self.fail("API child did not settle")

    def test_real_pass_fail_detail_history_download_and_restart(self):
        with self.client() as client:
            cases = client.get("/api/business/cases").json()
            self.assertEqual(len(cases), 12)
            case_id = next(case["case_id"] for case in cases if case["case_id"].startswith("T03"))
            ids = []
            for version, expected in (("fixed", "PASS"), ("unsafe", "FAIL")):
                response = client.post("/api/business/runs", json={"case_id": case_id, "app_version": version})
                self.assertEqual(response.status_code, 202)
                run_id = response.json()["run_id"]
                ids.append(run_id)
                detail = self.wait(client, run_id)
                self.assertEqual(detail["job"]["business_status"], expected, detail)
                self.assertEqual(detail["checks"]["status"], expected)
                self.assertTrue(detail["events"])
                self.assertNotEqual(detail["initial"], detail["final"])
                report = client.get(f"/api/business/runs/{run_id}/artifacts/report.md")
                self.assertEqual(report.status_code, 200)
                self.assertIn(expected, report.text)
                observation = client.get(f"/api/business/runs/{run_id}/artifacts/observation-final.json")
                self.assertEqual(observation.status_code, 200)
                self.assertEqual(observation.json()["data"], detail["final"])
                self.assertEqual(observation.json()["run_id"], detail["run"]["run_id"])
                self.assertEqual(client.get(f"/api/business/runs/{run_id}/artifacts/.env").status_code, 404)
            self.assertEqual(len(client.get("/api/business/runs").json()), 2)
            self.assertNotEqual(ids[0], ids[1])
        with self.client() as client:
            self.assertEqual(len(client.get("/api/business/runs").json()), 2)
            self.assertEqual(client.get(f"/api/business/runs/{ids[0]}").json()["checks"]["status"], "PASS")

    def test_reject_unknown_case_paths_model_and_extra_parameters(self):
        with self.client() as client:
            for body, expected in (({"case_id": "../../.env"}, 404),
                                   ({"case_id": "T01_normal_create", "agent": "llm"}, 422),
                                   ({"case_id": "T01_normal_create", "app_version": "other"}, 422)):
                self.assertEqual(client.post("/api/business/runs", json=body).status_code, expected)
            self.assertEqual(client.get("/api/business/runs/not-a-run").status_code, 404)
            self.assertEqual(client.get("/api/business/runs").json(), [])

    def test_timeout_is_error_and_frees_worker(self):
        with self.client() as client, patch("dashboard.api.business.subprocess.run", side_effect=subprocess.TimeoutExpired("runner", 1)):
            response = client.post("/api/business/runs", json={"case_id": "T01_normal_create"})
            detail = self.wait(client, response.json()["run_id"])
            self.assertEqual(detail["job"]["status"], "timed_out")
            self.assertEqual(detail["job"]["business_status"], "ERROR")
            self.assertIsNone(detail["checks"])
            self.assertEqual(client.post("/api/business/runs", json={"case_id": "T01_normal_create"}).status_code, 202)

    def test_restart_marks_unfinished_job_inconclusive(self):
        run_id = "job-" + "a" * 32
        directory = self.root / run_id
        directory.mkdir()
        save_json(directory / "job.json", {"run_id": run_id, "case_id": "T01_normal_create", "status": "running", "created_at": "2026-09-08"})
        with self.client() as client:
            detail = client.get(f"/api/business/runs/{run_id}").json()
            self.assertEqual(detail["job"]["status"], "interrupted")
            self.assertEqual(detail["job"]["business_status"], "INCONCLUSIVE")

    def test_busy_worker_rejects_additional_job_without_allocation(self):
        from dashboard.api.business import RunRequest
        from fastapi import HTTPException
        store = BusinessRuns(self.root, ROOT / "cases/tickets/full")
        self.addCleanup(store.close)
        store.start()
        store.busy = True
        with self.assertRaises(HTTPException) as error:
            store.submit(RunRequest(case_id="T01_normal_create"))
        self.assertEqual(error.exception.status_code, 429)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_graph_execution_and_version_comparison_over_http(self):
        with self.client() as client:
            ids = []
            for version, agent in (("unsafe", "scripted"), ("fixed", "scripted"), ("fixed", "langgraph")):
                response = client.post("/api/business/runs", json={"case_id": "T03_response_loss_retry", "app_version": version, "agent": agent})
                self.assertEqual(response.status_code, 202, response.text)
                run_id = response.json()["run_id"]
                detail = self.wait(client, run_id)
                self.assertEqual(detail["job"]["agent"], agent)
                self.assertEqual(detail["checks"]["status"], "FAIL" if version == "unsafe" else "PASS")
                ids.append(run_id)
            result = client.get("/api/business/compare", params={"left": ids[0], "right": ids[1]}).json()
            self.assertTrue(result["controlled"], result)
            self.assertEqual(result["comparison_type"], "service_version")
            result = client.get("/api/business/compare", params={"left": ids[1], "right": ids[2]}).json()
            self.assertTrue(result["controlled"], result)
            self.assertEqual(result["comparison_type"], "adapter")
            self.assertEqual(client.get("/api/business/compare", params={"left": ids[0], "right": ids[0]}).status_code, 422)
            self.assertEqual(client.get("/api/business/compare", params={"left": "../outside", "right": ids[0]}).status_code, 404)
            self.assertEqual(client.post("/api/business/runs", json={"case_id": "T12_concurrent_same_operation", "agent": "langgraph"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
