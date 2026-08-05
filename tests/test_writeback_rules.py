"""
The write-back rules, and the autonomy policy's autonomy policy, pinned.

Every rule here is one where a regression is invisible until it matters:

  * "Nothing is written before recovery is verified" — a broken check does not
    fail anything; it just starts polluting a shared catalog with claims about
    fixes that did not work.
  * The partial-failure policy — a broken check leaves the graph asserting a
    verified state whose supporting knowledge is missing, which is worse than an
    incomplete write because it is confidently wrong.
  * CRITICAL has no approver — a widened policy table would let an automated
    agent get destructive DDL rubber-stamped.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from backend.v2.agents.magistrate import (
    AUTONOMY_POLICY, ApprovalMode, ApprovalRequest, Owner, _owners,
)
from backend.v2.agents.referee import RecoveryReport
from backend.v2.agents.scribe import (
    ArtifactOutcome, DEVGUARD_VERSION, OWNERSHIP_TYPE_TECHNICAL, STAMP, Scribe,
)
from backend.v2.agents.surgeon import Surgeon, _referenced_identifiers, _rewrite_reference
from backend.v2.evidence import (
    Evidence, EvidenceChain, EvidenceConfidence, EvidenceSource, EvidenceTrust,
)
from backend.v2.proofpack import ProofPack
from backend.v2.sentinel import PatchRisk, Sentinel

from datetime import datetime, timezone

UTC = datetime(2026, 8, 3, 11, 0, tzinfo=timezone.utc)
DATASET = "urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.raw.users,PROD)"


def ev(seq: int, source: EvidenceSource) -> Evidence:
    return Evidence(id=f"EV-D6-{seq:03d}", source=source,
                    trust=EvidenceTrust.TRUSTED_SYSTEM,
                    confidence=EvidenceConfidence.OBSERVED, collected_at=UTC,
                    claim="a claim", raw_ref="evidence/x.json")


CHAIN = EvidenceChain(incident_id="D6").add(
    ev(1, EvidenceSource.RUNTIME), ev(2, EvidenceSource.DATAHUB_GRAPH))

VERIFIED = RecoveryReport(
    verified=True, original_failure="dbt run exited 1", now_command="dbt run",
    now_exit_code=0, now_passed_models=3, now_errored_models=0, output_tail="")
NOT_VERIFIED = RecoveryReport(
    verified=False, original_failure="dbt run exited 1", now_command="dbt run",
    now_exit_code=1, now_passed_models=1, now_errored_models=1, output_tail="")


def approved_request(owners=(Owner(urn="urn:li:corpuser:x", display_name="X"),)):
    request = ApprovalRequest(
        incident_id="D6", risk=PatchRisk.LOW, mode=ApprovalMode.NAMED_OWNER,
        allowed_action="apply after approval", owners=tuple(owners),
        target_urn=DATASET)
    return request.approve(approver_urn="urn:li:corpuser:x", approver_name="X")


@dataclass
class FakeClient:
    """Records calls; never touches a network."""
    calls: list = None
    ok: bool = True

    def __post_init__(self):
        self.calls = []

    def call(self, agent, tool, arguments):
        self.calls.append((agent, tool, arguments))

        @dataclass
        class R:
            ok: bool
            text: str = '{"success": true, "urn": "urn:li:document:test"}'
            error: str = ""
        return R(ok=self.ok, text='{"success": true, "urn": "urn:li:document:test"}'
                 if self.ok else "", error="" if self.ok else "boom")


class TestNothingIsWrittenBeforeRecoveryIsVerified:
    """Rule: 'a deliberately failing fix writes nothing'."""

    def test_unverified_recovery_writes_nothing(self, tmp_path):
        client = FakeClient()
        scribe = Scribe(client, None, ProofPack(tmp_path, "r"))
        result = scribe.write_back(
            chain=CHAIN, recovery=NOT_VERIFIED, approval=approved_request(),
            dataset_urn=DATASET, column="user_id", incident_urn="urn:li:incident:i",
            root_cause="rc")
        assert result.performed is False
        assert result.artifacts == []
        assert client.calls == [], "an unverified recovery must issue zero writes"
        assert "not verified" in result.reason.lower()

    def test_unapproved_change_writes_nothing(self, tmp_path):
        client = FakeClient()
        unapproved = ApprovalRequest(
            incident_id="D6", risk=PatchRisk.LOW, mode=ApprovalMode.NAMED_OWNER,
            allowed_action="", owners=(), target_urn=DATASET)
        result = Scribe(client, None, ProofPack(tmp_path, "r")).write_back(
            chain=CHAIN, recovery=VERIFIED, approval=unapproved,
            dataset_urn=DATASET, column="user_id", incident_urn=None, root_cause="rc")
        assert result.performed is False
        assert client.calls == []

    def test_rejected_change_writes_nothing(self, tmp_path):
        client = FakeClient()
        rejected = ApprovalRequest(
            incident_id="D6", risk=PatchRisk.LOW, mode=ApprovalMode.NAMED_OWNER,
            allowed_action="", owners=(), target_urn=DATASET,
        ).reject(approver_urn="urn:li:corpuser:x", approver_name="X")
        result = Scribe(client, None, ProofPack(tmp_path, "r")).write_back(
            chain=CHAIN, recovery=VERIFIED, approval=rejected, dataset_urn=DATASET,
            column="user_id", incident_urn=None, root_cause="rc")
        assert result.performed is False
        assert client.calls == []


class TestPartialFailurePolicy:
    """Rule: the incident is RESOLVED only after all knowledge artifacts land."""

    def test_failed_knowledge_artifact_holds_the_incident_active(self, tmp_path):
        resolved = []
        scribe = Scribe(FakeClient(ok=False), None, ProofPack(tmp_path, "r"),
                        gql=lambda q, v: resolved.append(v) or {"data": {"updateIncidentStatus": True}})
        result = scribe.write_back(
            chain=CHAIN, recovery=VERIFIED, approval=approved_request(),
            dataset_urn=DATASET, column="user_id",
            incident_urn="urn:li:incident:i", root_cause="rc")
        assert result.incident_resolved is False
        assert resolved == [], "the resolve mutation must not even be attempted"
        assert "stays ACTIVE" in result.reason

    def test_all_landed_resolves_the_incident(self, tmp_path):
        scribe = Scribe(FakeClient(ok=True), None, ProofPack(tmp_path, "r"),
                        gql=lambda q, v: {"data": {"updateIncidentStatus": True}})
        result = scribe.write_back(
            chain=CHAIN, recovery=VERIFIED, approval=approved_request(),
            dataset_urn=DATASET, column="user_id",
            incident_urn="urn:li:incident:i", root_cause="rc")
        assert result.all_landed
        assert result.incident_resolved is True

    def test_resolve_is_the_last_artifact(self, tmp_path):
        scribe = Scribe(FakeClient(), None, ProofPack(tmp_path, "r"),
                        gql=lambda q, v: {"data": {"updateIncidentStatus": True}})
        result = scribe.write_back(
            chain=CHAIN, recovery=VERIFIED, approval=approved_request(),
            dataset_urn=DATASET, column="user_id",
            incident_urn="urn:li:incident:i", root_cause="rc")
        assert result.artifacts[-1].number == 1


class TestDryRun:
    def test_dry_run_sends_nothing(self, tmp_path):
        client = FakeClient()
        result = Scribe(client, None, ProofPack(tmp_path, "r"), dry_run=True).write_back(
            chain=CHAIN, recovery=VERIFIED, approval=approved_request(),
            dataset_urn=DATASET, column="user_id",
            incident_urn="urn:li:incident:i", root_cause="rc")
        assert client.calls == []
        assert all(a.outcome in (ArtifactOutcome.SKIPPED_DRY_RUN,
                                 ArtifactOutcome.ALREADY_PRESENT)
                   for a in result.artifacts)

    def test_dry_run_is_not_reported_as_a_partial_failure(self, tmp_path):
        """A dry run has not failed at anything — it attempted nothing."""
        result = Scribe(FakeClient(), None, ProofPack(tmp_path, "r"),
                        dry_run=True).write_back(
            chain=CHAIN, recovery=VERIFIED, approval=approved_request(),
            dataset_urn=DATASET, column="user_id",
            incident_urn="urn:li:incident:i", root_cause="rc")
        assert "DRY RUN" in result.reason
        assert "stays ACTIVE" not in result.reason
        assert result.all_landed

    def test_dry_run_captures_the_exact_payloads(self, tmp_path):
        pack = ProofPack(tmp_path, "r")
        Scribe(FakeClient(), None, pack, dry_run=True).write_back(
            chain=CHAIN, recovery=VERIFIED, approval=approved_request(),
            dataset_urn=DATASET, column="user_id", incident_urn="urn:li:incident:i",
            root_cause="rc")
        names = {a["name"] for a in pack.artifacts}
        assert any("dry-run" in n for n in names)


class TestStampAndIdempotency:
    def test_every_body_carries_the_stamp(self, tmp_path):
        client = FakeClient()
        Scribe(client, None, ProofPack(tmp_path, "r")).write_back(
            chain=CHAIN, recovery=VERIFIED, approval=approved_request(),
            dataset_urn=DATASET, column="user_id", incident_urn=None, root_cause="rc")
        bodies = [str(args) for _, _, args in client.calls]
        stamped = [b for b in bodies
                   if "VERIFIED INCIDENT KNOWLEDGE — WRITTEN BACK BY DEVGUARD" in b]
        assert stamped, "no artifact carried the write-back stamp"

    def test_stamp_carries_evidence_digest_and_version(self):
        rendered = STAMP.format(evidence_ids="EV-1", digest="abc123", utc="now")
        assert "EV-1" in rendered and "abc123" in rendered
        assert DEVGUARD_VERSION in rendered

    def test_idempotency_key_is_incident_type_target(self):
        assert Scribe._key("INC", "runbook", DATASET) == f"INC|runbook|{DATASET}"

    def test_owned_asset_reports_already_present_not_a_new_write(self, tmp_path):
        client = FakeClient()
        result = Scribe(client, None, ProofPack(tmp_path, "r")).write_back(
            chain=CHAIN, recovery=VERIFIED, approval=approved_request(),
            dataset_urn=DATASET, column="user_id", incident_urn=None, root_cause="rc")
        ownership = next(a for a in result.artifacts if a.number == 5)
        assert ownership.outcome is ArtifactOutcome.ALREADY_PRESENT
        assert not any(tool == "add_owners" for _, tool, _ in client.calls)

    def test_add_owners_supplies_the_required_ownership_type(self, tmp_path):
        """Integration finding 21: it is REQUIRED, and was missing on the first run."""
        client = FakeClient()
        Scribe(client, None, ProofPack(tmp_path, "r")).write_back(
            chain=CHAIN, recovery=VERIFIED, approval=approved_request(owners=()),
            dataset_urn=DATASET, column="user_id", incident_urn=None, root_cause="rc")
        call = next(a for _, tool, a in client.calls if tool == "add_owners")
        assert call["ownership_type"] == OWNERSHIP_TYPE_TECHNICAL


class TestAutonomyPolicy:
    """The autonomy policy's table. It is the same object the docs render and the code branches on."""

    def test_critical_has_no_approver(self):
        assert AUTONOMY_POLICY[PatchRisk.CRITICAL]["mode"] is ApprovalMode.FORBIDDEN
        assert AUTONOMY_POLICY[PatchRisk.CRITICAL]["approver"] is None

    def test_critical_cannot_be_approved_by_anyone(self):
        request = ApprovalRequest(
            incident_id="D6", risk=PatchRisk.CRITICAL, mode=ApprovalMode.FORBIDDEN,
            allowed_action="", owners=(), target_urn=DATASET)
        with pytest.raises(PermissionError, match="no approval path"):
            request.approve(approver_urn="urn:li:corpuser:x", approver_name="X")

    def test_high_always_requires_a_named_owner(self):
        assert AUTONOMY_POLICY[PatchRisk.HIGH]["mode"] is ApprovalMode.NAMED_OWNER

    def test_nothing_is_autonomous_in_this_build(self):
        assert all(p["mode"] is not ApprovalMode.AUTONOMOUS
                   for p in AUTONOMY_POLICY.values())

    def test_approval_requires_a_real_identity(self):
        request = ApprovalRequest(
            incident_id="D6", risk=PatchRisk.LOW, mode=ApprovalMode.NAMED_OWNER,
            allowed_action="", owners=(), target_urn=DATASET)
        with pytest.raises(ValueError, match="real name"):
            request.approve(approver_urn="", approver_name="")

    def test_unapproved_request_cannot_proceed(self):
        request = ApprovalRequest(
            incident_id="D6", risk=PatchRisk.LOW, mode=ApprovalMode.NAMED_OWNER,
            allowed_action="", owners=(), target_urn=DATASET)
        assert not request.can_proceed

    def test_unowned_asset_is_reported_not_defaulted(self):
        request = ApprovalRequest(
            incident_id="D6", risk=PatchRisk.LOW, mode=ApprovalMode.NAMED_OWNER,
            allowed_action="", owners=(), target_urn=DATASET)
        assert request.unowned

    def test_owner_parsing_from_a_real_shape(self):
        import json
        payload = json.dumps({"entities": [{"ownership": {"owners": [
            {"owner": {"urn": "urn:li:corpuser:datahub",
                       "properties": {"displayName": "DataHub"}}}]}}]})
        owners = _owners(payload)
        assert len(owners) == 1 and owners[0].display_name == "DataHub"


class TestSurgeonRefusesRatherThanGuessing:
    def test_unambiguous_rename_is_proposed(self, tmp_path):
        model = tmp_path / "m.sql"
        model.write_text("select\n    user_id,\n    email\nfrom raw.users\n")
        proposal = Surgeon(None, ProofPack(tmp_path, "r"), repo_root=tmp_path).propose(
            "m.sql", "user_id", ("customer_id", "email"))
        assert proposal.proposed
        assert proposal.replacement_column == "customer_id"
        assert "customer_id as user_id" in proposal.patched_sql

    def test_ambiguous_rename_is_refused(self, tmp_path):
        """Two candidates: a wrong guess builds green and computes wrong numbers."""
        model = tmp_path / "m.sql"
        model.write_text("select\n    user_id,\n    email\nfrom raw.users\n")
        proposal = Surgeon(None, ProofPack(tmp_path, "r"), repo_root=tmp_path).propose(
            "m.sql", "user_id", ("customer_id", "account_id", "email"))
        assert proposal.refused
        assert "ambiguous" in proposal.refusal_reason
        assert set(proposal.candidates) == {"customer_id", "account_id"}

    def test_no_candidate_is_refused(self, tmp_path):
        model = tmp_path / "m.sql"
        model.write_text("select\n    user_id,\n    email\nfrom raw.users\n")
        proposal = Surgeon(None, ProofPack(tmp_path, "r"), repo_root=tmp_path).propose(
            "m.sql", "user_id", ("email",))
        assert proposal.refused

    def test_wrong_file_is_refused(self, tmp_path):
        model = tmp_path / "m.sql"
        model.write_text("select 1 as x\n")
        proposal = Surgeon(None, ProofPack(tmp_path, "r"), repo_root=tmp_path).propose(
            "m.sql", "user_id", ("customer_id",))
        assert proposal.refused
        assert "does not reference" in proposal.refusal_reason

    def test_comments_are_not_rewritten(self):
        sql = ("-- this model selects raw.users.user_id BY NAME\n"
               "select\n    user_id,\n    email\nfrom x\n")
        patched, count = _rewrite_reference(sql, "user_id", "customer_id")
        assert count == 1
        assert "raw.users.user_id BY NAME" in patched

    def test_identifier_extraction_ignores_jinja_and_keywords(self):
        sql = "select\n a,\n b\nfrom {{ source('raw', 'users') }}\nwhere is_active\n"
        found = _referenced_identifiers(sql)
        assert {"a", "b", "is_active"} <= found
        assert "select" not in found and "raw" not in found


class TestPatchScanning:
    def test_the_real_fix_is_low_risk(self):
        diff = ("--- a/x.sql\n+++ b/x.sql\n-    user_id,\n+    customer_id as user_id,\n")
        scan = Sentinel().scan_patch(diff)
        assert scan.risk is PatchRisk.LOW and not scan.blocked
        assert scan.files_touched == ("x.sql",)

    @pytest.mark.parametrize("line,rule", [
        ("+drop table raw.users;", "destructive-ddl"),
        ("+delete from raw.orders where 1=1;", "data-mutation"),
        ("+grant all on raw.users to public;", "grant"),
        ("+password = 'hunter2'", "secret-literal"),
    ])
    def test_dangerous_additions_are_critical_and_blocked(self, line, rule):
        scan = Sentinel().scan_patch(f"--- a/x.sql\n+++ b/x.sql\n{line}\n")
        assert scan.blocked
        assert rule in {f.rule for f in scan.findings}

    def test_removing_a_filter_is_high_risk(self):
        scan = Sentinel().scan_patch("--- a/x.sql\n+++ b/x.sql\n-where is_active\n")
        assert scan.risk is PatchRisk.HIGH

    def test_multiple_files_raise_the_class(self):
        diff = "--- a/x.sql\n+++ b/x.sql\n+a\n--- a/y.sql\n+++ b/y.sql\n+b\n"
        assert Sentinel().scan_patch(diff).risk is PatchRisk.MEDIUM
