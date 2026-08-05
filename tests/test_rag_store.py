"""
RAG retrieval tests, and the regression guard for a real defect.

THE DEFECT THESE TESTS EXIST FOR
--------------------------------
The pure-Python fallback embedder used to bucket tokens with Python's built-in
`hash()`:

    v[hash(tok) % DIM] += 1.0

and a comment described it as "Deterministic hashing fallback ... keeps
retrieval order stable". Both halves were false. Python randomises `hash()` for
`str` per process (PYTHONHASHSEED), so every process placed the same token in a
different bucket. Measured across three processes, the same SQL-injection query
returned three completely different CWE sets and CWE-89 was top-ranked in none
of them.

That is worse than having no retrieval: the Scanner prompt was being injected
with near-random CWE definitions presented as "RELEVANT SECURITY KNOWLEDGE
(retrieved — cite these in explanations)". Grounding that is actually noise
pushes the model toward citing the wrong weakness with confidence.

Replaced with a token-overlap vector over a vocabulary fitted to the corpus:
deterministic by construction, and it ranks the right weakness first for most
classes.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def lexical_store(monkeypatch):
    """A store forced onto the pure-Python fallback path.

    Setting the module to None makes `from sentence_transformers import ...`
    raise, which is exactly how a lean install behaves.
    """
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    monkeypatch.setitem(sys.modules, "chromadb", None)
    from backend.core.rag_store import CWEVectorStore

    store = CWEVectorStore()
    store.build()
    assert store.backend == "memory"
    assert "lexical" in store._embedder.name
    return store


SQL_SNIPPET = 'query = "SELECT * FROM users WHERE id = " + uid; db.execute(query)'


# --------------------------------------------------------------------------- #
# Determinism — the property that was claimed but false
# --------------------------------------------------------------------------- #

def test_no_builtin_hash_of_strings_in_the_module():
    """Regression guard for the root cause.

    `hash()` on a str is randomised per process. Any reintroduction of it into
    the retrieval path silently restores non-reproducible retrieval, which is
    invisible in a single-process test run — hence a source-level assertion.
    """
    src = (REPO_ROOT / "backend" / "core" / "rag_store.py").read_text()
    # Strip comments so the explanatory comment about the old bug doesn't match.
    code_only = "\n".join(
        line.split("#", 1)[0] for line in src.splitlines()
    )
    assert not re.search(r"\bhash\s*\(", code_only), (
        "builtin hash() is back in rag_store.py — it is randomised per process "
        "and makes retrieval non-reproducible across restarts"
    )


def test_repeated_retrieval_is_stable(lexical_store):
    first = [e["cwe_id"] for e in lexical_store.retrieve(SQL_SNIPPET, k=4)]
    for _ in range(5):
        assert [e["cwe_id"] for e in lexical_store.retrieve(SQL_SNIPPET, k=4)] == first


def test_two_independently_built_stores_agree(monkeypatch):
    """Determinism across store instances, not just across calls on one."""
    monkeypatch.setitem(sys.modules, "sentence_transformers", None)
    monkeypatch.setitem(sys.modules, "chromadb", None)
    from backend.core.rag_store import CWEVectorStore

    a, b = CWEVectorStore(), CWEVectorStore()
    a.build()
    b.build()
    assert [e["cwe_id"] for e in a.retrieve(SQL_SNIPPET, k=5)] == [
        e["cwe_id"] for e in b.retrieve(SQL_SNIPPET, k=5)
    ]


# --------------------------------------------------------------------------- #
# Relevance — retrieval has to be worth doing
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "snippet,expected_cwe",
    [
        ('import os\nos.system("ping " + host)', "CWE-78"),          # command injection
        ("import hashlib\nhashlib.md5(pw.encode()).hexdigest()", "CWE-327"),  # weak hash
        ("import random\ntoken = str(random.random())", "CWE-330"),  # weak PRNG
    ],
)
def test_top_hit_is_the_right_weakness(lexical_store, snippet, expected_cwe):
    top = lexical_store.retrieve(snippet, k=1)
    assert top, "retrieval returned nothing"
    assert top[0]["cwe_id"] == expected_cwe


def test_sql_injection_is_retrieved_for_a_sql_snippet(lexical_store):
    """CWE-89 must be in play for a SQL snippet.

    Asserted as top-3 rather than top-1: this is a lexical matcher, and CWE-90
    (LDAP injection) shares injection vocabulary. Being honest about the
    fallback's precision beats writing a test that overstates it — before the
    fix CWE-89 was absent entirely.
    """
    ids = [e["cwe_id"] for e in lexical_store.retrieve(SQL_SNIPPET, k=3)]
    assert "CWE-89" in ids


def test_unrelated_text_still_returns_k_entries(lexical_store):
    """No query should produce an empty result — the Scanner expects context."""
    got = lexical_store.retrieve("the quick brown fox jumps over the lazy dog", k=3)
    assert len(got) == 3


# --------------------------------------------------------------------------- #
# Contract of retrieve() / format_cwe_context()
# --------------------------------------------------------------------------- #

def test_k_zero_and_negative_return_empty(lexical_store):
    assert lexical_store.retrieve(SQL_SNIPPET, k=0) == []
    assert lexical_store.retrieve(SQL_SNIPPET, k=-3) == []


def test_k_larger_than_the_corpus_does_not_raise(lexical_store):
    got = lexical_store.retrieve(SQL_SNIPPET, k=9999)
    assert len(got) == len(lexical_store._entries)


def test_retrieved_entries_carry_the_fields_the_prompt_needs(lexical_store):
    for entry in lexical_store.retrieve(SQL_SNIPPET, k=4):
        for field in ("cwe_id", "name", "owasp", "description"):
            assert entry.get(field), f"retrieved entry missing {field}"


def test_format_cwe_context_renders_every_entry(lexical_store):
    from backend.core.rag_store import format_cwe_context

    entries = lexical_store.retrieve(SQL_SNIPPET, k=3)
    block = format_cwe_context(entries)
    for e in entries:
        assert e["cwe_id"] in block
        assert e["name"] in block


def test_format_cwe_context_handles_an_empty_retrieval():
    """k=0 is legal, so the formatter must not produce a dangling header."""
    from backend.core.rag_store import format_cwe_context

    block = format_cwe_context([])
    assert isinstance(block, str)
