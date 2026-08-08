#!/usr/bin/env python
"""
verify_datahub_capabilities.py — ask a live DataHub what it can actually do.

Why this exists
---------------
"DataHub supports lineage" is a claim about the product. "*This* DataHub, at
this version, with this data, answered this query with this payload" is a fact
about a deployment — and it is the only kind of statement this repository is
allowed to put in a capability matrix.

So this does not read documentation. For every capability it asks the running
server two separate questions, in this order:

1. **Does the API exist here?** Answered by GraphQL introspection against the
   live schema, not by a version check. A field that is absent from the
   introspected schema is absent from this build, full stop.
2. **Did it return anything?** Answered by executing a real query and looking at
   the payload.

Those two questions have four distinct answers, and collapsing any of them into
"supported" would be the exact dishonesty this file exists to prevent:

    VERIFIED        API present, query ran, instance returned data for it.
    PRESENT_NO_DATA API present, query ran clean, this instance holds no such
                    data. A true statement about the catalog, not a defect.
    ABSENT          The field is not in this server's schema. Not implemented
                    in this build.
    ERROR           The query failed. Recorded verbatim, never smoothed over.

Only VERIFIED rows may be described as working. `PRESENT_NO_DATA` is reported as
what it is: an available surface with an empty catalog behind it.

Usage
-----
    DATAHUB_GMS_URL=http://localhost:8080 DATAHUB_TOKEN_FILE=<path> \\
      python scripts/verify_datahub_capabilities.py --out evidence/datahub-live

Writes `capability-matrix.json` (machine-readable, every raw response) and
`CAPABILITY_MATRIX.md` (the table humans read) into `--out`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.v2.datahub_preflight import resolve_gms_url, resolve_token

#: How long any single probe may take. Search-across-lineage on a cold
#: OpenSearch is genuinely slow; a short timeout here would report ERROR for
#: something that works.
TIMEOUT_S = 90

VERIFIED = "VERIFIED"
PRESENT_NO_DATA = "PRESENT_NO_DATA"
ABSENT = "ABSENT"
ERROR = "ERROR"


# --------------------------------------------------------------------- transport


class GraphQL:
    """A GraphQL caller that records rather than raises.

    A probe suite whose transport throws stops at the first unsupported
    capability, which is the one thing it must never do — the *shape* of what is
    missing is the output.
    """

    def __init__(self, url: str, token: Optional[str]) -> None:
        self.url = url
        self.token = token
        self.calls = 0

    def __call__(self, query: str, variables: Optional[dict] = None) -> dict:
        self.calls += 1
        body = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            return {"httpError": exc.code,
                    "body": exc.read().decode("utf-8", "replace")[:2000]}
        except Exception as exc:  # noqa: BLE001 — a probe must not raise
            return {"transportError": f"{type(exc).__name__}: {exc}"}


def rest_get(url: str, token: Optional[str]) -> tuple[int, str]:
    req = urllib.request.Request(url, method="GET")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return resp.status, resp.read(8192).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(2048).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


# ------------------------------------------------------------------ introspection


class Schema:
    """The live GraphQL schema, used to tell 'absent' apart from 'empty'."""

    def __init__(self, gql: GraphQL) -> None:
        self.raw = gql(
            """
            query { __schema { types { name kind
                fields { name }
                inputFields { name }
                enumValues { name } } } }
            """
        )
        self.types: dict[str, dict] = {}
        for entry in (((self.raw.get("data") or {}).get("__schema") or {})
                      .get("types") or []):
            self.types[entry["name"]] = entry

    @property
    def ok(self) -> bool:
        return bool(self.types)

    def has_type(self, name: str) -> bool:
        return name in self.types

    def has_field(self, type_name: str, field_name: str) -> bool:
        entry = self.types.get(type_name)
        if not entry:
            return False
        for key in ("fields", "inputFields"):
            for f in entry.get(key) or []:
                if f["name"] == field_name:
                    return True
        return False

    def has_enum_value(self, type_name: str, value: str) -> bool:
        entry = self.types.get(type_name)
        if not entry:
            return False
        return any(v["name"] == value for v in entry.get("enumValues") or [])


# ------------------------------------------------------------------------ probes


@dataclass
class Probe:
    """One capability, one question, one recorded answer."""

    capability: str
    #: What surface this exercises, for the matrix's second column.
    surface: str
    #: `(type, field)` pairs that must all exist in the live schema. Empty means
    #: the probe is not a GraphQL-schema question (REST, or an aggregate).
    requires: tuple[tuple[str, str], ...] = ()
    query: str = ""
    variables: dict = field(default_factory=dict)
    #: Given the response payload, return (has_data, evidence_string).
    extract: Optional[Callable[[dict], tuple[bool, str]]] = None
    note: str = ""


@dataclass
class Result:
    capability: str
    surface: str
    status: str
    evidence: str
    note: str = ""
    raw: Any = None
    missing_fields: tuple[str, ...] = ()
    #: Which URN actually answered. DataHub models a physical table and the dbt
    #: node describing it as **siblings**, and the two carry different aspects —
    #: profiling lands on the warehouse entity, ownership and assertions on the
    #: dbt one. The UI merges them; GraphQL does not. Recording which sibling
    #: answered is the difference between "this catalog has ownership" and a
    #: reader assuming it hangs off the URN they happened to look at.
    answered_on: str = ""


def run_probe(probe: Probe, gql: GraphQL, schema: Schema,
              alternates: tuple[str, ...] = ()) -> Result:
    """Run one probe, falling back across related entities before reporting empty.

    `alternates` is every other dataset URN worth asking: the probed entity's
    DataHub sibling, plus any other dataset the caller named. This is not
    shopping for a flattering answer — the question a capability matrix asks is
    *"does this catalog hold X"*, and no single dataset in a realistic catalog
    holds all of it. Profiling lands on warehouse entities, assertions on dbt
    ones, incidents and structured properties on whatever an agent last wrote
    to. Every fallback records the URN that answered, so a reader can check.
    """
    missing = tuple(
        f"{t}.{f}" for t, f in probe.requires if not schema.has_field(t, f)
    )
    if missing:
        return Result(probe.capability, probe.surface, ABSENT,
                      f"not in this server's GraphQL schema: {', '.join(missing)}",
                      probe.note, missing_fields=missing)

    result = _execute(probe, gql, probe.variables)
    primary = probe.variables.get("urn")
    if result.status != PRESENT_NO_DATA or not primary:
        return result

    for candidate in alternates:
        if candidate == primary:
            continue
        retry = _execute(probe, gql, {**probe.variables, "urn": candidate})
        if retry.status == VERIFIED:
            retry.answered_on = candidate
            retry.note = (probe.note + " " if probe.note else "") + (
                f"Answered on {candidate}, not the URN probed first.")
            return retry
    return result


def _execute(probe: Probe, gql: GraphQL, variables: dict) -> Result:
    payload = gql(probe.query, variables)
    if payload.get("httpError") or payload.get("transportError"):
        detail = payload.get("transportError") or f"HTTP {payload['httpError']}"
        return Result(probe.capability, probe.surface, ERROR, detail, probe.note, payload)
    if payload.get("errors"):
        first = payload["errors"][0]
        message = first.get("message", json.dumps(first))[:300]
        return Result(probe.capability, probe.surface, ERROR,
                      f"GraphQL error: {message}", probe.note, payload)

    try:
        has_data, evidence = (probe.extract or _default_extract)(payload)
    except Exception as exc:  # noqa: BLE001 — a bad extractor must not hide a good probe
        return Result(probe.capability, probe.surface, ERROR,
                      f"extractor failed: {type(exc).__name__}: {exc}",
                      probe.note, payload)

    return Result(probe.capability, probe.surface,
                  VERIFIED if has_data else PRESENT_NO_DATA,
                  evidence, probe.note, payload)


def _default_extract(payload: dict) -> tuple[bool, str]:
    data = payload.get("data") or {}
    return bool(data and any(v is not None for v in data.values())), json.dumps(data)[:200]


# ------------------------------------------------------------- probe definitions


def _documentation(payload: dict, path) -> tuple[bool, str]:
    """Documentation lives in three places; report which of them answered."""
    memory = _count(path(payload, "dataset", "institutionalMemory", "elements"))
    dataset_doc = (path(payload, "dataset", "editableProperties") or {}).get("description")
    fields = [
        f for f in (path(payload, "dataset", "editableSchemaMetadata",
                         "editableSchemaFieldInfo") or [])
        if (f or {}).get("description")
    ]
    parts = []
    if fields:
        parts.append(f"{len(fields)} documented column(s): "
                     + ", ".join(f["fieldPath"] for f in fields[:3]))
    if dataset_doc:
        parts.append("dataset-level description present")
    if memory:
        parts.append(f"{memory} institutional-memory link(s)")
    return bool(parts), "; ".join(parts) or "no documentation on this entity"


def _count(node: Any) -> int:
    if isinstance(node, list):
        return len(node)
    if isinstance(node, dict):
        return int(node.get("total") or node.get("count") or len(node))
    return 0


def build_probes(dataset_urn: str, ml_model_urn: str) -> list[Probe]:
    """The capability list, each one bound to a query that either answers or does not."""
    ds = {"urn": dataset_urn}

    def path(payload: dict, *keys: str) -> Any:
        node: Any = payload.get("data") or {}
        for key in keys:
            if node is None:
                return None
            node = node.get(key) if isinstance(node, dict) else None
        return node

    return [
        Probe(
            capability="Dataset",
            surface="GraphQL `dataset(urn:)` → name, platform, properties",
            requires=(("Query", "dataset"), ("Dataset", "properties")),
            query="""query D($urn:String!){ dataset(urn:$urn){ urn name platform{name}
                     properties{ name description qualifiedName customProperties{key value} } } }""",
            variables=ds,
            extract=lambda p: (
                bool(path(p, "dataset", "urn")),
                f"resolved {path(p, 'dataset', 'urn')}",
            ),
        ),
        Probe(
            capability="Schema",
            surface="GraphQL `Dataset.schemaMetadata` → fields, native types",
            requires=(("Dataset", "schemaMetadata"),),
            query="""query S($urn:String!){ dataset(urn:$urn){ schemaMetadata{ name
                     fields{ fieldPath type nativeDataType nullable description } } } }""",
            variables=ds,
            extract=lambda p: (
                bool(path(p, "dataset", "schemaMetadata", "fields")),
                f"{_count(path(p, 'dataset', 'schemaMetadata', 'fields'))} schema fields",
            ),
        ),
        Probe(
            capability="Lineage (table-level)",
            surface="GraphQL `searchAcrossLineage` DOWNSTREAM",
            requires=(("Query", "searchAcrossLineage"),),
            query="""query L($urn:String!){ searchAcrossLineage(input:{urn:$urn,
                     direction:DOWNSTREAM, query:"*", start:0, count:50}){
                     total searchResults{ degree entity{ urn type } } } }""",
            variables=ds,
            extract=lambda p: (
                _count(path(p, "searchAcrossLineage", "searchResults")) > 0,
                f"{path(p, 'searchAcrossLineage', 'total')} downstream results",
            ),
        ),
        Probe(
            capability="Column Lineage",
            surface="GraphQL `Dataset.lineage` fine-grained / `schemaFieldEntity`",
            requires=(("Query", "searchAcrossLineage"),),
            query="""query CL($urn:String!){ searchAcrossLineage(input:{urn:$urn,
                     direction:DOWNSTREAM, query:"*", start:0, count:50}){
                     searchResults{ degree paths{ path{ urn type } }
                     entity{ urn type } } } }""",
            variables=ds,
            extract=lambda p: (
                any(r.get("paths") for r in
                    (path(p, "searchAcrossLineage", "searchResults") or [])),
                "fine-grained paths present"
                if any(r.get("paths") for r in
                       (path(p, "searchAcrossLineage", "searchResults") or []))
                else "no column-level paths returned",
            ),
            note="Column lineage travels as `paths` on lineage search results.",
        ),
        Probe(
            capability="Upstream Assets",
            surface="GraphQL `searchAcrossLineage` UPSTREAM",
            requires=(("Query", "searchAcrossLineage"),),
            query="""query U($urn:String!){ searchAcrossLineage(input:{urn:$urn,
                     direction:UPSTREAM, query:"*", start:0, count:50}){
                     total searchResults{ degree entity{ urn type } } } }""",
            variables=ds,
            extract=lambda p: (
                _count(path(p, "searchAcrossLineage", "searchResults")) > 0,
                f"{path(p, 'searchAcrossLineage', 'total')} upstream results",
            ),
        ),
        Probe(
            capability="Downstream Assets",
            surface="GraphQL `Dataset.lineage(direction: DOWNSTREAM)`",
            requires=(("Dataset", "lineage"),),
            query="""query DS($urn:String!){ dataset(urn:$urn){ lineage(input:{
                     direction:DOWNSTREAM, start:0, count:100}){
                     total relationships{ type entity{ urn type } } } } }""",
            variables=ds,
            extract=lambda p: (
                _count(path(p, "dataset", "lineage", "relationships")) > 0,
                f"{path(p, 'dataset', 'lineage', 'total')} direct downstream edges",
            ),
            note="`Dataset.downstream` does not exist on this build; the "
                 "one-hop neighbour list is `Dataset.lineage(input:)`.",
        ),
        Probe(
            capability="Column Lineage (fine-grained aspect)",
            surface="GraphQL `Dataset.fineGrainedLineages`",
            requires=(("Dataset", "fineGrainedLineages"),),
            query="""query FGL($urn:String!){ dataset(urn:$urn){ fineGrainedLineages{
                     upstreams{ urn } downstreams{ urn } } } }""",
            variables=ds,
            extract=lambda p: (
                _count(path(p, "dataset", "fineGrainedLineages")) > 0,
                f"{_count(path(p, 'dataset', 'fineGrainedLineages'))} fine-grained "
                "column mappings on the entity itself",
            ),
            note="Read straight off the aspect DataHub's dbt source wrote from "
                 "the manifest — no traversal involved.",
        ),
        Probe(
            capability="Entity Health",
            surface="GraphQL `Dataset.health` (assertion-derived status)",
            requires=(("Dataset", "health"),),
            query="""query H($urn:String!){ dataset(urn:$urn){
                     health{ type status message } } }""",
            variables=ds,
            extract=lambda p: (
                _count(path(p, "dataset", "health")) > 0,
                "health: " + ", ".join(
                    f"{h.get('type')}={h.get('status')}"
                    for h in (path(p, "dataset", "health") or [])) or "no health signals",
            ),
            note="DataHub derives this from assertion results, which is why it "
                 "is empty until dbt test outcomes are ingested.",
        ),
        Probe(
            capability="Relationships",
            surface="GraphQL `Dataset.relationships(input:)` typed edges",
            requires=(("Dataset", "relationships"),),
            query="""query R($urn:String!){ dataset(urn:$urn){ relationships(input:{
                     types:["DownstreamOf","Consumes","Produces","TrainedBy"],
                     direction:OUTGOING, start:0, count:50}){
                     total relationships{ type entity{ urn type } } } } }""",
            variables=ds,
            extract=lambda p: (
                _count(path(p, "dataset", "relationships", "relationships")) > 0,
                f"{path(p, 'dataset', 'relationships', 'total')} typed edges",
            ),
        ),
        Probe(
            capability="Graph Traversal (paths)",
            surface="GraphQL `searchAcrossLineage` degree>1 hop walk",
            requires=(("Query", "searchAcrossLineage"),),
            query="""query GT($urn:String!){ searchAcrossLineage(input:{urn:$urn,
                     direction:DOWNSTREAM, query:"*", start:0, count:100}){
                     searchResults{ degree entity{ urn type } } } }""",
            variables=ds,
            extract=lambda p: (
                any((r.get("degree") or 0) > 1 for r in
                    (path(p, "searchAcrossLineage", "searchResults") or [])),
                "multi-hop degrees observed: " + ",".join(sorted({
                    str(r.get("degree")) for r in
                    (path(p, "searchAcrossLineage", "searchResults") or [])})),
            ),
        ),
        Probe(
            capability="Ownership",
            surface="GraphQL `Dataset.ownership` → owners + ownership type",
            requires=(("Dataset", "ownership"),),
            query="""query O($urn:String!){ dataset(urn:$urn){ ownership{ owners{
                     owner{ ... on CorpUser{ urn username } ... on CorpGroup{ urn name } }
                     ownershipType{ urn info{ name } } } } } }""",
            variables=ds,
            extract=lambda p: (
                _count(path(p, "dataset", "ownership", "owners")) > 0,
                f"{_count(path(p, 'dataset', 'ownership', 'owners'))} owners",
            ),
        ),
        Probe(
            capability="Domains",
            surface="GraphQL `listDomains` + `Dataset.domain`",
            requires=(("Query", "listDomains"),),
            query="""query DM{ listDomains(input:{start:0,count:50}){ total
                     domains{ urn properties{ name description } } } }""",
            extract=lambda p: (
                _count(path(p, "listDomains", "domains")) > 0,
                f"{path(p, 'listDomains', 'total')} domains",
            ),
        ),
        Probe(
            capability="Glossary",
            surface="GraphQL `getRootGlossaryNodes` + `getRootGlossaryTerms`",
            requires=(("Query", "getRootGlossaryTerms"), ("Query", "getRootGlossaryNodes")),
            query="""query G{
                     getRootGlossaryNodes(input:{start:0,count:50}){ total
                       nodes{ urn properties{ name } } }
                     getRootGlossaryTerms(input:{start:0,count:50}){ total
                       terms{ urn properties{ name description } } } }""",
            extract=lambda p: (
                (_count(path(p, "getRootGlossaryNodes", "nodes"))
                 + _count(path(p, "getRootGlossaryTerms", "terms"))) > 0,
                f"{path(p, 'getRootGlossaryNodes', 'total')} root nodes, "
                f"{path(p, 'getRootGlossaryTerms', 'total')} root terms",
            ),
            note="Terms defined under a node are not root terms — querying only "
                 "`getRootGlossaryTerms` reports an empty glossary on a catalog "
                 "that has one.",
        ),
        Probe(
            capability="Tags",
            surface="GraphQL `Dataset.tags` + `searchAcrossEntities(types:[TAG])`",
            requires=(("Dataset", "tags"),),
            query="""query T{ searchAcrossEntities(input:{types:[TAG], query:"*",
                     start:0, count:50}){ total searchResults{ entity{ urn
                     ... on Tag{ properties{ name } } } } } }""",
            extract=lambda p: (
                _count(path(p, "searchAcrossEntities", "searchResults")) > 0,
                f"{path(p, 'searchAcrossEntities', 'total')} tag entities",
            ),
        ),
        Probe(
            capability="Browse Paths",
            surface="GraphQL `browseV2`",
            requires=(("Query", "browseV2"),),
            query="""query B{ browseV2(input:{type:DATASET, path:[], query:"*",
                     start:0, count:20}){ total groups{ name count } } }""",
            extract=lambda p: (
                _count(path(p, "browseV2", "groups")) > 0,
                f"{path(p, 'browseV2', 'total')} browse groups at root",
            ),
        ),
        Probe(
            capability="Assertions",
            surface="GraphQL `Dataset.assertions`",
            requires=(("Dataset", "assertions"),),
            query="""query A($urn:String!){ dataset(urn:$urn){ assertions(start:0,count:50){
                     total assertions{ urn info{ type description } } } } }""",
            variables=ds,
            extract=lambda p: (
                _count(path(p, "dataset", "assertions", "assertions")) > 0,
                f"{path(p, 'dataset', 'assertions', 'total')} assertions on this dataset",
            ),
        ),
        Probe(
            capability="Policies",
            surface="GraphQL `listPolicies`",
            requires=(("Query", "listPolicies"),),
            query="""query P{ listPolicies(input:{start:0,count:100}){ total
                     policies{ urn type state name privileges
                     actors{ allUsers allGroups resourceOwners } } } }""",
            extract=lambda p: (
                _count(path(p, "listPolicies", "policies")) > 0,
                f"{path(p, 'listPolicies', 'total')} policies",
            ),
        ),
        Probe(
            capability="Governance (privileges)",
            surface="GraphQL `me.platformPrivileges`",
            requires=(("Query", "me"),),
            query="""query M{ me{ corpUser{ urn username } platformPrivileges{
                     managePolicies manageIngestion manageDomains manageGlossaries
                     manageTags viewAnalytics } } }""",
            extract=lambda p: (
                bool(path(p, "me", "corpUser", "urn")),
                f"authenticated as {path(p, 'me', 'corpUser', 'urn')}",
            ),
        ),
        Probe(
            capability="Dataset Profiles",
            surface="GraphQL `Dataset.datasetProfiles` (row/column stats)",
            requires=(("Dataset", "datasetProfiles"),),
            query="""query DP($urn:String!){ dataset(urn:$urn){ datasetProfiles(limit:5){
                     timestampMillis rowCount columnCount
                     fieldProfiles{ fieldPath uniqueCount nullCount min max } } } }""",
            variables=ds,
            extract=lambda p: (
                _count(path(p, "dataset", "datasetProfiles")) > 0,
                f"{_count(path(p, 'dataset', 'datasetProfiles'))} profile snapshots",
            ),
        ),
        Probe(
            capability="Freshness / Operations",
            surface="GraphQL `Dataset.operations` (last updated)",
            requires=(("Dataset", "operations"),),
            query="""query F($urn:String!){ dataset(urn:$urn){ operations(limit:5){
                     timestampMillis operationType lastUpdatedTimestamp } } }""",
            variables=ds,
            extract=lambda p: (
                _count(path(p, "dataset", "operations")) > 0,
                f"{_count(path(p, 'dataset', 'operations'))} operation records",
            ),
            note="The API is present and answers. Operations are emitted by "
                 "connectors that can read a warehouse's own query/DML history "
                 "(Snowflake, BigQuery, Redshift); DataHub's Postgres source "
                 "does not, so an empty result here is a fact about the "
                 "connector, not about DataHub.",
        ),
        Probe(
            capability="Usage Statistics",
            surface="GraphQL `Dataset.usageStats`",
            requires=(("Dataset", "usageStats"),),
            query="""query US($urn:String!){ dataset(urn:$urn){
                     usageStats(resource:$urn, range:MONTH){
                     buckets{ bucket metrics{ uniqueUserCount totalSqlQueries } } } } }""",
            variables=ds,
            extract=lambda p: (
                _count(path(p, "dataset", "usageStats", "buckets")) > 0,
                f"{_count(path(p, 'dataset', 'usageStats', 'buckets'))} usage buckets",
            ),
            note="Same shape of gap as Freshness: usage requires a source that "
                 "can read query history. Nothing in this substrate produces "
                 "one, and inventing usage numbers to fill the panel is exactly "
                 "what this repository refuses to do.",
        ),
        Probe(
            capability="ML Models",
            surface="GraphQL `searchAcrossEntities(types:[MLMODEL])`",
            requires=(("Query", "searchAcrossEntities"),),
            query="""query MM{ searchAcrossEntities(input:{types:[MLMODEL], query:"*",
                     start:0, count:50}){ total searchResults{ entity{ urn
                     ... on MLModel{ name properties{ description } } } } } }""",
            extract=lambda p: (
                _count(path(p, "searchAcrossEntities", "searchResults")) > 0,
                f"{path(p, 'searchAcrossEntities', 'total')} ML models",
            ),
        ),
        Probe(
            capability="ML Metadata",
            surface="GraphQL `mlModel(urn:)` → training data, metrics, hyper-params",
            requires=(("Query", "mlModel"),),
            query="""query MLM($urn:String!){ mlModel(urn:$urn){ urn name
                     properties{ description trainingMetrics{ name value }
                     hyperParams{ name value } externalUrl } } }""",
            variables={"urn": ml_model_urn},
            extract=lambda p: (
                bool(path(p, "mlModel", "urn")),
                f"resolved {path(p, 'mlModel', 'urn')}",
            ),
        ),
        Probe(
            capability="Search",
            surface="GraphQL `searchAcrossEntities` free text + facets",
            requires=(("Query", "searchAcrossEntities"),),
            query="""query SE{ searchAcrossEntities(input:{query:"*", start:0, count:20}){
                     total searchResults{ entity{ urn type } }
                     facets{ field aggregations{ value count } } } }""",
            extract=lambda p: (
                _count(path(p, "searchAcrossEntities", "searchResults")) > 0,
                f"{path(p, 'searchAcrossEntities', 'total')} entities indexed",
            ),
        ),
        Probe(
            capability="Business Metadata (structured properties)",
            surface="GraphQL `Dataset.structuredProperties`",
            requires=(("Dataset", "structuredProperties"),),
            query="""query SP($urn:String!){ dataset(urn:$urn){ structuredProperties{
                     properties{ structuredProperty{ urn }
                     values{ ... on StringValue{ stringValue } } } } } }""",
            variables=ds,
            extract=lambda p: (
                _count(path(p, "dataset", "structuredProperties", "properties")) > 0,
                f"{_count(path(p, 'dataset', 'structuredProperties', 'properties'))} "
                "structured properties",
            ),
        ),
        Probe(
            capability="Business Metadata (documentation)",
            surface="GraphQL `Dataset.editableSchemaMetadata` + `institutionalMemory`",
            requires=(("Dataset", "editableSchemaMetadata"),),
            query="""query IM($urn:String!){ dataset(urn:$urn){
                     institutionalMemory{ elements{ url description } }
                     editableProperties{ description }
                     editableSchemaMetadata{ editableSchemaFieldInfo{
                       fieldPath description globalTags{ tags{ tag{ urn } } } } } } }""",
            variables=ds,
            extract=lambda p: _documentation(p, path),
            note="Includes column-level descriptions. Dataset-level docs and "
                 "field-level docs are different aspects, and DevGuard writes "
                 "the field-level one — a probe that checked only "
                 "`editableProperties.description` reported an undocumented "
                 "catalog while column documentation was plainly present.",
        ),
        Probe(
            capability="Incidents",
            surface="GraphQL `Dataset.incidents` (raiseIncident / updateIncidentStatus)",
            requires=(("Dataset", "incidents"),),
            query="""query IN($urn:String!){ dataset(urn:$urn){ incidents(start:0,count:20){
                     total incidents{ urn title status{ state message } } } } }""",
            variables=ds,
            extract=lambda p: (
                _count(path(p, "dataset", "incidents", "incidents")) > 0,
                f"{path(p, 'dataset', 'incidents', 'total')} incidents on this dataset",
            ),
        ),
    ]


# ------------------------------------------------------------------------ output


def markdown(results: list[Result], meta: dict) -> str:
    icon = {VERIFIED: "✅", PRESENT_NO_DATA: "🟡", ABSENT: "⬜", ERROR: "❌"}
    lines = [
        "# DataHub capability matrix — verified against a live instance",
        "",
        "Generated by [`scripts/verify_datahub_capabilities.py`]"
        "(../../scripts/verify_datahub_capabilities.py). Every row is the recorded",
        "outcome of a query executed against the running server named below. No row",
        "is derived from documentation.",
        "",
        f"| | |",
        f"|---|---|",
        f"| **DataHub version** | `{meta.get('server_version') or 'unknown'}` |",
        f"| **GMS** | `{meta['gms_url']}` |",
        f"| **GraphQL** | `{meta['graphql_url']}` |",
        f"| **Probe dataset** | `{meta['dataset_urn']}` |",
        f"| **Fallbacks probed** | {len(meta.get('datasets_probed') or []) - 1} "
        "related datasets (siblings + explicitly named) |",
        f"| **Probe ML model** | `{meta['ml_model_urn']}` |",
        f"| **Captured** | {meta['captured_at']} |",
        f"| **Probes** | {len(results)} |",
        "",
        "## Legend",
        "",
        "| Status | Meaning |",
        "|---|---|",
        f"| {icon[VERIFIED]} `VERIFIED` | API present **and** this instance returned data. |",
        f"| {icon[PRESENT_NO_DATA]} `PRESENT_NO_DATA` | API present and answered cleanly; the catalog holds no such data. |",
        f"| {icon[ABSENT]} `ABSENT` | Field is not in this server's GraphQL schema. |",
        f"| {icon[ERROR]} `ERROR` | The query failed. Message recorded verbatim. |",
        "",
        "## Matrix",
        "",
        "| Capability | Status | Surface exercised | Observed |",
        "|---|---|---|---|",
    ]
    for r in results:
        observed = r.evidence + (" *(on the sibling entity)*" if r.answered_on else "")
        lines.append(
            f"| **{r.capability}** | {icon[r.status]} `{r.status}` | {r.surface} "
            f"| {observed} |"
        )

    tally = {s: sum(1 for r in results if r.status == s)
             for s in (VERIFIED, PRESENT_NO_DATA, ABSENT, ERROR)}
    lines += [
        "",
        f"**Tally** — {tally[VERIFIED]} verified · {tally[PRESENT_NO_DATA]} present but "
        f"empty · {tally[ABSENT]} absent · {tally[ERROR]} error.",
        "",
    ]
    notes = [r for r in results if r.note]
    if notes:
        lines += ["## Notes", ""]
        lines += [f"- **{r.capability}** — {r.note}" for r in notes]
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default="evidence/datahub-live",
                    help="Directory for capability-matrix.json and CAPABILITY_MATRIX.md")
    ap.add_argument("--dataset-urn", action="append", default=None,
                    help="Dataset to probe. Repeatable: the first is the headline "
                         "subject, the rest are fallbacks for capabilities it does "
                         "not carry. Defaults to the first dataset search returns.")
    ap.add_argument("--ml-model-urn", default=os.environ.get("DEVGUARD_PROBE_MLMODEL", ""),
                    help="ML model to probe. Defaults to the first mlModel search returns.")
    args = ap.parse_args()

    gms = resolve_gms_url() or "http://localhost:8080"
    token, token_source = resolve_token()
    graphql_url = f"{gms}/api/graphql"
    gql = GraphQL(graphql_url, token)

    print(f"GMS       {gms}")
    print(f"GraphQL   {graphql_url}")
    print(f"Token     {token_source or 'none'}")

    code, body = rest_get(f"{gms}/config", None)
    server_version = None
    if code == 200:
        try:
            server_version = (json.loads(body).get("versions") or {}).get(
                "acryldata/datahub", {}).get("version")
        except Exception:  # noqa: BLE001
            pass
    print(f"/config   HTTP {code} (DataHub {server_version or 'unknown'})")

    schema = Schema(gql)
    if not schema.ok:
        print("FATAL: GraphQL introspection returned nothing — cannot tell absent "
              "from empty, so no matrix will be written.", file=sys.stderr)
        print(json.dumps(schema.raw)[:1000], file=sys.stderr)
        return 2
    print(f"schema    {len(schema.types)} GraphQL types introspected")

    # Pick probe subjects from the live catalog rather than hard-coding them, so
    # this runs against any DataHub, not only ours.
    requested = args.dataset_urn or [
        u for u in (os.environ.get("DEVGUARD_PROBE_DATASET", "") or "").split(",") if u
    ]
    dataset_urn = requested[0] if requested else ""
    if not dataset_urn:
        found = gql("""query{ searchAcrossEntities(input:{types:[DATASET],query:"*",
                      start:0,count:1}){ searchResults{ entity{ urn } } } }""")
        results = (((found.get("data") or {}).get("searchAcrossEntities") or {})
                   .get("searchResults") or [])
        dataset_urn = results[0]["entity"]["urn"] if results else ""
    ml_model_urn = args.ml_model_urn
    if not ml_model_urn:
        found = gql("""query{ searchAcrossEntities(input:{types:[MLMODEL],query:"*",
                      start:0,count:1}){ searchResults{ entity{ urn } } } }""")
        results = (((found.get("data") or {}).get("searchAcrossEntities") or {})
                   .get("searchResults") or [])
        ml_model_urn = results[0]["entity"]["urn"] if results else "urn:li:mlModel:(none,none,PROD)"

    if not dataset_urn:
        print("FATAL: no dataset in this catalog to probe against. Ingest something "
              "first — a matrix built on an empty catalog says nothing.", file=sys.stderr)
        return 2

    # Siblings, if DataHub has any. Not a nicety: aspects genuinely split across
    # the pair, and a matrix that probed only one side would report
    # `PRESENT_NO_DATA` for ownership on a catalog that plainly has ownership.
    def siblings_of(urn: str) -> list[str]:
        payload = gql("""query S($urn:String!){ dataset(urn:$urn){
                         siblings{ siblings{ urn } } } }""", {"urn": urn})
        found = (((payload.get("data") or {}).get("dataset") or {})
                 .get("siblings") or {}).get("siblings") or []
        return [e["urn"] for e in found if e and e.get("urn") != urn]

    alternates: list[str] = []
    for urn in requested or [dataset_urn]:
        for candidate in [urn, *siblings_of(urn)]:
            if candidate and candidate not in alternates:
                alternates.append(candidate)

    print(f"dataset   {dataset_urn}")
    for extra in alternates[1:]:
        print(f"  also    {extra}")
    print(f"mlModel   {ml_model_urn}")
    print()

    probes = build_probes(dataset_urn, ml_model_urn)
    results: list[Result] = []
    for probe in probes:
        result = run_probe(probe, gql, schema, tuple(alternates))
        results.append(result)
        mark = {VERIFIED: "OK  ", PRESENT_NO_DATA: "EMPTY", ABSENT: "ABSENT", ERROR: "ERR "}
        suffix = "  [sibling]" if result.answered_on else ""
        print(f"  {mark[result.status]:6} {result.capability:42} {result.evidence}{suffix}")

    meta = {
        "gms_url": gms,
        "graphql_url": graphql_url,
        "server_version": server_version,
        "dataset_urn": dataset_urn,
        "datasets_probed": alternates,
        "ml_model_urn": ml_model_urn,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "graphql_types_introspected": len(schema.types),
        "graphql_calls": gql.calls,
        "token_source": token_source,
    }

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "capability-matrix.json").write_text(json.dumps({
        "meta": meta,
        "results": [{
            "capability": r.capability, "surface": r.surface, "status": r.status,
            "evidence": r.evidence, "note": r.note,
            "answered_on": r.answered_on,
            "missing_fields": list(r.missing_fields), "raw": r.raw,
        } for r in results],
    }, indent=2, default=str), encoding="utf-8")
    (out / "CAPABILITY_MATRIX.md").write_text(markdown(results, meta), encoding="utf-8")

    tally = {s: sum(1 for r in results if r.status == s)
             for s in (VERIFIED, PRESENT_NO_DATA, ABSENT, ERROR)}
    print()
    print(f"  {tally[VERIFIED]} verified · {tally[PRESENT_NO_DATA]} present-no-data · "
          f"{tally[ABSENT]} absent · {tally[ERROR]} error")
    print(f"  wrote {out/'capability-matrix.json'} and {out/'CAPABILITY_MATRIX.md'}")

    # An ERROR is a real failure of the probe suite; PRESENT_NO_DATA and ABSENT
    # are findings, not failures.
    return 1 if tally[ERROR] else 0


if __name__ == "__main__":
    raise SystemExit(main())
