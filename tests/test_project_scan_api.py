"""
API surface for ZIP and repository scans.

These drive the endpoints through FastAPI's TestClient with the pipeline
stubbed, so the job lifecycle, the limits and the honesty rules are all
exercised without an API key or a network clone. The collection logic itself
is covered separately in `test_project_scan.py`.

The honesty rule under test is the one that is easy to get wrong: a project
where every file failed to scan has found nothing, but it has not established
that there is nothing to find. It must not report as a clean scan.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from backend.core.schemas import (
    FixResult,
    PipelineResult,
    ScanResult,
    Severity,
    Vulnerability,
)


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    from backend.main import app

    with TestClient(app) as c:
        yield c


def make_zip(entries: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in entries.items():
            z.writestr(name, content)
    return buf.getvalue()


def upload(client: TestClient, data: bytes, name: str = "project.zip"):
    return client.post(
        "/scan/zip", files={"file": (name, data, "application/zip")}
    )


def _pipeline_result(code: str, findings: int) -> PipelineResult:
    vulns = [
        Vulnerability(
            cwe_id="CWE-89",
            severity=Severity.HIGH,
            line_number=1,
            explanation="Concatenated user input into a SQL statement; " * 2,
            confidence_score=0.9,
        )
        for _ in range(findings)
    ]
    return PipelineResult(
        original_code=code,
        scan=ScanResult(vulnerabilities=vulns, model_used="stub"),
        final_fix=FixResult(
            patched_code=code,
            diff_summary="stubbed fix summary for tests",
            addressed_cwe_ids=[],
            model_used="stub",
        ),
        final_validation=None,
        reflection_history=[],
        converged=True,
        routing_decisions={},
    )


@pytest.fixture()
def stub_pipeline(monkeypatch):
    """Replace the pipeline so no model call happens. One finding per file."""
    from backend.api import router as router_mod

    async def fake(request):
        return _pipeline_result(request.code, findings=1)

    async def no_cache(request):
        return None

    async def no_store(request, result):
        return None

    monkeypatch.setattr(router_mod, "run_pipeline_resilient", fake)
    monkeypatch.setattr(router_mod.cache, "get_cached", no_cache)
    monkeypatch.setattr(router_mod.cache, "set_cached", no_store)


def drain(client: TestClient, project_id: str) -> dict:
    """TestClient runs the background task to completion within the request."""
    r = client.get(f"/scan/project/{project_id}")
    assert r.status_code == 200
    return r.json()


# --------------------------------------------------------------------------- #
# ZIP
# --------------------------------------------------------------------------- #

def test_zip_scan_reports_findings_per_file(client, stub_pipeline):
    data = make_zip({"src/a.py": "import os\n", "src/B.java": "class B {}\n"})
    started = upload(client, data)
    assert started.status_code == 200, started.text
    body = started.json()
    assert body["total"] == 2
    assert body["collection"]["files_scanned"] == 2

    state = drain(client, body["project_id"])
    assert state["status"] == "complete"
    assert state["files_analysed"] == 2
    assert state["files_failed"] == 0
    assert state["total_vulnerabilities"] == 2
    assert {f["path"] for f in state["files"]} == {"src/a.py", "src/B.java"}
    assert {f["language"] for f in state["files"]} == {"python", "java"}


def test_zip_scan_excludes_dependencies_and_traversal(client, stub_pipeline):
    data = make_zip(
        {
            "src/app.py": "import os\n",
            "node_modules/x/i.js": "var a = 1;\n",
            "../escape.py": "import os\n",
            "README.md": "# docs\n",
        }
    )
    state = drain(client, upload(client, data).json()["project_id"])
    assert [f["path"] for f in state["files"]] == ["src/app.py"]


def test_zip_without_source_files_is_rejected(client):
    r = upload(client, make_zip({"README.md": "# docs\n"}))
    assert r.status_code == 422
    assert "No scannable source files" in r.json()["detail"]


def test_corrupt_zip_is_rejected(client):
    r = upload(client, b"not a zip at all")
    assert r.status_code == 422
    assert "not a valid ZIP" in r.json()["detail"]


def test_empty_upload_is_rejected(client):
    r = upload(client, b"")
    assert r.status_code == 422


def test_truncation_is_reported_in_the_response(client, stub_pipeline):
    from backend.core import project_scan

    entries = {f"src/f{i:03d}.py": "import os\n" for i in range(project_scan.MAX_FILES + 5)}
    body = upload(client, make_zip(entries)).json()
    assert body["collection"]["truncated"] is True
    assert body["collection"]["source_files_found"] == project_scan.MAX_FILES + 5
    assert body["total"] == project_scan.MAX_FILES


# --------------------------------------------------------------------------- #
# Honesty: a failed scan is not a clean scan
# --------------------------------------------------------------------------- #

def test_all_files_failing_reports_failed_not_clean(client, monkeypatch):
    """
    The bug this guards: every file erroring produced status "complete" with
    total_vulnerabilities 0, which reads as a clean bill of health for code
    that was never analysed.
    """
    from backend.api import router as router_mod

    async def always_fails(request):
        raise RuntimeError("[scanner] LLM call failed")

    async def no_cache(request):
        return None

    monkeypatch.setattr(router_mod, "run_pipeline_resilient", always_fails)
    monkeypatch.setattr(router_mod.cache, "get_cached", no_cache)

    data = make_zip({"a.py": "import os\n", "b.py": "import sys\n"})
    state = drain(client, upload(client, data).json()["project_id"])

    assert state["status"] == "failed"
    assert state["files_analysed"] == 0
    assert state["files_failed"] == 2
    assert state["total_vulnerabilities"] == 0
    assert "no code was analysed" in state["error"]


def test_partial_failure_is_distinguished_from_success(client, monkeypatch):
    from backend.api import router as router_mod

    async def fails_on_b(request):
        if "import sys" in request.code:
            raise RuntimeError("[scanner] LLM call failed")
        return _pipeline_result(request.code, findings=1)

    async def no_cache(request):
        return None

    async def no_store(request, result):
        return None

    monkeypatch.setattr(router_mod, "run_pipeline_resilient", fails_on_b)
    monkeypatch.setattr(router_mod.cache, "get_cached", no_cache)
    monkeypatch.setattr(router_mod.cache, "set_cached", no_store)

    data = make_zip({"a.py": "import os\n", "b.py": "import sys\n"})
    state = drain(client, upload(client, data).json()["project_id"])

    assert state["status"] == "complete_with_errors"
    assert state["files_analysed"] == 1
    assert state["files_failed"] == 1
    # Counted only over files that were actually analysed.
    assert state["total_vulnerabilities"] == 1


def test_one_failure_does_not_abort_the_project(client, monkeypatch):
    from backend.api import router as router_mod

    async def fails_on_one(request):
        if "boom" in request.code:
            raise RuntimeError("bad file")
        return _pipeline_result(request.code, findings=1)

    async def no_cache(request):
        return None

    async def no_store(request, result):
        return None

    monkeypatch.setattr(router_mod, "run_pipeline_resilient", fails_on_one)
    monkeypatch.setattr(router_mod.cache, "get_cached", no_cache)
    monkeypatch.setattr(router_mod.cache, "set_cached", no_store)

    data = make_zip(
        {"a.py": "import os\n", "bad.py": "boom = 1\n", "c.py": "import json\n"}
    )
    state = drain(client, upload(client, data).json()["project_id"])
    assert len(state["files"]) == 3
    assert state["files_analysed"] == 2


# --------------------------------------------------------------------------- #
# Repository — the SSRF boundary, at the HTTP layer
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "git://github.com/o/r",
        "ssh://git@github.com/o/r",
        "http://github.com/o/r",
        "https://169.254.169.254/latest/meta-data",
        "https://localhost/r",
        "https://127.0.0.1/r",
        "https://10.0.0.1/r",
        "https://evil.example.com/r",
        "https://user:token@github.com/o/r",
        "",
    ],
)
def test_repository_endpoint_rejects_dangerous_urls(client, url):
    r = client.post("/scan/repository", json={"repo_url": url})
    assert r.status_code == 422, f"{url} was not rejected"


def test_repository_endpoint_rejects_missing_url(client):
    assert client.post("/scan/repository", json={}).status_code == 422


def test_repository_endpoint_never_shells_out_for_a_rejected_url(client, monkeypatch):
    """URL validation must happen before any git invocation."""
    from backend.core import project_scan

    called = []

    def spy(url, destination):
        called.append(url)

    monkeypatch.setattr(project_scan, "clone_repository", spy)
    client.post("/scan/repository", json={"repo_url": "file:///etc/passwd"})
    assert called == []


# --------------------------------------------------------------------------- #
# Status endpoint
# --------------------------------------------------------------------------- #

def test_unknown_project_id_is_404(client):
    r = client.get("/scan/project/does-not-exist")
    assert r.status_code == 404
    assert "expired" in r.json()["detail"]
