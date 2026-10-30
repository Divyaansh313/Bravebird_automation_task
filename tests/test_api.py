"""API + queue, with a fake executor (no browser, no network)."""

import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import Settings, create_app
from app.config import Credentials
from app.runner import RunResult

CREDS = Credentials(username="api-test-user", password="api-test-pass",
                    totp_secret="JBSWY3DPEHPK3PXP")
INPUT = {
    "repo": "someone/sandbox",
    "form": {"tag": "v9.9.9", "target": "main", "title": "t"},
    "document": {"release_tag": "v1.0.0", "asset_name": "doc.pdf"},
}


def fake_executor(delay: float = 0.0, status: str = "ok"):
    async def run(inputs, creds, options):
        await asyncio.sleep(delay)
        run_dir = inputs.output_dir / options.run_id
        (run_dir / "downloads").mkdir(parents=True, exist_ok=True)
        (run_dir / "downloads" / "doc.pdf").write_bytes(b"%PDF-1.4 fake")
        (run_dir / "manifest.json").write_text(json.dumps({"status": status, "stages": []}))
        (run_dir / "run.log").write_text("line for api-test-user\n")
        return RunResult(run_id=options.run_id, status=status,
                         exit_code=0 if status == "ok" else 1, run_dir=run_dir,
                         manifest_path=run_dir / "manifest.json", session="reused",
                         files=[str(run_dir / "downloads" / "doc.pdf")])
    return run


@pytest.fixture
def make_client(tmp_path):
    inputs_dir = tmp_path / "inputs"
    inputs_dir.mkdir()
    (inputs_dir / "one.json").write_text(json.dumps(INPUT))

    def make(token=None, executor=None, workers=2, allowed_hosts=None):
        settings = Settings(inputs_dir=inputs_dir, state_dir=tmp_path / "state",
                            output_dir=tmp_path / "runs", db_path=tmp_path / "state/jobs.db",
                            workers=workers, token=token, allowed_hosts=allowed_hosts)
        app = create_app(settings, CREDS, executor=executor or fake_executor())
        return TestClient(app)
    return make


def wait_for(client, job_id, statuses=("ok", "failed", "needs_human"), timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job stuck in {job['status']}")


def test_submit_run_and_fetch_outputs(make_client):
    with make_client() as c:
        assert c.get("/").status_code == 200
        assert [i["name"] for i in c.get("/api/inputs").json()] == ["one.json"]
        job = c.post("/api/jobs", json={"input_file": "one.json"}).json()
        done = wait_for(c, job["id"])
        assert done["status"] == "ok" and done["result"]["files"] == ["doc.pdf"]
        assert c.get(f"/api/jobs/{job['id']}/manifest").json()["status"] == "ok"
        assert c.get(f"/api/jobs/{job['id']}/files/doc.pdf").content.startswith(b"%PDF")
        assert "api-test-user" not in c.get(f"/api/jobs/{job['id']}/log").text  # redacted
        assert job["id"] in [j["id"] for j in c.get("/api/jobs").json()]


def test_inline_input_is_accepted(make_client):
    with make_client() as c:
        r = c.post("/api/jobs", json={"input": INPUT, "label": "inline"})
        assert r.status_code == 201
        assert wait_for(c, r.json()["id"])["label"] == "inline"


@pytest.mark.parametrize("body, why", [
    ({"input_file": "../secrets.json"}, "path in input_file"),
    ({"input_file": "missing.json"}, "unknown file"),
    ({}, "neither input nor file"),
    ({"input": {**INPUT, "state_dir": "/tmp/elsewhere"}}, "state_dir override"),
    ({"input": {**INPUT, "output_dir": "/etc"}}, "output_dir override"),
    ({"input": {**INPUT, "proxy": {"server": "http://p:8080", "password": "x"}}},
     "inline proxy password"),
    ({"input": {**INPUT, "proxy_route": "nowhere"}}, "route without env var"),
    ({"input_file": "one.json", "options": {"break_step": "rm -rf"}}, "unknown break step"),
    ({"input": {"repo": "x"}}, "invalid input"),
    ({"input": {**INPUT, "base_url": "https://evil.example"}}, "credentials to another site"),
    ({"input": {**INPUT, "portal": "../../tmp/x"}}, "path in portal (names the state file)"),
    ({"input": {**INPUT, "repo": "o/r/../../settings"}}, "path in repo"),
])
def test_rejects_unsafe_or_invalid_requests(make_client, body, why):
    with make_client() as c:
        assert c.post("/api/jobs", json=body).status_code in (404, 422), why


def test_file_endpoints_cannot_escape_the_run_dir(make_client):
    with make_client() as c:
        job = c.post("/api/jobs", json={"input_file": "one.json"}).json()
        wait_for(c, job["id"])
        for name in ("..%2Fmanifest.json", "../manifest.json", "run.log", "doc.pdf.bak"):
            assert c.get(f"/api/jobs/{job['id']}/files/{name}").status_code == 404
        assert c.get("/api/jobs/..%2F..%2Fetc/manifest").status_code == 404
        assert c.get("/api/jobs/not-a-job-id").status_code == 404


def test_token_is_required_when_configured(make_client):
    with make_client(token="s3cret-token") as c:
        assert c.get("/api/health").status_code == 200  # health stays open
        assert c.get("/api/jobs").status_code == 401
        assert c.get("/api/jobs", headers={"Authorization": "Bearer wrong"}).status_code == 401
        ok = c.get("/api/jobs", headers={"Authorization": "Bearer s3cret-token"})
        assert ok.status_code == 200


def test_cancel_only_queued_jobs(make_client):
    with make_client(executor=fake_executor(delay=0.5), workers=1) as c:
        first = c.post("/api/jobs", json={"input_file": "one.json"}).json()
        wait_for(c, first["id"], statuses=("running",))
        second = c.post("/api/jobs", json={"input_file": "one.json"}).json()
        assert c.post(f"/api/jobs/{second['id']}/cancel").json()["status"] == "cancelled"
        assert c.post(f"/api/jobs/{first['id']}/cancel").status_code == 409
        assert wait_for(c, first["id"])["status"] == "ok"


def test_db_file_is_owner_only(make_client, tmp_path):
    with make_client():
        pass
    assert oct(Path(tmp_path / "state/jobs.db").stat().st_mode & 0o777) == "0o600"


def test_foreign_host_header_is_refused_without_a_token(make_client):
    """DNS rebinding: a web page on evil.example resolving to 127.0.0.1."""
    with make_client(allowed_hosts=["127.0.0.1", "localhost"]) as c:
        assert c.get("/api/jobs", headers={"Host": "evil.example:8765"}).status_code == 400
        assert c.get("/api/jobs", headers={"Host": "127.0.0.1:8765"}).status_code == 200
