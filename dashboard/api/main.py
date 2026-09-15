"""FastAPI application entry point for AgentCheck dashboard."""

from __future__ import annotations

import shutil
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from dashboard.api.business import create_business_router
from dashboard.api.recovery import create_recovery_router
from dashboard.api.capabilities import create_capabilities_router

from dashboard.api.demo_mcp import router as demo_mcp_router
from dashboard.api.workbench import router

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
STATIC_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"
SEED_DB = REPO_ROOT / "dashboard" / "seed" / "agentcheck.db"
TARGET_DB = REPO_ROOT / "agentcheck.db"
load_dotenv()


def _ensure_bundled_db() -> None:
    """Copy the seed DB into place when missing or empty (no comparisons)."""
    if not SEED_DB.exists():
        return
    if TARGET_DB.exists():
        try:
            import sqlite3

            with sqlite3.connect(TARGET_DB) as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='comparisons'"
                ).fetchone()
                if row and row[0]:
                    count = conn.execute("SELECT COUNT(*) FROM comparisons").fetchone()[0]
                    if count:
                        return
        except Exception:
            pass
    shutil.copy(SEED_DB, TARGET_DB)


_ensure_bundled_db()

def create_app(*, business_output=None, recovery_report=None, recovery_root=None, delivery_identity=None):
    """Build the same dashboard against explicitly selected local run evidence."""
    app = FastAPI(title="AgentCheck Dashboard")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(demo_mcp_router)
    app.include_router(router)
    app.include_router(create_business_router(**({"output_root": business_output} if business_output else {})))
    recovery_options = {}
    if recovery_report is not None:
        recovery_options['report_path'] = recovery_report
    if recovery_root is not None:
        recovery_options['allowed_root'] = recovery_root
    app.include_router(create_recovery_router(**recovery_options))
    app.include_router(create_capabilities_router())
    if delivery_identity is not None:
        @app.get('/api/delivery/health')
        def delivery_health():
            return delivery_identity | {'status': 'PASS'}

    if STATIC_DIR.exists():
        @app.get("/business", include_in_schema=False)
        def business_page():
            return FileResponse(STATIC_DIR / "index.html")

        app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
    return app


app = create_app()
