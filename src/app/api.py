"""HTTP API + small web UI in front of the job queue.

    uv run bravebird-serve                       # http://127.0.0.1:8765
    uv run bravebird-serve --workers 3 --per-portal 2

Endpoints (JSON unless noted):
    GET  /                                  web UI (HTML)
    GET  /api/health
    GET  /api/inputs                        input files in inputs/ (for the UI)
    POST /api/jobs                          {"input_file": "github_v1.json"} or {"input": {...}}
                                            + optional "options", "label"
    GET  /api/jobs?limit=50
    GET  /api/jobs/{id}
    POST /api/jobs/{id}/cancel              queued jobs only
    GET  /api/jobs/{id}/manifest
    GET  /api/jobs/{id}/log                 text
    GET  /api/jobs/{id}/files/{name}        a captured file listed in the manifest
    GET  /api/jobs/{id}/evidence/screenshot.png

Security:
- Binds to 127.0.0.1 by default. Any other host requires BRAVEBIRD_API_TOKEN,
  sent as "Authorization: Bearer <token>" (the UI asks for it once). Without a
  token, requests must carry a loopback Host header (blocks DNS rebinding).
- Jobs from the API cannot choose state_dir/output_dir (no writing elsewhere on
  disk) or base_url (where credentials are typed), cannot run headed, and
  cannot carry an inline proxy password.
- File endpoints only serve names listed in that job's manifest, resolved
  inside that job's run directory.
- Everything returned passes through the redactor.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import ConfigError, Credentials, RunInput, load_input
from .jobs import JobQueue, JobStore
from .redact import redactor
from .runner import BREAKABLE, RunOptions, execute

log = logging.getLogger(__name__)

JOB_ID = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-[0-9a-f]{4}$")
INPUT_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}\.json$")


class JobOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")
    simulate_wrong_code: bool = False
    break_step: str | None = None


class JobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_file: str | None = Field(default=None, description="A file name from GET /api/inputs")
    input: dict[str, Any] | None = Field(default=None, description="Inline input JSON")
    options: JobOptions = JobOptions()
    label: str | None = Field(default=None, max_length=80)


class Settings(BaseModel):
    inputs_dir: Path = Path("inputs")
    state_dir: Path = Path("state")
    output_dir: Path = Path("runs")
    db_path: Path = Path("state/jobs.sqlite3")
    workers: int = 2
    per_portal: int = 2
    token: str | None = None
    # Host headers to accept. Set by bravebird-serve when there is no token, so
    # a web page can't reach the unauthenticated API through DNS rebinding.
    allowed_hosts: list[str] | None = None


def create_app(settings: Settings, creds: Credentials, executor=execute) -> FastAPI:
    # Defence in depth: whatever built the credentials, the API never echoes them.
    redactor.add(creds.username.get_secret_value(), creds.password.get_secret_value(),
                 creds.totp_secret.get_secret_value())
    if settings.token:
        redactor.add(settings.token)
    store = JobStore(settings.db_path)
    queue = JobQueue(store, creds, workers=settings.workers, per_portal=settings.per_portal,
                     executor=executor)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await queue.start()
        try:
            yield
        finally:
            await queue.stop()
            store.close()

    app = FastAPI(title="Bravebird runner", lifespan=lifespan, docs_url="/api/docs",
                  redoc_url=None, openapi_url="/api/openapi.json")
    app.state.queue = queue
    app.state.store = store
    if settings.allowed_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    def auth(authorization: Annotated[str | None, Header()] = None) -> None:
        if not settings.token:
            return
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not hmac.compare_digest(given.encode(), settings.token.encode()):
            raise HTTPException(401, "Missing or wrong bearer token")

    guard = [Depends(auth)]

    def job_or_404(job_id: str):
        if not JOB_ID.match(job_id):
            raise HTTPException(404, "No such job")
        job = store.get(job_id)
        if not job:
            raise HTTPException(404, "No such job")
        return job

    def run_dir(job) -> Path:
        return (settings.output_dir / job.id).resolve()

    # -- UI ----------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def ui() -> str:
        return resources.files("app").joinpath("ui/index.html").read_text(encoding="utf-8")

    # -- API ---------------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "workers": queue.workers, "per_portal": queue.per_portal,
                "auth": bool(settings.token), "counts": store.counts()}

    @app.get("/api/inputs", dependencies=guard)
    def inputs() -> list[dict]:
        out = []
        for path in sorted(settings.inputs_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            out.append({"name": path.name, "input": data})
        return json.loads(redactor.redact_json(out))

    @app.post("/api/jobs", status_code=201, dependencies=guard)
    def submit(req: JobRequest) -> dict:
        if bool(req.input_file) == bool(req.input):
            raise HTTPException(422, "Send exactly one of input_file or input")
        if req.options.break_step and req.options.break_step not in BREAKABLE:
            raise HTTPException(422, f"break_step must be one of {BREAKABLE}")
        try:
            if req.input_file:
                if not INPUT_NAME.match(req.input_file):
                    raise HTTPException(422, "input_file must be a file name from /api/inputs")
                path = settings.inputs_dir / req.input_file
                if not path.is_file():
                    raise HTTPException(404, "No such input file")
                run_input = load_input(path)
                raw = json.loads(path.read_text(encoding="utf-8"))
            else:
                raw = req.input or {}
                run_input = RunInput.model_validate(raw)
        except ConfigError as exc:
            raise HTTPException(422, str(exc)) from exc
        except ValidationError as exc:
            raise HTTPException(422, json.loads(exc.json(include_url=False))) from exc

        # Server decides where things are written and which site gets the
        # credentials (base_url is where the password and 2FA code are typed),
        # and API jobs are always headless.
        for key in ("state_dir", "output_dir", "base_url"):
            if key in raw:
                raise HTTPException(422, f"{key} cannot be set through the API")
        if run_input.proxy and run_input.proxy.password:
            raise HTTPException(422, "Inline proxy passwords are not accepted by the API; "
                                     "use proxy_route with BRAVEBIRD_PROXY_<ROUTE>")
        try:
            run_input.effective_proxy()
        except ConfigError as exc:
            raise HTTPException(422, str(exc)) from exc
        run_input = run_input.model_copy(update={
            "state_dir": settings.state_dir, "output_dir": settings.output_dir,
            "headless": True, "captcha_wait_seconds": 0})

        options = RunOptions(simulate_wrong_code=req.options.simulate_wrong_code,
                             break_step=req.options.break_step)
        label = req.label or req.input_file or run_input.document.release_tag
        return queue.submit(run_input, options, label).public()

    @app.get("/api/jobs", dependencies=guard)
    def list_jobs(limit: Annotated[int, Query(ge=1, le=500)] = 50) -> list[dict]:
        return [j.public() for j in store.list(limit)]

    @app.get("/api/jobs/{job_id}", dependencies=guard)
    def get_job(job_id: str) -> dict:
        return job_or_404(job_id).public()

    @app.post("/api/jobs/{job_id}/cancel", dependencies=guard)
    def cancel(job_id: str) -> dict:
        job = job_or_404(job_id)
        if not store.cancel(job.id):
            raise HTTPException(409, f"Job is {job.status}; only queued jobs can be cancelled")
        return store.get(job.id).public()

    @app.get("/api/jobs/{job_id}/manifest", dependencies=guard)
    def manifest(job_id: str) -> dict:
        path = run_dir(job_or_404(job_id)) / "manifest.json"
        if not path.is_file():
            raise HTTPException(404, "Manifest not written yet")
        return json.loads(path.read_text(encoding="utf-8"))  # already redacted on write

    @app.get("/api/jobs/{job_id}/log", response_class=PlainTextResponse, dependencies=guard)
    def run_log(job_id: str) -> str:
        path = run_dir(job_or_404(job_id)) / "run.log"
        if not path.is_file():
            raise HTTPException(404, "No log yet")
        return redactor.redact(path.read_text(encoding="utf-8"))

    @app.get("/api/jobs/{job_id}/files/{name}", dependencies=guard)
    def file(job_id: str, name: str) -> FileResponse:
        job = job_or_404(job_id)
        listed = set((job.result or {}).get("files") or [])
        if name not in listed:
            raise HTTPException(404, "No such file for this job")
        root = run_dir(job)
        path = (root / "downloads" / name).resolve()
        if path.parent != (root / "downloads") or not path.is_file():
            raise HTTPException(404, "No such file for this job")
        return FileResponse(path, filename=name)

    @app.get("/api/jobs/{job_id}/evidence/screenshot.png", dependencies=guard)
    def screenshot(job_id: str) -> FileResponse:
        path = run_dir(job_or_404(job_id)) / "evidence" / "screenshot.png"
        if not path.is_file():
            raise HTTPException(404, "No evidence for this job")
        return FileResponse(path, media_type="image/png")

    return app


def serve_main(argv: list[str] | None = None) -> int:
    import argparse

    import uvicorn

    from .config import load_credentials
    from .redact import configure_logging

    p = argparse.ArgumentParser(prog="bravebird-serve", description="Job queue + API + web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--workers", type=int, default=2, help="Jobs running at once")
    p.add_argument("--per-portal", type=int, default=2, help="Max running jobs per portal")
    p.add_argument("--env-file", default=".env")
    p.add_argument("--db", default="state/jobs.sqlite3")
    a = p.parse_args(argv)
    configure_logging(tag_runs=True)

    token = os.getenv("BRAVEBIRD_API_TOKEN") or None
    if a.host not in ("127.0.0.1", "localhost", "::1") and not token:
        log.error("Refusing to listen on %s without BRAVEBIRD_API_TOKEN: the API can sign in "
                  "to your account and download its documents.", a.host)
        return 2
    if token:
        redactor.add(token)
    try:
        creds = load_credentials(a.env_file)
    except ConfigError as exc:
        log.error("Configuration error: %s", exc)
        return 2
    # Without a token the API is only safe on loopback, and only for requests
    # that name a loopback host (stops DNS-rebinding pages from driving it).
    allowed = None if token else ["127.0.0.1", "localhost", "::1", "[::1]"]
    settings = Settings(db_path=Path(a.db), workers=a.workers, per_portal=a.per_portal,
                        token=token, allowed_hosts=allowed)
    app = create_app(settings, creds)
    log.info("Serving on http://%s:%d  (UI at /, API docs at /api/docs)", a.host, a.port)
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    return 0
