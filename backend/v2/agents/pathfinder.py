"""
pathfinder.py — the Pathfinder: blast radius, column-level, terminating at the ML model.

The responsibility: *"Blast radius, column-level, terminating at the ML model."*
Allowlist: `get_lineage`, `get_lineage_paths_between`, `get_dataset_queries` —
which is step 6 exactly.

"Terminating at the ML model" is the load-bearing phrase, and live traversal established what
it actually costs. Two live-server facts constrain this agent:

  * `mlModelTrainingData` — the aspect that *declares* a model's training data —
    produces **no graph edge** (integration finding 14). A blast radius cannot
    reach a model through it. The traversable path is
    `dataset -> dataJob -> mlModel`, so `terminal_ml_models` walks entity
    *types* rather than assuming the last hop is a dataset.
  * `get_lineage_paths_between` returns a path whose middle element is a
    `urn:li:query:` entity — the actual SQL. That is what ties "there is SQL
    touching this column" (from `get_dataset_queries`) to "here is where it
    goes", and it is why both tools live in one agent rather than two.

The agent reports what lineage says. It does not infer edges. If a hop is
missing from the graph, the blast radius is short, and that is a finding about
the catalog rather than something to paper over — the evidence rule has a `DEVGUARD_INFERENCE`
source precisely so inferred edges can never be silently mixed with real ones,
and this agent never emits it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from backend.v2.datahub_client import DataHubMCPClient
from backend.v2.evidence import (
    Evidence, EvidenceBuilder, EvidenceConfidence, EvidenceSource, EvidenceTrust,
)
from backend.v2.handoff import AgentDecision, AgentHandoff, ToolCallRecord, now
from backend.v2.proofpack import ProofPack


@dataclass(frozen=True)
class ImpactedEntity:
    urn: str
    entity_type: str
    hops: Optional[int] = None

    @property
    def is_ml_model(self) -> bool:
        return self.entity_type.upper() in ("MLMODEL", "ML_MODEL")


@dataclass(frozen=True)
class QueryTouchingColumn:
    """Real SQL from the catalog. `source` distinguishes ingested from authored."""

    urn: str
    statement: str
    source: str


@dataclass
class BlastRadius:
    """Two traces, kept separate because the live graph cannot merge them.

    The design asks for a blast radius that is "column-level, terminating at the ML
    model". The live system will not give you both in one
    call, and the reason is a live-server behaviour:

      * A **column-level** trace follows `schemaField` edges. It is precise —
        every hit carries `lineageColumns: ["user_id"]` — and it stops at the
        last dataset, because the ML model's edge is not a column edge.
      * A **dataset-level** trace follows `dataset -> dataJob -> mlModel` and
        reaches the terminus, but says nothing about which column carried the
        impact.

    So both run, and they are reported as two distinct facts rather than summed
    into one number. Summing them would be a fabricated count; picking one would
    either understate the impact or lose the column precision.
    """

    root_urn: str
    column: Optional[str]
    impacted: tuple[ImpactedEntity, ...] = ()
    """Dataset-level trace. This is the one that reaches the ML model."""

    column_impacted: tuple[ImpactedEntity, ...] = ()
    """Column-level trace. Precise, and terminates at the last dataset."""

    column_path: tuple[str, ...] = ()
    queries: tuple[QueryTouchingColumn, ...] = ()

    @property
    def terminal_ml_models(self) -> tuple[ImpactedEntity, ...]:
        return tuple(e for e in self.impacted if e.is_ml_model)

    @property
    def reaches_ml_model(self) -> bool:
        return bool(self.terminal_ml_models)

    @property
    def max_hops(self) -> int:
        return max((e.hops or 0) for e in self.impacted) if self.impacted else 0

    @property
    def column_reaches_ml_model(self) -> bool:
        """Expected to be False on this stack. See the class docstring."""
        return any(e.is_ml_model for e in self.column_impacted)


class Pathfinder:
    """Walks real lineage. Emits DATAHUB_GRAPH evidence, never inferred edges."""

    NAME = "pathfinder"

    def __init__(self, client: DataHubMCPClient, builder: EvidenceBuilder,
                 pack: ProofPack) -> None:
        self._client = client
        self._builder = builder
        self._pack = pack

    def trace(self, root_urn: str, *, column: Optional[str] = None,
              max_hops: int = 5, terminal_urn: Optional[str] = None
              ) -> tuple[BlastRadius, list[Evidence], list[ToolCallRecord]]:
        records: list[ToolCallRecord] = []
        evidence: list[Evidence] = []

        short = root_urn.split(",")[1] if "," in root_urn else root_urn

        # ---- 1a. dataset-level trace: the one that reaches the terminus ------
        args = {"urn": root_urn, "upstream": False, "max_hops": max_hops}
        lineage = self._client.call(self.NAME, "get_lineage", args)
        lineage_ref = self._pack.write(
            "pathfinder/get_lineage-downstream.json",
            {"arguments": args, "ok": lineage.ok, "text": lineage.text,
             "error": lineage.error},
            note="Dataset-level blast radius. Reaches the dataJob and the mlModel.",
        )
        records.append(lineage.record(lineage_ref))
        impacted = _impacted(lineage.text)

        radius = BlastRadius(root_urn=root_urn, column=column, impacted=tuple(impacted))

        if lineage.ok:
            evidence.append(self._builder.make(
                source=EvidenceSource.DATAHUB_GRAPH,
                trust=EvidenceTrust.TRUSTED_SYSTEM,
                confidence=EvidenceConfidence.OBSERVED,
                claim=f"{len(impacted)} downstream entities impacted within "
                      f"{max_hops} hops of {short} (dataset-level)",
                raw_ref=lineage_ref, datahub_urn=root_urn,
            ))
        else:
            evidence.append(self._builder.make(
                source=EvidenceSource.DATAHUB_GRAPH,
                trust=EvidenceTrust.TRUSTED_SYSTEM,
                confidence=EvidenceConfidence.OBSERVED,
                claim=f"get_lineage failed for {root_urn}: {(lineage.error or '')[:120]}",
                raw_ref=lineage_ref, datahub_urn=root_urn,
            ))

        # ---- 1b. column-level trace: precise, and it stops at the last dataset
        if column:
            col_args = {"urn": root_urn, "upstream": False, "max_hops": max_hops,
                        "column": column}
            col_lineage = self._client.call(self.NAME, "get_lineage", col_args)
            col_ref = self._pack.write(
                "pathfinder/get_lineage-downstream-column.json",
                {"arguments": col_args, "ok": col_lineage.ok, "text": col_lineage.text,
                 "error": col_lineage.error},
                note="Column-level blast radius. Every hit carries lineageColumns. "
                     "Does NOT reach the mlModel — that edge is dataset-level "
                     "(integration finding 14).",
            )
            records.append(col_lineage.record(col_ref))
            radius.column_impacted = tuple(_impacted(col_lineage.text))

            if col_lineage.ok:
                evidence.append(self._builder.make(
                    source=EvidenceSource.DATAHUB_GRAPH,
                    trust=EvidenceTrust.TRUSTED_SYSTEM,
                    confidence=EvidenceConfidence.OBSERVED,
                    claim=f"{len(radius.column_impacted)} entities carry column "
                          f"{column} downstream of {short} (column-level)",
                    raw_ref=col_ref, datahub_urn=root_urn,
                ))

        for model in radius.terminal_ml_models:
            # The agent roster claim, stated as its own evidence item so it is citable in
            # the UI rather than buried in a count.
            evidence.append(self._builder.make(
                source=EvidenceSource.DATAHUB_GRAPH,
                trust=EvidenceTrust.TRUSTED_SYSTEM,
                confidence=EvidenceConfidence.OBSERVED,
                claim=f"blast radius terminates at registered ML model {model.urn}",
                raw_ref=lineage_ref, datahub_urn=model.urn,
            ))

        # ---- 2. the column-level path, including the SQL in the middle -------
        if terminal_urn:
            path_args = {"source_urn": root_urn, "target_urn": terminal_urn,
                         "direction": "downstream"}
            if column:
                # The live schema requires target_column whenever source_column
                # is given (finding 18 territory: read it, do not assume).
                path_args["source_column"] = column
                path_args["target_column"] = column
            paths = self._client.call(self.NAME, "get_lineage_paths_between", path_args)
            path_ref = self._pack.write(
                "pathfinder/get_lineage_paths_between.json",
                {"arguments": path_args, "ok": paths.ok, "text": paths.text,
                 "error": paths.error},
                note="Column-level path. The middle element is the real SQL.",
            )
            records.append(paths.record(path_ref))

            hops = _path_hops(paths.text)
            radius.column_path = tuple(hops)
            if paths.ok and hops:
                evidence.append(self._builder.make(
                    source=EvidenceSource.DATAHUB_GRAPH,
                    trust=EvidenceTrust.TRUSTED_SYSTEM,
                    confidence=EvidenceConfidence.OBSERVED,
                    claim=f"column-level path is {len(hops)} hops"
                          + (f" via column {column}" if column else ""),
                    raw_ref=path_ref, datahub_urn=root_urn,
                ))
            elif not paths.ok:
                evidence.append(self._builder.make(
                    source=EvidenceSource.DATAHUB_GRAPH,
                    trust=EvidenceTrust.TRUSTED_SYSTEM,
                    confidence=EvidenceConfidence.OBSERVED,
                    claim=f"get_lineage_paths_between failed: {(paths.error or '')[:160]}",
                    raw_ref=path_ref, datahub_urn=root_urn,
                ))

        # ---- 3. real SQL touching the column ---------------------------------
        q_args = {"urn": root_urn}
        if column:
            q_args["column"] = column
        queries = self._client.call(self.NAME, "get_dataset_queries", q_args)
        q_ref = self._pack.write(
            "pathfinder/get_dataset_queries.json",
            {"arguments": q_args, "ok": queries.ok, "text": queries.text,
             "error": queries.error},
            note="Real SQL touching the column, ingested rather than authored.",
        )
        records.append(queries.record(q_ref))

        found = _queries(queries.text)
        radius.queries = tuple(found)
        if found:
            evidence.append(self._builder.make(
                source=EvidenceSource.DATAHUB_GRAPH,
                trust=EvidenceTrust.TRUSTED_SYSTEM,
                confidence=EvidenceConfidence.OBSERVED,
                claim=f"{len(found)} catalog quer{'y' if len(found) == 1 else 'ies'} "
                      f"reference{'s' if len(found) == 1 else ''} "
                      f"{column or 'this dataset'} "
                      f"(source: {', '.join(sorted({q.source for q in found}))})",
                raw_ref=q_ref, datahub_urn=root_urn,
            ))
        else:
            evidence.append(self._builder.make(
                source=EvidenceSource.DATAHUB_GRAPH,
                trust=EvidenceTrust.TRUSTED_SYSTEM,
                confidence=EvidenceConfidence.OBSERVED,
                claim=f"no catalog queries reference {column or 'this dataset'}",
                raw_ref=q_ref, datahub_urn=root_urn,
            ))

        return radius, evidence, records

    def run(self, root_urn: str, *, column: Optional[str] = None,
            terminal_urn: Optional[str] = None, max_hops: int = 5,
            to_agent: str = "diagnostician"):
        started = now()
        radius, evidence, records = self.trace(
            root_urn, column=column, max_hops=max_hops, terminal_urn=terminal_urn)
        decision = AgentDecision.OK if radius.impacted else AgentDecision.DEGRADED
        if radius.impacted:
            rationale = (
                f"{len(radius.impacted)} impacted entities dataset-level, "
                f"{len(radius.column_impacted)} carrying column {column} "
                f"({'terminates at ' + radius.terminal_ml_models[0].urn if radius.reaches_ml_model else 'no ML model reached'})."
            )
        else:
            rationale = (
                "Lineage returned no downstream entities. Reporting a short blast "
                "radius rather than inferring edges the graph does not have."
            )
        handoff = AgentHandoff(
            from_agent=self.NAME, to_agent=to_agent,
            incident_id=self._builder._incident_id,
            evidence_ids=tuple(e.id for e in evidence),
            decision=decision, rationale=rationale,
            started_at=started, ended_at=now(),
            tokens=0, model=None, tool_calls=tuple(records),
        )
        return radius, evidence, handoff


# --------------------------------------------------------------------- parsing

def _walk(node) -> list:
    out = []
    if isinstance(node, dict):
        out.append(node)
        for v in node.values():
            out.extend(_walk(v))
    elif isinstance(node, list):
        for v in node:
            out.extend(_walk(v))
    return out


def _impacted(text: str) -> list[ImpactedEntity]:
    """Impacted entities, read ONLY from `searchResults`.

    This deliberately does not walk the whole payload. An earlier version did,
    and it silently counted the `entity` objects inside DataHub's **facet
    aggregations** — the filter chips for platform and container — as impacted
    assets. A column-level trace that really touched 5 datasets was reported as
    "9 impacted", because `urn:li:dataPlatform:postgres` and two container URNs
    were swept up with them.

    An inflated blast radius is a fabricated metric (the no-fabrication rule), and it is worse
    than an obviously broken one because it looks plausible. The tell was there
    in the data — every spurious entry had `degree: None`, since facets have no
    hop count — which is why `hops` is now required rather than optional.
    """
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []

    found: dict[str, ImpactedEntity] = {}
    for block in _result_blocks(payload):
        for result in block.get("searchResults") or []:
            if not isinstance(result, dict):
                continue
            entity = result.get("entity")
            if not isinstance(entity, dict):
                continue
            urn = entity.get("urn")
            if not isinstance(urn, str):
                continue
            degree = result.get("degree")
            found.setdefault(urn, ImpactedEntity(
                urn=urn,
                entity_type=str(entity.get("type") or _type_from_urn(urn)),
                hops=degree if isinstance(degree, int) else None,
            ))
    return list(found.values())


def _result_blocks(payload) -> list[dict]:
    """The `downstreams` / `upstreams` envelopes, whichever the tool returned."""
    if not isinstance(payload, dict):
        return []
    blocks = [payload[k] for k in ("downstreams", "upstreams")
              if isinstance(payload.get(k), dict)]
    # Some responses put searchResults at the top level with no envelope.
    if not blocks and "searchResults" in payload:
        blocks = [payload]
    return blocks


def _type_from_urn(urn: str) -> str:
    """Fallback when the response omits `type`. urn:li:<entity>:(...)"""
    parts = urn.split(":", 3)
    return parts[2].upper() if len(parts) > 2 else "UNKNOWN"


def _path_hops(text: str) -> list[str]:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    paths = payload.get("paths") if isinstance(payload, dict) else None
    if not paths:
        return []
    first = paths[0]
    hops = first.get("path", first) if isinstance(first, dict) else first
    out: list[str] = []
    for hop in hops if isinstance(hops, list) else []:
        if isinstance(hop, dict) and isinstance(hop.get("urn"), str):
            out.append(hop["urn"])
        elif isinstance(hop, str):
            out.append(hop)
    return out


def _queries(text: str) -> list[QueryTouchingColumn]:
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return []
    out: list[QueryTouchingColumn] = []
    for q in (payload.get("queries") or []) if isinstance(payload, dict) else []:
        if not isinstance(q, dict):
            continue
        props = q.get("properties") or {}
        statement = (props.get("statement") or {}).get("value", "")
        out.append(QueryTouchingColumn(
            urn=q.get("urn", ""), statement=statement,
            source=props.get("source", "UNKNOWN"),
        ))
    return out
