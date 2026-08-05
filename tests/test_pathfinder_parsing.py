"""
Tests for the Pathfinder's response parsing.

The first class exists because of a real defect found on a live run:
`_impacted` walked the entire response and counted the `entity` objects inside
DataHub's **facet aggregations** — the platform and container filter chips — as
impacted assets. A trace that really touched 5 datasets was reported as "9
impacted".

That is a fabricated metric (the no-fabrication rule) produced by our own code, and it is the
dangerous kind: plausible, in the right ballpark, and invisible unless someone
compares it against `total`. The fixture below is the actual payload from
`evidence/proof-pack/d4-20260801T065912Z`, trimmed to the shapes that matter, so
the regression is pinned against something the live server really returned.
"""

from __future__ import annotations

import json

import pytest

from backend.v2.agents.pathfinder import (
    BlastRadius, ImpactedEntity, _impacted, _path_hops, _queries,
)

DS = "urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.{},PROD)"
DBT = "urn:li:dataset:(urn:li:dataPlatform:dbt,devguard.{},PROD)"

#: Trimmed from the real column-level response. The facets are what broke it.
REAL_COLUMN_LINEAGE = json.dumps({
    "downstreams": {
        "total": 5,
        "facets": [
            {"field": "platform", "aggregations": [
                {"value": "urn:li:dataPlatform:postgres", "count": 1,
                 "entity": {"urn": "urn:li:dataPlatform:postgres"}},
                {"value": "urn:li:dataPlatform:dbt", "count": 1,
                 "entity": {"urn": "urn:li:dataPlatform:dbt"}},
            ]},
            {"field": "container", "aggregations": [
                {"value": "urn:li:container:f9adf01d", "count": 1,
                 "entity": {"urn": "urn:li:container:f9adf01d"}},
                {"value": "urn:li:container:49c362e5", "count": 1,
                 "entity": {"urn": "urn:li:container:49c362e5"}},
            ]},
        ],
        "searchResults": [
            {"degree": 1, "lineageColumns": ["user_id"],
             "entity": {"urn": DBT.format("raw.users"), "type": "DATASET"}},
            {"degree": 1, "lineageColumns": ["user_id"],
             "entity": {"urn": DS.format("analytics_staging.stg_users"),
                        "type": "DATASET"}},
            {"degree": 2, "lineageColumns": ["user_id"],
             "entity": {"urn": DBT.format("analytics_staging.stg_users"),
                        "type": "DATASET"}},
            {"degree": 2, "lineageColumns": ["user_id"],
             "entity": {"urn": DBT.format("analytics_marts.user_order_features"),
                        "type": "DATASET"}},
            {"degree": 3, "lineageColumns": ["user_id"],
             "entity": {"urn": DS.format("analytics_marts.user_order_features"),
                        "type": "DATASET"}},
        ],
    }
})

MODEL_URN = "urn:li:mlModel:(urn:li:dataPlatform:devguard_ml,devguard_churn_risk,PROD)"
JOB_URN = ("urn:li:dataJob:(urn:li:dataFlow:(devguard_ml,substrate_ml,PROD),"
           "train_churn_model)")

REAL_DATASET_LINEAGE = json.dumps({
    "downstreams": {
        "total": 7,
        "facets": [{"field": "platform", "aggregations": [
            {"value": "urn:li:dataPlatform:postgres",
             "entity": {"urn": "urn:li:dataPlatform:postgres"}}]}],
        "searchResults": [
            {"degree": 1, "entity": {"urn": DS.format("analytics_staging.stg_users"),
                                     "type": "DATASET"}},
            {"degree": 3, "entity": {"urn": DS.format("analytics_marts.user_order_features"),
                                     "type": "DATASET"}},
            {"degree": 4, "entity": {"urn": JOB_URN, "type": "DATA_JOB"}},
            {"degree": 5, "entity": {"urn": MODEL_URN, "type": "MLMODEL"}},
        ],
    }
})


class TestFacetsAreNotImpactedEntities:
    """The regression. Facet chips are UI filters, not affected assets."""

    def test_counts_only_search_results(self):
        assert len(_impacted(REAL_COLUMN_LINEAGE)) == 5

    def test_matches_the_servers_own_total(self):
        payload = json.loads(REAL_COLUMN_LINEAGE)
        assert len(_impacted(REAL_COLUMN_LINEAGE)) == payload["downstreams"]["total"]

    def test_no_platform_or_container_urns_leak_in(self):
        urns = {e.urn for e in _impacted(REAL_COLUMN_LINEAGE)}
        assert not any(u.startswith("urn:li:dataPlatform:") for u in urns)
        assert not any(u.startswith("urn:li:container:") for u in urns)

    def test_every_impacted_entity_has_a_hop_count(self):
        """The tell that exposed the bug: facet entries had degree None."""
        assert all(e.hops is not None for e in _impacted(REAL_COLUMN_LINEAGE))

    def test_deduplicates_repeated_urns(self):
        payload = json.loads(REAL_COLUMN_LINEAGE)
        payload["downstreams"]["searchResults"].append(
            payload["downstreams"]["searchResults"][0])
        assert len(_impacted(json.dumps(payload))) == 5


class TestTerminus:
    def test_dataset_level_reaches_the_ml_model(self):
        radius = BlastRadius(root_urn="x", column="user_id",
                             impacted=tuple(_impacted(REAL_DATASET_LINEAGE)))
        assert radius.reaches_ml_model
        assert radius.terminal_ml_models[0].urn == MODEL_URN
        assert radius.max_hops == 5

    def test_column_level_does_not_reach_the_ml_model(self):
        """Not a bug: the mlModel edge is dataset-level (finding 14).

        Pinned as a test because the day this changes, DevGuard should notice —
        it would mean DataHub started emitting a graph edge for
        mlModelTrainingData, and the Pathfinder could stop running two traces.
        """
        radius = BlastRadius(root_urn="x", column="user_id",
                             column_impacted=tuple(_impacted(REAL_COLUMN_LINEAGE)))
        assert not radius.column_reaches_ml_model

    def test_both_traces_are_kept_separate(self):
        radius = BlastRadius(
            root_urn="x", column="user_id",
            impacted=tuple(_impacted(REAL_DATASET_LINEAGE)),
            column_impacted=tuple(_impacted(REAL_COLUMN_LINEAGE)))
        assert len(radius.impacted) == 4
        assert len(radius.column_impacted) == 5
        # Never summed — that would be a fabricated count.
        assert radius.reaches_ml_model and not radius.column_reaches_ml_model

    @pytest.mark.parametrize("urn,expected", [
        (MODEL_URN, True),
        (JOB_URN, False),
        (DS.format("raw.users"), False),
    ])
    def test_ml_model_identification(self, urn, expected):
        etype = "MLMODEL" if "mlModel" in urn else "DATASET"
        assert ImpactedEntity(urn=urn, entity_type=etype).is_ml_model is expected


class TestRobustness:
    @pytest.mark.parametrize("text", ["", "not json", "null", "[]", "{}"])
    def test_malformed_payloads_yield_nothing_rather_than_raising(self, text):
        assert _impacted(text) == []
        assert _path_hops(text) == []
        assert _queries(text) == []

    def test_upstream_envelope_is_handled(self):
        payload = json.dumps({"upstreams": {"searchResults": [
            {"degree": 1, "entity": {"urn": DS.format("raw.users"), "type": "DATASET"}}]}})
        assert len(_impacted(payload)) == 1

    def test_unenveloped_results_are_handled(self):
        payload = json.dumps({"searchResults": [
            {"degree": 1, "entity": {"urn": DS.format("raw.users"), "type": "DATASET"}}]})
        assert len(_impacted(payload)) == 1


class TestPathAndQueryParsing:
    def test_column_path_keeps_the_query_entity_in_the_middle(self):
        """The query hop is the point — it ties SQL to lineage."""
        payload = json.dumps({"paths": [{"path": [
            {"urn": "urn:li:schemaField:(x,user_id)"},
            {"urn": "urn:li:query:view_5ba31a4e"},
            {"urn": "urn:li:schemaField:(y,user_id)"},
        ]}]})
        hops = _path_hops(payload)
        assert len(hops) == 3
        assert hops[1].startswith("urn:li:query:")

    def test_queries_carry_statement_and_source(self):
        payload = json.dumps({"total": 1, "queries": [{
            "urn": "urn:li:query:view_5ba31a4e",
            "properties": {"source": "SYSTEM",
                           "statement": {"value": "SELECT user_id FROM raw.users"}},
        }]})
        found = _queries(payload)
        assert len(found) == 1
        assert found[0].source == "SYSTEM"
        assert "user_id" in found[0].statement
