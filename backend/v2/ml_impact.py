"""
What the broken column actually does to the model at the end of the lineage.

The gap this closes
-------------------
The Pathfinder already discovers that blast radius terminates at an `mlModel`,
and until now that was the whole story: an evidence item reading *"blast radius
terminates at registered ML model urn:li:mlModel:(...)"*. A URN. Nothing about
the model — not its name, not its owner, not whether anyone is on the hook for
it.

That is a real operational gap, not a cosmetic one. The reason a renamed column
matters is that a churn model silently trains on a stale feature table, and the
person who needs to know is the model's owner, who is very often **not** the
dataset's owner. Routing the incident to the dataset owner alone leaves the
model owner unaware that their training data moved under them.

So this reads the model entities the Pathfinder finds and answers: *which
model, owned by whom, at what version, and is there a human to notify?*

Real API only
-------------
`get_entities` is a published `mcp-server-datahub` tool (see `mcp_contract.py`)
and `mlModel` is a first-class DataHub entity. Field names come from DataHub's
own `entity.graphql` — `MLModel` has `name`, `description`, `platform`,
`origin`, `ownership`, and `properties: MLModelProperties` carrying `version`
and `trainingMetrics`. Nothing here is invented.

Parsing is deliberately shape-tolerant, matching `cartographer._schema_fields`
and `pathfinder._impacted`: responses arrive as JSON text whose nesting varies
with the server version and with whether an entity has siblings, so this walks
for known keys rather than asserting a path. A brittle parser here fails only
against a live catalog, which is the one place this repository cannot test.

What it does not do
-------------------
It does not write to the model. No tag, no description, no owner is applied to
an `mlModel` — the mutation allowlist scopes writes to five dataset URNs and
two entity types, and widening it to reach ML entities is a governance decision
with its own blast radius, not a convenience. This reads and reports.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from backend.v2.evidence import (
    Evidence,
    EvidenceBuilder,
    EvidenceConfidence,
    EvidenceSource,
    EvidenceTrust,
)

#: Cap on models enriched in one pass. A blast radius reaching more than this
#: many models is a platform-wide event, and the count is reported so a
#: truncated read is visible rather than silently narrowing the impact.
MAX_MODELS = 20

#: URN prefixes that identify a human or a team who can be notified.
_ACTOR_PREFIXES = ("urn:li:corpuser:", "urn:li:corpGroup:")


@dataclass(frozen=True)
class MLModelImpact:
    """One downstream ML model, as the catalog describes it."""

    urn: str
    name: Optional[str] = None
    description: Optional[str] = None
    platform: Optional[str] = None
    origin: Optional[str] = None
    version: Optional[str] = None
    owners: tuple[str, ...] = field(default_factory=tuple)
    training_metrics: tuple[str, ...] = field(default_factory=tuple)
    #: False when the entity could not be read at all — the URN is known from
    #: lineage but the catalog returned nothing usable for it.
    enriched: bool = False

    @property
    def has_owner(self) -> bool:
        return bool(self.owners)

    @property
    def display(self) -> str:
        """A name a human recognises, falling back to the URN's last segment."""
        if self.name:
            return self.name
        tail = self.urn.rstrip(")").split(",")
        return tail[-2].strip() if len(tail) >= 2 else self.urn

    def summary(self) -> str:
        if not self.enriched:
            return f"ML model {self.display} is in the blast radius; the catalog returned no detail for it"
        owner = ", ".join(self.owners) if self.owners else "NO REGISTERED OWNER"
        version = f" v{self.version}" if self.version else ""
        return f"ML model {self.display}{version} is in the blast radius; owner: {owner}"


@dataclass(frozen=True)
class MLImpactReport:
    """Every model the blast radius reaches, and whether anyone owns them."""

    models: tuple[MLModelImpact, ...] = field(default_factory=tuple)
    truncated: bool = False
    queried: int = 0

    @property
    def reached(self) -> int:
        return len(self.models)

    @property
    def unowned(self) -> tuple[MLModelImpact, ...]:
        """
        Models with no registered owner.

        These are the dangerous ones. A model nobody owns is a model nobody
        will be told about, and the incident will be closed as resolved while
        it keeps training on the changed column.
        """
        return tuple(m for m in self.models if m.enriched and not m.has_owner)

    @property
    def notifiable_actors(self) -> tuple[str, ...]:
        seen: list[str] = []
        for m in self.models:
            for o in m.owners:
                if o not in seen:
                    seen.append(o)
        return tuple(seen)

    def summary(self) -> str:
        if not self.models:
            return "blast radius reaches no registered ML model"
        owned = sum(1 for m in self.models if m.has_owner)
        return (f"blast radius reaches {len(self.models)} ML model(s); "
                f"{owned} have a registered owner, {len(self.unowned)} do not")


class MLImpactReader:
    """
    Enriches the `mlModel` URNs the Pathfinder discovered.

    Never raises. A failure to read a model's detail must degrade to "URN known,
    detail unavailable" rather than losing the blast-radius finding that the
    model is affected at all — the second is the load-bearing fact.
    """

    NAME = "pathfinder"

    def __init__(self, client, pack=None) -> None:
        self._client = client
        self._pack = pack

    def read(self, model_urns: Sequence[str]) -> MLImpactReport:
        urns = [u for u in dict.fromkeys(model_urns) if u]
        if not urns:
            return MLImpactReport()

        truncated = len(urns) > MAX_MODELS
        urns = urns[:MAX_MODELS]

        result = self._client.call(self.NAME, "get_entities", {"urns": list(urns)})
        raw_text = getattr(result, "text", None) or ""
        ref = ""
        if self._pack is not None:
            ref = self._pack.write(
                "pathfinder/ml-model-impact.json",
                {"arguments": {"urns": list(urns)},
                 "ok": getattr(result, "ok", False),
                 "text": raw_text,
                 "error": getattr(result, "error", None)},
                note=("The ML models at the end of the blast radius, read from the "
                      "catalog. Read-only — no mlModel is ever written to."),
            )

        by_urn = _models_from_text(raw_text)
        models = tuple(
            by_urn.get(u, MLModelImpact(urn=u, enriched=False)) for u in urns
        )
        return MLImpactReport(models=models, truncated=truncated, queried=len(urns))

    # ------------------------------------------------------------- evidence

    def evidence(self, builder: EvidenceBuilder, report: MLImpactReport,
                 raw_ref: str) -> list[Evidence]:
        """
        One item per model, plus an explicit item for unowned models.

        The unowned case gets its own citable evidence rather than a footnote,
        because "this model has nobody to tell" is an operational finding a
        reviewer should be able to click on.
        """
        out: list[Evidence] = []
        for model in report.models:
            out.append(builder.make(
                source=EvidenceSource.DATAHUB_GRAPH,
                trust=EvidenceTrust.TRUSTED_SYSTEM,
                # A model whose detail could not be read is a weaker claim than
                # one that was, and the confidence has to say so.
                confidence=(EvidenceConfidence.OBSERVED if model.enriched
                            else EvidenceConfidence.INFERRED),
                claim=model.summary()[:200],
                raw_ref=raw_ref,
                datahub_urn=model.urn,
            ))

        if report.unowned:
            names = ", ".join(m.display for m in report.unowned[:3])
            out.append(builder.make(
                source=EvidenceSource.DATAHUB_GRAPH,
                trust=EvidenceTrust.TRUSTED_SYSTEM,
                confidence=EvidenceConfidence.DERIVED,
                claim=(f"{len(report.unowned)} downstream ML model(s) have no "
                       f"registered owner and cannot be notified: {names}")[:200],
                raw_ref=raw_ref,
                datahub_urn=report.unowned[0].urn,
            ))
        return out


# --------------------------------------------------------------------------- #
# Parsing — tolerant by design, see the module docstring
# --------------------------------------------------------------------------- #

def _walk(node: Any) -> Iterable[dict]:
    """Every dict anywhere in a decoded response."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _owners_from(node: dict) -> tuple[str, ...]:
    """Every corpuser/corpGroup URN reachable under an entity's ownership."""
    found: list[str] = []
    for sub in _walk(node.get("ownership") or {}):
        urn = sub.get("urn")
        if isinstance(urn, str) and urn.startswith(_ACTOR_PREFIXES) and urn not in found:
            found.append(urn)
    return tuple(found)


def _platform_of(node: dict) -> Optional[str]:
    platform = node.get("platform")
    if isinstance(platform, str):
        return platform
    if isinstance(platform, dict):
        for key in ("name", "urn"):
            value = platform.get(key)
            if isinstance(value, str):
                return value
    return None


def _properties_of(node: dict) -> dict:
    props = node.get("properties") or node.get("mlModelProperties") or {}
    return props if isinstance(props, dict) else {}


def _metric_names(props: dict) -> tuple[str, ...]:
    names: list[str] = []
    for metric in props.get("trainingMetrics") or []:
        if isinstance(metric, dict):
            name = metric.get("name")
            if isinstance(name, str) and name not in names:
                names.append(name)
    return tuple(names)


def _models_from_text(text: str) -> dict[str, MLModelImpact]:
    """
    Pull every `mlModel` entity out of a `get_entities` response.

    Keyed by URN so the caller can preserve the order lineage gave it, and so a
    URN the response omitted degrades to `enriched=False` rather than silently
    vanishing from the blast radius.
    """
    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {}

    out: dict[str, MLModelImpact] = {}
    for node in _walk(payload):
        urn = node.get("urn")
        if not isinstance(urn, str) or not urn.startswith("urn:li:mlModel:"):
            continue
        if urn in out:
            continue
        props = _properties_of(node)
        name = node.get("name") or props.get("name")
        description = node.get("description") or props.get("description")
        out[urn] = MLModelImpact(
            urn=urn,
            name=name if isinstance(name, str) else None,
            description=description if isinstance(description, str) else None,
            platform=_platform_of(node),
            origin=node.get("origin") if isinstance(node.get("origin"), str) else None,
            version=(props.get("version") if isinstance(props.get("version"), str) else None),
            owners=_owners_from(node),
            training_metrics=_metric_names(props),
            enriched=True,
        )
    return out
