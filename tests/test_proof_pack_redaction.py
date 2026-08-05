"""
Rule: the proof pack is "Redacted at capture time, size-capped with truncation markers."

"At capture time" is the property under test. A redaction step that runs before
publishing is one you can forget; these tests assert that the *only* way bytes
reach the pack is through a writer that redacts, so forgetting is not available.

The token-leak cases are the ones that would actually hurt: early integration piped a
live GMS token into a subprocess whose responses land in this pack.
"""

from __future__ import annotations

import json

import pytest

from backend.v2.proofpack import (
    MAX_ARTIFACT_BYTES, TRUNCATION_MARKER, ProofPack, redact,
)


class TestRedaction:
    @pytest.mark.parametrize("secret", [
        "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abc123def456",
        "bearer abcdef0123456789abcdef",
    ])
    def test_bearer_tokens(self, secret):
        assert "REDACTED" in redact(f"header: {secret}")
        assert "abcdef0123456789" not in redact(f"header: {secret}")

    def test_authorization_header_in_json(self):
        payload = json.dumps({"headers": {"Authorization": "Bearer sk-live-9f8e7d6c5b4a"}})
        out = redact(payload)
        assert "9f8e7d6c5b4a" not in out

    def test_bare_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJhIn0.QWERTY123"
        assert "[REDACTED-JWT]" in redact(f"token={jwt}")
        assert "QWERTY123" not in redact(f"token={jwt}")

    def test_groq_key_shape(self):
        key = "gsk_" + "A1b2C3d4E5f6G7h8I9j0K1l2"
        out = redact(f"GROQ_API_KEY={key}")
        assert key not in out

    @pytest.mark.parametrize("assignment", [
        "DATAHUB_GMS_TOKEN=abc123def456ghi",
        "SUBSTRATE_PG_PASSWORD: hunter2hunter2",
        "MY_API_KEY = zzzzzzzzzzzz",
    ])
    def test_env_assignments(self, assignment):
        out = redact(assignment)
        assert "[REDACTED]" in out

    def test_emails_become_the_contract_s_placeholder(self):
        """The secret-hygiene rule asks for emails -> owner@example.com specifically."""
        assert redact("owner is alice.smith@corp.internal") == \
            "owner is owner@example.com"

    def test_redaction_is_idempotent(self):
        once = redact("Authorization: Bearer abcdefgh12345678")
        assert redact(once) == once

    def test_non_secrets_survive(self):
        """Over-redaction destroys reproducibility, which is its own failure."""
        text = ("urn:li:dataset:(urn:li:dataPlatform:postgres,"
                "devguard.raw.users,PROD) has column user_id")
        assert redact(text) == text

    def test_localhost_is_deliberately_not_redacted(self):
        assert redact("http://localhost:8080/api/graphql") == \
            "http://localhost:8080/api/graphql"


class TestCapture:
    def test_write_redacts(self, tmp_path):
        pack = ProofPack(tmp_path, "run-1")
        ref = pack.write("a.json", {"Authorization": "Bearer supersecrettoken123"})
        assert "supersecrettoken123" not in open(ref).read()

    def test_write_accepts_objects_and_serialises(self, tmp_path):
        pack = ProofPack(tmp_path, "run-1")
        ref = pack.write("a.json", {"k": ["v", 1]})
        assert json.loads(open(ref).read()) == {"k": ["v", 1]}

    def test_oversize_payload_is_capped_and_marked(self, tmp_path):
        pack = ProofPack(tmp_path, "run-1")
        ref = pack.write("big.txt", "x" * (MAX_ARTIFACT_BYTES + 5000))
        body = open(ref).read()
        assert body.rstrip("\n").endswith(TRUNCATION_MARKER)
        assert len(body.encode()) < MAX_ARTIFACT_BYTES + len(TRUNCATION_MARKER) + 10

    def test_under_cap_is_untouched(self, tmp_path):
        pack = ProofPack(tmp_path, "run-1")
        ref = pack.write("small.txt", "hello")
        assert open(ref).read() == "hello\n"
        assert pack.artifacts[0]["truncated"] is False

    def test_nested_names_create_directories(self, tmp_path):
        pack = ProofPack(tmp_path, "run-1")
        ref = pack.write("pathfinder/lineage.json", {"a": 1})
        assert "pathfinder" in ref

    def test_index_lists_every_artifact(self, tmp_path):
        pack = ProofPack(tmp_path, "run-1")
        pack.write("a.txt", "a")
        pack.write("b/c.txt", "c")
        index = json.loads(open(pack.write_index()).read())
        assert index["artifact_count"] == 2
        assert {a["name"] for a in index["artifacts"]} == {"a.txt", "b/c.txt"}

    def test_same_run_id_is_the_same_directory(self, tmp_path):
        """Idempotent re-runs must not scatter evidence across directories."""
        assert ProofPack(tmp_path, "r").root == ProofPack(tmp_path, "r").root
