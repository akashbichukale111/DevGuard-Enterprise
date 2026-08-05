"""
Tests for the Archivist's document retrieval — the write-back rules\' "what makes it a loop".

These exist because of a defect found the only way it could be found: by
running the loop twice and noticing that the second run reported "NO PRIOR
KNOWLEDGE" while the runbook the first run had written was sitting in the
result set.

That is the worst possible failure mode for this agent. A retrieval bug that
raises is a bug; a retrieval bug that reports a confident, clean **miss** is a
system quietly claiming an organisation has no prior knowledge of an incident it
has already solved. Both fixtures below are real captured payloads.
"""

from __future__ import annotations

import json

import pytest

from backend.v2.agents.archivist import _grep_bodies, _search_hits

#: Real `search_documents` response, from evidence/proof-pack/d6-loop-pass2.
#: Note `searchResults` — NOT `documents` or `results`, which is what the
#: original parser looked for.
REAL_SEARCH = json.dumps({
    "start": 0, "count": 10, "total": 2,
    "searchResults": [
        {"entity": {
            "urn": "urn:li:document:shared-99088993-9f90-4fa9-b59c-4054251b710f",
            "subType": "Analysis",
            "platform": {"urn": "urn:li:dataPlatform:datahub", "name": "datahub"},
            "info": {"title": "DevGuard runbook — user_id drift on devguard.raw.users",
                     "source": {"sourceType": "NATIVE"}}}},
        {"entity": {
            "urn": "urn:li:document:shared-afdb89cb-2e93-4bec-b662-b248764248af",
            "subType": "Analysis",
            "info": {"title": "DevGuard runbook — user_id drift on devguard.raw.users"}}},
    ],
})

#: Real empty response, from evidence/proof-pack/d4-evidence-chain — a genuine
#: miss, which must stay a miss.
REAL_EMPTY = json.dumps({"start": 0, "count": 10, "total": 0, "searchResults": []})


class TestSearchHits:
    def test_finds_documents_in_the_real_shape(self):
        hits = _search_hits(REAL_SEARCH)
        assert len(hits) == 2

    def test_hit_count_matches_the_servers_own_total(self):
        assert len(_search_hits(REAL_SEARCH)) == json.loads(REAL_SEARCH)["total"]

    def test_carries_urn_title_and_subtype(self):
        hit = _search_hits(REAL_SEARCH)[0]
        assert hit["urn"].startswith("urn:li:document:")
        assert "user_id drift" in hit["title"]
        assert hit["sub_type"] == "Analysis"

    def test_a_genuine_miss_stays_a_miss(self):
        """Those runs really did match nothing; that must not become a hit."""
        assert _search_hits(REAL_EMPTY) == []

    def test_documents_and_results_keys_are_not_consulted(self):
        """The old parser's keys. If the server ever uses them, that's a change
        we want to notice deliberately rather than absorb silently."""
        assert _search_hits(json.dumps({"documents": [{"urn": "urn:li:document:x"}]})) == []

    @pytest.mark.parametrize("text", ["", "not json", "null", "[]", "{}"])
    def test_malformed_payloads_yield_nothing(self, text):
        assert _search_hits(text) == []
        assert _grep_bodies(text) == {}

    def test_entries_without_a_urn_are_skipped(self):
        payload = json.dumps({"searchResults": [{"entity": {"info": {"title": "x"}}}]})
        assert _search_hits(payload) == []


class TestGrepBodies:
    def test_maps_urn_to_matched_content(self):
        payload = json.dumps({"results": [
            {"urn": "urn:li:document:a",
             "matches": [{"text": "root cause: user_id renamed"}, {"text": "verified"}]},
        ]})
        bodies = _grep_bodies(payload)
        assert "user_id renamed" in bodies["urn:li:document:a"]
        assert "verified" in bodies["urn:li:document:a"]

    def test_string_content_is_accepted(self):
        payload = json.dumps({"results": [
            {"urn": "urn:li:document:a", "content": "the whole body"}]})
        assert _grep_bodies(payload)["urn:li:document:a"] == "the whole body"

    def test_documents_without_matches_are_omitted(self):
        payload = json.dumps({"results": [{"urn": "urn:li:document:a"}]})
        assert _grep_bodies(payload) == {}


class TestTwoStageContract:
    """The flow the tools actually implement (integration finding 22)."""

    def test_search_returns_no_bodies(self):
        """Stage 1 deliberately omits content, so stage 2 is not optional."""
        payload = json.loads(REAL_SEARCH)
        for result in payload["searchResults"]:
            info = result["entity"].get("info", {})
            assert "content" not in info and "body" not in info

    def test_grep_requires_urns_which_only_search_can_supply(self):
        """Pinned as documentation: grep_documents cannot be called standalone.

        An early run called it with `{"pattern": ...}` alone and got
        `urns: Missing required argument`. The dependency between the two tools
        is the thing to remember.
        """
        hits = _search_hits(REAL_SEARCH)
        urns = [h["urn"] for h in hits]
        assert len(urns) == 2 and all(u.startswith("urn:li:document:") for u in urns)
