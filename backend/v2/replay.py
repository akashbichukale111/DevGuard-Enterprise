"""
replay.py — turn a captured proof pack into the bundle the UI renders (the zero-infrastructure replay requirement).

The zero-infrastructure replay requirement asks for `--replay <run-id>` to drive "this exact UI from the proof pack
with **zero infrastructure** — no DataHub, no LLM key, no Postgres". That
constraint is the whole design:

  * Everything the UI needs must be **in one file**. A bundle that references
    `evidence/proof-pack/...` paths at render time is not zero-infrastructure —
    it needs a filesystem and a server to read it. So every raw payload a panel
    can open is embedded here, under `raw`, keyed by its `raw_ref`.
  * Nothing may be computed that the pack does not contain. Where a value was
    never captured this emits `None`, and the metrics strip requires the UI to render that
    as `N/A` — never a placeholder number. `_missing` records why, so the
    absence is legible rather than looking like a bug.

The blast-radius numbers are parsed with Pathfinder's own parsers rather than a
second implementation. That is deliberate: `_impacted` carries a hard-won
correction (it counts only `searchResults`, because an earlier version swept up
DataHub's facet aggregations and inflated a 5-dataset radius to 9). Re-deriving
those numbers here would mean re-acquiring that bug, and a blast radius that
disagrees with the evidence chain it sits next to is worse than no panel.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from backend.v2.agents.pathfinder import _impacted, _path_hops, _queries

#: The table order. The rail is one node per agent in this order — not one per
#: handoff record — because the rail is the pipeline, and an agent that never
#: ran is a fact worth showing. `human` and `auditor` are terminals that appear
#: only as `to_agent`; they are rendered, but they never own a record.
AGENT_ORDER: tuple[str, ...] = (
    "watcher",
    "archivist",
    "cartographer",
    "pathfinder",
    "sentinel",
    "diagnostician",
    "surgeon",
    "referee",
    "magistrate",
    "scribe",
    "auditor",
)

#: The Command Center's state machine, in order. A run's state is the furthest one its
#: artifacts actually justify — see `_derive_state`.
INCIDENT_STATES: tuple[str, ...] = (
    "DETECTED",
    "DIAGNOSING",
    "AWAITING_APPROVAL",
    "REMEDIATING",
    "VERIFIED",
    "KNOWLEDGE_WRITTEN",
)

#: Embedding is capped so one pathological artifact cannot make the bundle
#: unloadable in a browser. ProofPack already caps each file at 256 KiB at
#: capture time; this is the second, smaller cap for what ships to the UI.
MAX_EMBEDDED_BYTES = 96 * 1024
EMBED_TRUNCATION_MARKER = "\n... [TRUNCATED FOR REPLAY BUNDLE — open the file in the repo]"

#: DataHub's UI route for an entity. Used only to build links; nothing is
#: fetched. If no base URL is supplied the links are omitted rather than
#: pointed at a host that may not exist.
_DATASET_PATH = "/dataset/"


class ReplayBuildError(RuntimeError):
    """The pack cannot produce a bundle — malformed or missing required files."""


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _embed(text: str) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= MAX_EMBEDDED_BYTES:
        return text, False
    return encoded[:MAX_EMBEDDED_BYTES].decode("utf-8", errors="ignore") + EMBED_TRUNCATION_MARKER, True


def _parse_ts(value: Optional[str]) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _entity_link(urn: Optional[str], base: Optional[str]) -> Optional[str]:
    """A DataHub UI link, or None when we have no base URL to build one from.

    The Command Center wants the dataset URN to link into DataHub. In replay there may be no
    DataHub at all, so a link is only emitted when the caller supplies the base
    URL it was captured against. A dead link in the money-shot panel is worse
    than a plain URN.
    """
    if not urn or not base:
        return None
    return f"{base.rstrip('/')}{_DATASET_PATH}{urn}/"


class ReplayBundleBuilder:
    """Reads one `evidence/proof-pack/<run-id>/` directory. Writes nothing."""

    def __init__(self, pack_dir: Path, *, datahub_base_url: Optional[str] = None,
                 repo_root: Optional[Path] = None) -> None:
        self.pack = Path(pack_dir)
        if not self.pack.is_dir():
            raise ReplayBuildError(f"not a proof-pack directory: {self.pack}")
        self.run_id = self.pack.name
        self.base_url = datahub_base_url
        self.repo_root = Path(repo_root) if repo_root else self.pack.parents[2]
        #: Reasons a field is None, so the UI can say *why* it renders N/A.
        self._missing: dict[str, str] = {}

    # ---- small helpers over the pack ------------------------------------

    def _path(self, rel: str) -> Path:
        return self.pack / rel

    def _has(self, rel: str) -> bool:
        return self._path(rel).is_file()

    def _json_or_none(self, rel: str) -> Any:
        p = self._path(rel)
        if not p.is_file():
            return None
        try:
            return _read_json(p)
        except json.JSONDecodeError as exc:
            raise ReplayBuildError(f"{p} is not valid JSON: {exc}") from exc

    def _text_or_none(self, rel: str) -> Optional[str]:
        p = self._path(rel)
        return _read_text(p) if p.is_file() else None

    def _note_missing(self, field: str, why: str) -> None:
        self._missing[field] = why

    def _ref(self, rel: str) -> str:
        """The canonical key for an artifact — the same string INDEX.json uses.

        There is exactly one spelling of a `raw_ref` in a bundle, and it is the
        repo-relative path, because that is what `INDEX.json` and the evidence
        chain already record. Panels look artifacts up in `raw` by this key, so
        a second spelling (`<run-id>/…`) would render a dead "open raw" button
        on every panel that used it while the evidence chips kept working.
        """
        pack = str(self.pack.relative_to(self.repo_root)) if self._is_relative(self.pack) \
            else str(self.pack)
        return f"{pack}/{rel}"

    # ---- the sections ----------------------------------------------------

    def build(self) -> dict:
        index = self._json_or_none("INDEX.json")
        if index is None:
            raise ReplayBuildError(f"{self.pack}/INDEX.json is missing — not a proof pack")

        chain = self._json_or_none("evidence-chain.json") or {}
        rail_records = self._json_or_none("handoff-rail.json") or []
        write_back = self._json_or_none("scribe/write-back-summary.json")
        approval = self._json_or_none("magistrate/approval.json")
        autonomy = self._json_or_none("magistrate/autonomy-decision.json")

        incident_id = chain.get("incident_id") or self._incident_from_rail(rail_records)

        bundle: dict[str, Any] = {
            "schema_version": 1,
            "run_id": self.run_id,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "source_pack": str(self.pack.relative_to(self.repo_root))
            if self._is_relative(self.pack) else str(self.pack),
            "pack_written_at": index.get("written_at"),
            "artifact_count": index.get("artifact_count"),
            "banners": self._banners(write_back, chain),
            "incident": self._incident(incident_id, chain, rail_records, autonomy, approval,
                                       write_back),
            "rail": self._rail(rail_records),
            "evidence": self._evidence(chain),
            "chain": {
                "digest": chain.get("digest"),
                "is_sufficient": chain.get("is_sufficient"),
                "missing_for_sufficiency": chain.get("missing_for_sufficiency") or [],
                "datahub_error": chain.get("datahub_error"),
            },
            "prior_knowledge": self._prior_knowledge(rail_records),
            "catalog": self._catalog(rail_records),
            "blast_radius": self._blast_radius(),
            "root_cause": self._root_cause(chain, rail_records),
            "policy": self._policy(autonomy, approval),
            "write_back": self._write_back(write_back),
            "security": self._security(),
            "metrics": self._metrics(rail_records, chain),
            "artifacts": index.get("artifacts") or [],
        }
        bundle["raw"] = self._raw_payloads(index)
        bundle["missing"] = dict(self._missing)
        return bundle

    def _is_relative(self, p: Path) -> bool:
        try:
            p.relative_to(self.repo_root)
            return True
        except ValueError:
            return False

    @staticmethod
    def _incident_from_rail(rail: list) -> Optional[str]:
        for rec in rail:
            if isinstance(rec, dict) and rec.get("incident_id"):
                return rec["incident_id"]
        return None

    # -- 1. header + mode banners -----------------------------------------

    def _banners(self, write_back: Optional[dict], chain: dict) -> dict:
        """The Command Center's always-visible mode banners.

        `replay` is unconditionally true: a bundle exists only because someone
        replayed a recorded run, and the Command Center requires that to be stated on screen.
        """
        dry_run = False
        if isinstance(write_back, dict):
            outcomes = {a.get("outcome") for a in write_back.get("artifacts") or []}
            dry_run = "skipped_dry_run" in outcomes
        seeded = any(
            item.get("source") == "SEEDED_DEMO" for item in chain.get("items") or []
        )
        return {
            "replay": True,
            "replay_text": "REPLAY OF RECORDED RUN — NOT LIVE",
            "dry_run": dry_run,
            "seeded_catalog": seeded,
        }

    def _incident(self, incident_id, chain, rail, autonomy, approval, write_back) -> dict:
        target = None
        if isinstance(autonomy, dict):
            target = autonomy.get("target_urn")
        if not target:
            target = self._first_urn(chain)
        started, ended = self._window(rail)
        elapsed = (ended - started).total_seconds() * 1000 if started and ended else None
        if elapsed is None:
            self._note_missing("incident.elapsed_ms",
                               "no handoff records carried timestamps")
        severity = None
        if isinstance(autonomy, dict):
            severity = autonomy.get("risk")
        if severity is None and isinstance(approval, dict):
            severity = approval.get("risk")
        if severity is None:
            self._note_missing("incident.severity",
                               "the run stopped before the Magistrate classified risk")
        state, reached = self._derive_state(chain, rail, approval, write_back)
        return {
            "incident_id": incident_id,
            "dataset_urn": target,
            "datahub_link": _entity_link(target, self.base_url),
            "severity": severity,
            "state": state,
            "states": list(INCIDENT_STATES),
            "states_reached": reached,
            "started_at": started.isoformat() if started else None,
            "ended_at": ended.isoformat() if ended else None,
            "elapsed_ms": round(elapsed, 1) if elapsed is not None else None,
        }

    @staticmethod
    def _first_urn(chain: dict) -> Optional[str]:
        for item in chain.get("items") or []:
            if item.get("datahub_urn"):
                return item["datahub_urn"]
        return None

    @staticmethod
    def _window(rail: list) -> tuple[Optional[datetime], Optional[datetime]]:
        starts, ends = [], []
        for rec in rail:
            s, e = _parse_ts(rec.get("started_at")), _parse_ts(rec.get("ended_at"))
            if s:
                starts.append(s)
            if e:
                ends.append(e)
        return (min(starts) if starts else None, max(ends) if ends else None)

    def _derive_state(self, chain, rail, approval, write_back) -> tuple[Optional[str], list]:
        """The furthest state the artifacts *justify* — never an assumed one.

        Each state is claimed by the presence of the artifact that proves it, so
        a partial run (a chain-only pack, a refusal) reports where it
        actually stopped instead of the state a complete run would have reached.
        """
        reached: list[str] = []
        if chain.get("items"):
            reached.append("DETECTED")
        agents = {r.get("from_agent") for r in rail}
        if "pathfinder" in agents or "diagnostician" in agents:
            reached.append("DIAGNOSING")
        if isinstance(approval, dict) and approval.get("decided_at_utc"):
            reached.append("AWAITING_APPROVAL")
        if self._has("surgeon/proposed.patch"):
            reached.append("REMEDIATING")
        if self._has("referee/recovery.txt"):
            reached.append("VERIFIED")
        if isinstance(write_back, dict) and write_back.get("performed") \
                and not any(a.get("outcome") == "skipped_dry_run"
                            for a in write_back.get("artifacts") or []):
            reached.append("KNOWLEDGE_WRITTEN")
        ordered = [s for s in INCIDENT_STATES if s in reached]
        return (ordered[-1] if ordered else None), ordered

    # -- 2. the handoff rail ----------------------------------------------

    def _rail(self, rail: list) -> list[dict]:
        """One node per the agent design agent, plus the records themselves for the detail view.

        An agent can produce proof-pack artifacts without emitting a handoff
        record — the Sentinel does exactly that, writing `patch-scan.json` while
        the Surgeon owns the edge into it. Showing it as `idle` would be a lie
        about a security control that ran, so it gets `ran_no_record` and a
        duration of None rather than a borrowed one.
        """
        by_agent: dict[str, list[dict]] = {}
        for rec in rail:
            by_agent.setdefault(rec.get("from_agent"), []).append(rec)

        produced = self._agents_with_artifacts()
        nodes: list[dict] = []
        for agent in AGENT_ORDER:
            records = by_agent.get(agent) or []
            if records:
                decisions = [r.get("decision") for r in records]
                state = "done"
                if "INSUFFICIENT_EVIDENCE" in decisions:
                    state = "refused"
                elif "BLOCKED" in decisions:
                    state = "blocked"
                elif "DEGRADED" in decisions:
                    state = "degraded"
                durations = [r.get("duration_ms") for r in records
                             if isinstance(r.get("duration_ms"), (int, float))]
                tool_calls = [tc for r in records for tc in (r.get("tool_calls") or [])]
                evidence_ids = [e for r in records for e in (r.get("evidence_ids") or [])]
                tokens = sum(r.get("tokens") or 0 for r in records)
                models = sorted({r.get("model") for r in records if r.get("model")})
                nodes.append({
                    "agent": agent,
                    "state": state,
                    "duration_ms": round(sum(durations), 1) if durations else None,
                    "tokens": tokens,
                    "model": models[0] if models else None,
                    "tool_call_count": len(tool_calls),
                    "evidence_count": len(evidence_ids),
                    "evidence_ids": evidence_ids,
                    "records": records,
                })
            else:
                nodes.append({
                    "agent": agent,
                    "state": "ran_no_record" if agent in produced else "idle",
                    "duration_ms": None,
                    "tokens": None,
                    "model": None,
                    "tool_call_count": 0,
                    "evidence_count": 0,
                    "evidence_ids": [],
                    "records": [],
                })
        return nodes

    def _agents_with_artifacts(self) -> set[str]:
        return {p.name for p in self.pack.iterdir() if p.is_dir()}

    # -- 3. evidence ledger -------------------------------------------------

    def _evidence(self, chain: dict) -> list[dict]:
        out = []
        for item in chain.get("items") or []:
            out.append({
                "id": item.get("id"),
                "source": item.get("source"),
                "trust": item.get("trust"),
                "confidence": item.get("confidence"),
                "claim": item.get("claim"),
                "raw_ref": item.get("raw_ref"),
                "datahub_urn": item.get("datahub_urn"),
                "datahub_link": _entity_link(item.get("datahub_urn"), self.base_url),
                "collected_at": item.get("collected_at"),
                "untrusted": item.get("trust") == "UNTRUSTED_TEXT",
                "distinct": item.get("source") in ("DEVGUARD_INFERENCE", "SEEDED_DEMO")
                or item.get("confidence") == "INFERRED",
            })
        return out

    # -- 4. blast radius ----------------------------------------------------

    def _blast_radius(self) -> Optional[dict]:
        ds = self._json_or_none("pathfinder/get_lineage-downstream.json")
        col = self._json_or_none("pathfinder/get_lineage-downstream-column.json")
        paths = self._json_or_none("pathfinder/get_lineage_paths_between.json")
        queries = self._json_or_none("pathfinder/get_dataset_queries.json")
        if ds is None and col is None:
            self._note_missing("blast_radius", "the Pathfinder did not run in this pack")
            return None

        def impacted(doc) -> list[dict]:
            if not isinstance(doc, dict) or not doc.get("ok"):
                return []
            return [
                {"urn": e.urn, "entity_type": e.entity_type, "hops": e.hops,
                 "is_ml_model": e.is_ml_model}
                for e in _impacted(doc.get("text") or "")
            ]

        dataset_level = impacted(ds)
        column_level = impacted(col)
        column = None
        for doc in (col, queries):
            if isinstance(doc, dict):
                column = (doc.get("arguments") or {}).get("column") or column
        root = None
        for doc in (ds, col):
            if isinstance(doc, dict):
                root = (doc.get("arguments") or {}).get("urn") or root

        # Graph-confirmed vs inferred (the blast-radius panel): an entity the graph returned with
        # a hop count is confirmed. There is no inference step in this pack, so
        # nothing is marked inferred — rather than inventing a dashed node to
        # make the legend look used.
        hops = [e["hops"] for e in column_level or dataset_level if e.get("hops") is not None]
        return {
            "root_urn": root,
            "column": column,
            "dataset_level": dataset_level,
            "column_level": column_level,
            "column_path": _path_hops((paths or {}).get("text") or "")
            if isinstance(paths, dict) and paths.get("ok") else [],
            "terminal_ml_models": [e for e in dataset_level if e["is_ml_model"]],
            "max_hops": max(hops) if hops else None,
            "queries": [
                {"urn": q.urn, "statement": q.statement, "source": q.source}
                for q in _queries((queries or {}).get("text") or "")
            ] if isinstance(queries, dict) and queries.get("ok") else [],
            "queries_raw_ref": self._ref("pathfinder/get_dataset_queries.json")
            if queries is not None else None,
        }

    # -- 5. prior knowledge --------------------------------------------------

    def _prior_knowledge(self, rail: list) -> dict:
        """The prior-knowledge banner's three states, taken from the Archivist's own handoff record.

        The record's rationale is the Archivist's verdict; re-deriving it from
        the document count would drop the DEGRADED case, where the tools are
        hidden and the correct banner is RETRIEVAL UNAVAILABLE rather than "no
        prior incidents".
        """
        record = next((r for r in rail if r.get("from_agent") == "archivist"), None)
        caps = self._json_or_none("archivist/capabilities.json")
        search = self._json_or_none("archivist/search_documents.json")
        documents = self._document_urns(search)
        if record is None:
            self._note_missing("prior_knowledge",
                               "the Archivist did not run in this pack")
            return {"state": None, "rationale": None, "documents": [],
                    "capabilities_ref": None, "document_count": None}
        state = "PRIOR" if documents else "NONE"
        if record.get("decision") == "DEGRADED":
            state = "UNAVAILABLE"
        if state == "PRIOR":
            # The prior-knowledge banner's banner wants the prior run's time-to-root-cause. The
            # Scribe's runbook template (Root cause / How it was detected /
            # Blast radius / Fix / Recovery / Approval) never records it, so
            # there is nothing in the retrieved document to read it from.
            # Stated here so the N/A is a known gap and not a parse failure.
            self._note_missing(
                "prior_knowledge.time_to_root_cause_s",
                "the retrieved runbook does not record a time-to-root-cause — the "
                "Scribe's document template has no such section",
            )
        return {
            "state": state,
            "rationale": record.get("rationale"),
            "documents": documents,
            "document_count": len(documents),
            "time_to_root_cause_s": None,
            "capabilities_ref": self._ref("archivist/capabilities.json")
            if caps is not None else None,
        }

    def _catalog(self, rail: list) -> dict:
        """The catalog surface this run negotiated, and the part of it it used.

        Capability negotiation is a headline design property of this system —
        the tool list is what the server offered *on the day*, not a constant —
        and until now it was captured as evidence and then never shown. A
        reviewer had to open `archivist/capabilities.json` in the repository to
        find out that the set changes between runs.

        It changes for a reason worth seeing. `search_documents` and
        `grep_documents` are absent from a catalog with no documents, so the
        first run against a clean instance negotiates six tools and degrades;
        the next run — after that first one has written a runbook — negotiates
        eight and retrieves it. Two numbers on a panel make the loop closing
        visible in a way a prose claim does not.

        Everything here is read from the pack. `offered` is the server's own
        `tools/list` response; `used` is counted from the handoff records'
        `tool_calls`, which is what the agents actually invoked. Nothing is
        inferred about tools the server did not mention.
        """
        caps = self._json_or_none("archivist/capabilities.json")
        used: dict[str, int] = {}
        for record in rail:
            for call in record.get("tool_calls") or []:
                name = call.get("tool")
                if name:
                    used[name] = used.get(name, 0) + 1

        if caps is None:
            self._note_missing(
                "catalog",
                "the Archivist did not run, so no capability set was negotiated "
                "for this run")
            return {"server": None, "protocol_version": None, "offered": [],
                    "offered_count": None, "used": used, "capabilities_ref": None}

        server = caps.get("serverInfo") or {}
        offered = sorted(caps.get("tools") or [])
        return {
            "server": {
                "name": server.get("name"),
                # Deliberately reported as-is. This disagrees with the MCP
                # package version on PyPI, and the disagreement is a fact about
                # the server rather than a parsing mistake to normalise away.
                "version": server.get("version"),
            },
            "protocol_version": caps.get("protocolVersion"),
            "offered": offered,
            "offered_count": len(offered),
            "used": used,
            "capabilities_ref": self._ref("archivist/capabilities.json"),
        }

    @staticmethod
    def _document_urns(search: Any) -> list[dict]:
        """Documents from `search_documents`, read from `searchResults[].entity`.

        DataHub returns the same envelope here as it does for lineage — the hits
        are under `searchResults`, and the title is nested at `info.title`, not
        on the entity itself. An earlier version of this looked for a flat
        `documents` list, found nothing, and reported "NO PRIOR VERIFIED
        INCIDENTS" for a run whose Archivist had just retrieved four runbooks.
        That is the prior-knowledge banner banner asserting the opposite of what happened, so the
        shape is now taken from the payload the pack actually contains.
        """
        if not isinstance(search, dict) or not search.get("ok"):
            return []
        try:
            payload = json.loads(search.get("text") or "")
        except (json.JSONDecodeError, TypeError):
            return []
        if not isinstance(payload, dict):
            return []
        out: list[dict] = []
        seen: set[str] = set()
        for result in payload.get("searchResults") or []:
            entity = result.get("entity") if isinstance(result, dict) else None
            if not isinstance(entity, dict):
                continue
            urn = entity.get("urn")
            if not isinstance(urn, str) or urn in seen:
                continue
            seen.add(urn)
            info = entity.get("info") if isinstance(entity.get("info"), dict) else {}
            out.append({
                "urn": urn,
                "title": info.get("title") or entity.get("name"),
                "sub_type": entity.get("subType"),
            })
        return out

    # -- 6. root cause -------------------------------------------------------

    def _root_cause(self, chain: dict, rail: list) -> dict:
        """The chain as a chain — or the root-cause panel's full-width refusal state.

        `refused` is strictly an insufficient-evidence refusal (INSUFFICIENT_EVIDENCE), not any
        stop. The dry-run packs stop at the Diagnostician with BLOCKED because no
        reasoner was configured, and its own rationale is explicit that this is
        "NOT a refusal — the question was never asked". Collapsing the two would
        turn a missing dependency into a governance claim the run never made.
        """
        record = next((r for r in rail if r.get("from_agent") == "diagnostician"), None)
        derived = self._text_or_none("diagnostician/derived-root-cause.txt")
        diagnosis = self._json_or_none("diagnosis.json")
        decision = record.get("decision") if record else None
        refused = decision == "INSUFFICIENT_EVIDENCE" or chain.get("is_sufficient") is False
        statement = derived
        if statement is None and isinstance(diagnosis, dict):
            statement = diagnosis.get("root_cause") or diagnosis.get("statement")
        if statement is None and not refused:
            self._note_missing("root_cause.statement",
                               "no root-cause artifact in this pack")
        return {
            "refused": refused,
            "decision": decision,
            "rationale": record.get("rationale") if record else None,
            "missing_evidence_classes": chain.get("missing_for_sufficiency") or [],
            "datahub_error": chain.get("datahub_error"),
            "statement": statement.strip() if statement else None,
            "by_model": bool(record and record.get("model")),
            "chain_ids": [i.get("id") for i in chain.get("items") or []],
            "raw_ref": self._ref("diagnostician/derived-root-cause.txt")
            if derived is not None else None,
        }

    # -- 7. policy & approval -------------------------------------------------

    def _policy(self, autonomy: Optional[dict], approval: Optional[dict]) -> Optional[dict]:
        patch = self._text_or_none("surgeon/proposed.patch")
        scan = self._json_or_none("sentinel/patch-scan.json")
        validation = self._text_or_none("referee/validation.txt")
        if autonomy is None and approval is None and patch is None:
            self._note_missing("policy", "the run stopped before remediation")
            return None
        owners = (autonomy or {}).get("owners") or []
        if not owners:
            self._note_missing("policy.owners",
                               "no owner was returned by the graph for this asset")
        return {
            "risk": (autonomy or {}).get("risk") or (approval or {}).get("risk"),
            "mode": (autonomy or {}).get("mode") or (approval or {}).get("mode"),
            "allowed_action": (autonomy or {}).get("allowed_action"),
            "rationale": (autonomy or {}).get("rationale"),
            "owners": owners,
            "owner_fallback": None if owners else
            "No registered owner — the Magistrate falls back to the operator identity, "
            "and the approval records who that was.",
            "approver_urn": (approval or {}).get("approver_urn"),
            "approver_name": (approval or {}).get("approver_name"),
            "decided_at": (approval or {}).get("decided_at_utc"),
            "approval_note": (approval or {}).get("note"),
            "branch_reviewed": (approval or {}).get("branch_reviewed"),
            "proposed_diff": patch,
            "proposed_diff_ref": self._ref("surgeon/proposed.patch")
            if patch is not None else None,
            "sentinel_scan": scan,
            "validation": validation,
            "validation_ref": self._ref("referee/validation.txt")
            if validation is not None else None,
        }

    # -- 8. write-back ---------------------------------------------------------

    def _write_back(self, summary: Optional[dict]) -> Optional[dict]:
        if not isinstance(summary, dict):
            self._note_missing("write_back", "the run stopped before the Scribe")
            return None
        artifacts = []
        for art in summary.get("artifacts") or []:
            urn = art.get("target_urn")
            detail = art.get("detail") or ""
            # Artifacts 1 and 2 record the URN they *created* in `detail`; that
            # is the URN a judge wants to click, not the dataset it hangs off.
            created = detail if detail.startswith("urn:li:") else None
            artifacts.append({
                **art,
                "created_urn": created,
                "datahub_link": _entity_link(created or urn, self.base_url),
            })
        return {
            "performed": summary.get("performed"),
            "reason": summary.get("reason") or None,
            "incident_resolved": summary.get("incident_resolved"),
            "all_landed": summary.get("all_landed"),
            "artifacts": sorted(artifacts, key=lambda a: a.get("number") or 0),
            "guard": "Nothing is written until recovery is verified.",
        }

    # -- 9. security -----------------------------------------------------------

    def _security(self) -> dict:
        """Injection screening for THIS run.

        The injection demo lives in its own pack (`security/injection-demo`), so for a
        loop pack the honest answer is "screened, nothing detected" — evidenced
        by the fenced document artifacts — not a borrowed detection count from
        a different run.
        """
        untrusted = sorted(
            p.name for p in (self.pack / "archivist").glob("document-*.txt")
        ) if (self.pack / "archivist").is_dir() else []
        seed = self._json_or_none("01-seed-attack.json")
        screen = self._json_or_none("02-sentinel-screen.json")
        outcome = self._json_or_none("04-outcome.json")
        if screen is not None or seed is not None:
            detections = (screen or {}).get("detections") or (screen or {}).get("patterns") or []
            return {
                "is_demo": True,
                "sources_scanned": (screen or {}).get("sources_scanned"),
                "attempts_detected": len(detections) if detections else
                (screen or {}).get("attempts_detected"),
                "detections": detections,
                "action_taken": (outcome or {}).get("action")
                or (outcome or {}).get("summary"),
                "outcome": outcome,
                "untrusted_sources": untrusted,
            }
        return {
            "is_demo": False,
            "sources_scanned": len(untrusted),
            "attempts_detected": 0,
            "detections": [],
            "action_taken": "All catalog free-text was fenced as UNTRUSTED_TEXT before "
                            "entering any prompt. No injection pattern matched in this run."
            if untrusted else None,
            "untrusted_sources": untrusted,
        }

    # -- 10. metrics -----------------------------------------------------------

    def _metrics(self, rail: list, chain: dict) -> dict:
        """The metrics strip. Every value here is summed from captured records or is None.

        Cost is the honest N/A case and stays that way: no model ran in any
        captured loop (`model` is null on every record, and the Diagnostician
        reports REASONER_UNAVAILABLE), so there is no cost to render. Emitting
        $0.00 would read as "this was free" rather than "this was never
        measured", which is exactly the placeholder the metrics strip forbids.
        """
        started, ended = self._window(rail)
        total = (ended - started).total_seconds() * 1000 if started and ended else None

        # Time-to-root-cause: detection until the last diagnosis-stage agent
        # handed off. Uses the Diagnostician's end when it ran, else the
        # Pathfinder's — the point at which the cause was known.
        #
        # A refused run has no time-to-root-cause, because it has no root cause.
        # Reporting the elapsed time anyway would put a headline number on the
        # one run that deliberately produced none, and it reads as a success
        # metric directly above a full-width INSUFFICIENT EVIDENCE panel.
        refused = chain.get("is_sufficient") is False
        ttrc = None
        if refused:
            self._note_missing(
                "metrics.time_to_root_cause_ms",
                "the chain was insufficient and the Diagnostician refused, so no root "
                "cause was reached — there is no time-to-root-cause to report",
            )
        elif started:
            for agent in ("diagnostician", "pathfinder"):
                rec = next((r for r in rail if r.get("from_agent") == agent), None)
                end = _parse_ts(rec.get("ended_at")) if rec else None
                if end:
                    ttrc = (end - started).total_seconds() * 1000
                    break
        if ttrc is None and not refused:
            self._note_missing("metrics.time_to_root_cause_ms",
                               "no diagnosis-stage handoff carried timestamps")

        tool_calls = sum(len(r.get("tool_calls") or []) for r in rail)
        tokens = sum(r.get("tokens") or 0 for r in rail)
        models = sorted({r.get("model") for r in rail if r.get("model")})
        if not models:
            self._note_missing(
                "metrics.cost_usd",
                "no model was invoked in this run (every handoff records model=null; "
                "the Diagnostician reports REASONER_UNAVAILABLE), so there is no cost "
                "to report — this is not a zero cost, it is an unmeasured one.",
            )
        return {
            "time_to_root_cause_ms": round(ttrc, 1) if ttrc is not None else None,
            "total_loop_ms": round(total, 1) if total is not None else None,
            "mcp_calls": tool_calls,
            "tokens": tokens if models else None,
            "tokens_note": None if models else
            "0 recorded — no model was invoked in this run",
            "cost_usd": None,
            "models": models,
            "evidence_count": len(chain.get("items") or []),
            "handoff_count": len(rail),
            "source": "summed from handoff-rail.json in this proof pack",
        }

    # -- raw payloads ------------------------------------------------------------

    def _raw_payloads(self, index: dict) -> dict:
        """Every artifact the pack recorded, embedded by its `raw_ref`.

        Keyed exactly as the evidence chain references them so a chip click is a
        dictionary lookup, with no path resolution and no filesystem, which is
        what makes zero-infrastructure replay's "zero infrastructure" literally true.
        """
        out: dict[str, dict] = {}
        for art in index.get("artifacts") or []:
            ref = art.get("raw_ref")
            name = art.get("name")
            if not ref or not name:
                continue
            path = self.pack / name
            if not path.is_file():
                # The pack claims an artifact it does not have. Recorded rather
                # than skipped, so the UI shows the gap instead of a dead chip.
                out[ref] = {"missing": True,
                            "note": f"listed in INDEX.json but not present at {name}"}
                continue
            text, truncated = _embed(_read_text(path))
            out[ref] = {
                "name": name,
                "bytes": art.get("bytes"),
                "note": art.get("note"),
                "written_at": art.get("written_at"),
                "truncated_in_bundle": truncated,
                "content": text,
            }
        return out


def build_bundle(pack_dir: Path, *, datahub_base_url: Optional[str] = None,
                 repo_root: Optional[Path] = None) -> dict:
    """Convenience wrapper — build one bundle from one pack directory."""
    return ReplayBundleBuilder(pack_dir, datahub_base_url=datahub_base_url,
                               repo_root=repo_root).build()
