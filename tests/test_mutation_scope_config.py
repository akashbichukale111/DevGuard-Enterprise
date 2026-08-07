"""
The write scope is configurable, and fails closed when it is misconfigured.

Onboarding a sixth asset used to be a code change and a redeploy. That made the
narrowest, most security-relevant setting in the system the one hardest to
adjust — and a control that is painful to widen correctly is a control people
widen incorrectly, usually by deleting it.

The direction of failure is the whole point of these tests. A malformed
allowlist must never silently fall back to the built-in default: a deployment
would then believe it had narrowed a scope it had not.
"""

from __future__ import annotations

import importlib

import pytest

DS = "urn:li:dataset:(urn:li:dataPlatform:postgres,devguard.raw.users,PROD)"
OTHER = "urn:li:dataset:(urn:li:dataPlatform:snowflake,other.tbl,PROD)"


def _resolve(monkeypatch, value):
    """
    Resolve the env var through the real parser.

    Deliberately NOT importlib.reload(handoff): reloading the module rebinds
    EntityNotInScopeError and every other name other modules imported by
    reference, so `except EntityNotInScopeError` elsewhere stops matching and
    thirteen unrelated tests fail. The parser is a pure function; test it as
    one, and monkeypatch the resolved binding when behaviour is what matters.
    """
    from backend.v2 import handoff

    if value is None:
        monkeypatch.delenv("DEVGUARD_MUTATION_SCOPE_URNS", raising=False)
    else:
        monkeypatch.setenv("DEVGUARD_MUTATION_SCOPE_URNS", value)
    return handoff._scope_from_env()


def _with_scope(monkeypatch, urns):
    """Bind a resolved scope without touching module identity."""
    from backend.v2 import handoff

    monkeypatch.setattr(handoff, "MUTATION_SCOPE_URNS", frozenset(urns))
    return handoff


def test_unset_uses_the_built_in_five(monkeypatch):
    """A clean clone must behave exactly as it did before this existed."""
    from backend.v2 import handoff

    assert _resolve(monkeypatch, None) is None, "unset must mean 'use the default'"
    assert len(handoff._DEFAULT_MUTATION_SCOPE_URNS) == 5


def test_a_configured_scope_replaces_the_default(monkeypatch):
    assert _resolve(monkeypatch, f"{DS} {OTHER}") == frozenset({DS, OTHER})


def test_entries_may_be_newline_separated(monkeypatch):
    """One URN per line is how anyone will actually write more than two."""
    assert _resolve(monkeypatch, f"  {DS}\n  {OTHER}\n") == frozenset({DS, OTHER})


def test_configuring_a_narrower_scope_really_narrows_it(monkeypatch):
    """The reason to make this configurable is to tighten it, not only widen it."""
    h = _with_scope(monkeypatch, {DS})
    with pytest.raises(h.EntityNotInScopeError):
        h.check_mutation_scope("add_tags", {"entity_urns": [OTHER]})
    h.check_mutation_scope("add_tags", {"entity_urns": [DS]})  # in scope, no raise


def test_a_non_dataset_urn_is_dropped_not_accepted(monkeypatch):
    """
    Fails closed. Accepting `urn:li:corpuser:x` would let an operator believe
    they had scoped a write surface the check does not police.
    """
    assert _resolve(monkeypatch, f"{DS} urn:li:corpuser:someone") == frozenset({DS})


def test_an_entirely_invalid_scope_permits_nothing(monkeypatch):
    """
    Empty means no dataset write at all — the safe direction. The dangerous
    alternative is falling back to the default and reporting success.
    """
    assert _resolve(monkeypatch, "garbage also-garbage") == frozenset()


def test_an_empty_string_permits_nothing_rather_than_defaulting(monkeypatch):
    """`DEVGUARD_MUTATION_SCOPE_URNS=` in a compose file must not mean 'default'."""
    assert _resolve(monkeypatch, "") == frozenset()


def test_a_configured_empty_scope_refuses_every_dataset_write(monkeypatch):
    h = _with_scope(monkeypatch, set())
    with pytest.raises(h.EntityNotInScopeError):
        h.check_mutation_scope("add_tags", {"entity_urns": [DS]})


def test_commas_alone_do_not_separate_entries(monkeypatch):
    """
    Regression on the bug this nearly shipped with. A dataset URN contains
    commas, so a comma-joined pair is one token — and it still starts with
    `urn:li:dataset:` and still ends with `)`, so a prefix check alone accepts
    it and stores one bogus entry matching no real asset. Requiring exactly one
    occurrence of the scheme is what rejects it.
    """
    assert _resolve(monkeypatch, f"{DS},{OTHER}") == frozenset()


def test_a_configured_scope_cannot_widen_the_other_two_axes(monkeypatch):
    """
    Scope is one axis of three. Naming a dataset URN grants nothing about which
    tools may run or which entity types may be touched.
    """
    from backend.v2 import handoff

    _resolve(monkeypatch, DS)
    assert handoff.MUTABLE_ENTITY_TYPES == frozenset({"dataset", "document"})
    assert "add_terms" not in handoff.AGENT_TOOL_ALLOWLISTS["scribe"]
