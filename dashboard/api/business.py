"""Local, single-worker business runs. Only allowlisted scripted cases execute."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from threading import Lock
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from agentcheck_biz.cases import load_suite
from agentcheck_biz.checks import load_json
from agentcheck_biz.reports import save_json
from agentcheck_biz.comparison import compare_runs
from examples.ticket_agent.langgraph_agent import SUPPORTED_SCENARIOS


ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = {"case.json", "run.json", "initial.json", "final.json", "checks.json", "events.jsonl", "report.md",
             "observation-initial.json", "observation-final.json"}


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    case_id: str
    app_version: Literal["fixed", "unsafe"] = "fixed"
    agent: Literal["scripted", "langgraph"] = "scripted"


class BusinessRuns:
    def __init__(self, output_root: Path, case_root: Path, timeout=45):
        self.root = output_root.resolve()
        self.catalog = {case["case_id"]: (path.resolve(), case) for path, case in load_suite(case_root)}
        self.timeout = timeout
        self.lock = Lock()
        self.busy = False
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="business-run")

    def start(self):
        self.root.mkdir(parents=True, exist_ok=True)
        for path in self.root.glob("job-*/job.json"):
            try:
                job = load_json(path)
                if job["status"] in {"queued", "running"}:
                    job.update(status="interrupted", business_status="INCONCLUSIVE",
                               error="服务重启，上一运行未完整结束")
                    save_json(path, job)
            except (OSError, ValueError, KeyError):
                continue

    def close(self):
        # Child processes have a hard deadline; shutdown waits for evidence to settle.
        self.pool.shutdown(wait=True)

    def job_path(self, run_id):
        if not re.fullmatch(r"job-[a-f0-9]{32}", run_id):
            raise HTTPException(404, "运行不存在")
        path = self.root / run_id / "job.json"
        if not path.is_file():
            raise HTTPException(404, "运行不存在")
        return path

    def submit(self, request: RunRequest):
        if request.case_id not in self.catalog:
            raise HTTPException(404, "案例不存在")
        if request.agent == "langgraph" and self.catalog[request.case_id][1].get("scenario", "retry") not in SUPPORTED_SCENARIOS:
            raise HTTPException(422, "该案例是多上下文或并发契约，请使用 scripted")
        with self.lock:
            if self.busy:
                raise HTTPException(429, "已有业务运行正在执行，请完成后重试")
            self.busy = True
        try:
            run_id = "job-" + uuid4().hex
            directory = self.root / run_id
            directory.mkdir(parents=True, exist_ok=False)
            job = {"run_id": run_id, "case_id": request.case_id, "app_version": request.app_version,
                   "status": "queued", "business_status": None, "agent": request.agent, "model_called": False,
                   "created_at": datetime.now(timezone.utc).isoformat(), "timeout_seconds": self.timeout}
            save_json(directory / "job.json", job)
            self.pool.submit(self._execute, directory, dict(job))
            return job
        except Exception:
            with self.lock:
                self.busy = False
            raise

    def _execute(self, directory, job):
        try:
            job["status"] = "running"
            save_json(directory / "job.json", job)
            source, _ = self.catalog[job["case_id"]]
            command = [sys.executable, "-X", "utf8", "-m", "agentcheck_biz.cli", "run", "--case", str(source),
                       "--agent", job.get("agent", "scripted"), "--app-version", job["app_version"], "--output", str(directory)]
            options = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
            completed = subprocess.run(command, cwd=ROOT, capture_output=True, timeout=self.timeout, **options)
            # Verdicts FAIL/INCONCLUSIVE intentionally have non-zero CLI exit codes.
            payload = json.loads(completed.stdout.decode("utf-8"))
            if "run_dir" not in payload or completed.returncode not in (0, 1, 2, 3):
                raise ValueError("Runner did not produce a complete run")
            run_dir = Path(payload["run_dir"]).resolve()
            if run_dir.parent != directory.resolve():
                raise ValueError("Runner output escaped job directory")
            job.update(status="finished", business_status=payload["status"], artifact_id=run_dir.name)
        except subprocess.TimeoutExpired:
            job.update(status="timed_out", business_status="ERROR", error="运行超过硬超时，子进程已终止")
        except Exception as error:
            job.update(status="error", business_status="ERROR", error=f"运行器错误：{type(error).__name__}")
        finally:
            job["finished_at"] = datetime.now(timezone.utc).isoformat()
            try:
                save_json(directory / "job.json", job)
            finally:
                with self.lock:
                    self.busy = False

    def list_runs(self):
        jobs = []
        for path in self.root.glob("job-*/job.json"):
            try:
                jobs.append(load_json(path))
            except (OSError, ValueError):
                continue
        return sorted(jobs, key=lambda item: item["created_at"], reverse=True)[:100]

    def artifact_directory(self, job):
        parent = self.job_path(job["run_id"]).parent.resolve()
        paths = list(parent.glob("biz-*/run.json"))
        if len(paths) != 1:
            return None
        directory = paths[0].parent.resolve()
        if directory.parent != parent:
            raise HTTPException(404, "证据目录无效")
        return directory

    def detail(self, run_id):
        job = load_json(self.job_path(run_id))
        directory = self.artifact_directory(job)
        detail = {"job": job, "run": None, "case": self.catalog.get(job["case_id"], (None, None))[1],
                  "checks": None, "initial": None, "final": None, "events": []}
        if directory:
            for key, name in (("run", "run.json"), ("case", "case.json"), ("checks", "checks.json"),
                              ("initial", "initial.json"), ("final", "final.json")):
                if (directory / name).exists():
                    detail[key] = load_json(directory / name)
            path = directory / "events.jsonl"
            if path.exists():
                lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
                detail["events"] = [json.loads(line) for line in lines if line.endswith("\n")]
        return detail


def create_business_router(output_root=ROOT / "artifacts" / "business-api", case_root=ROOT / "cases" / "tickets" / "full", timeout=45):
    store = BusinessRuns(output_root, case_root, timeout)
    router = APIRouter(prefix="/api/business", tags=["business"])
    router.add_event_handler("startup", store.start)
    router.add_event_handler("shutdown", store.close)

    @router.get("/cases")
    def cases():
        return [{**case, "execution_mode": "scripted", "model_called": False,
                 "supported_agents": ["scripted", "langgraph"] if case.get("scenario", "retry") in SUPPORTED_SCENARIOS else ["scripted"]}
                for _, case in store.catalog.values()]

    @router.get("/compare")
    def compare(left: str, right: str):
        directories = []
        for run_id in (left, right):
            job = load_json(store.job_path(run_id))
            if job["status"] in {"queued", "running"}:
                raise HTTPException(409, "请等待运行完成后再对比")
            directory = store.artifact_directory(job)
            if directory is None:
                raise HTTPException(409, "运行尚无可对比的完整证据")
            directories.append(directory)
        try:
            result = compare_runs(*directories)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise HTTPException(422, str(error)) from error
        # Parent timeout/interruption overrides a child's apparently complete artifacts.
        for side, run_id in (("left", left), ("right", right)):
            job = load_json(store.job_path(run_id))
            result[side]["job_id"] = run_id
            if job["status"] != "finished":
                result["controlled"] = False
                result["blockers"].append(f"{side} 父任务状态为 {job['status']}，不具备完整执行证据")
        return result

    @router.post("/runs", status_code=202)
    def create_run(request: RunRequest):
        return store.submit(request)

    @router.get("/runs")
    def runs():
        return store.list_runs()

    @router.get("/runs/{run_id}")
    def detail(run_id: str):
        return store.detail(run_id)

    @router.get("/runs/{run_id}/artifacts/{name}")
    def artifact(run_id: str, name: str):
        if name not in ARTIFACTS:
            raise HTTPException(404, "证据文件不存在")
        job = load_json(store.job_path(run_id))
        directory = store.artifact_directory(job)
        if directory is None or not (directory / name).is_file():
            raise HTTPException(404, "证据尚未生成")
        return FileResponse(directory / name, filename=name)

    return router
