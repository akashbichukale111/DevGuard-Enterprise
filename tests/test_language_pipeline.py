"""
The `language` field must reach the agent prompts.

`ScanRequest.language` existed from the beginning and `cache.cache_key` always
hashed it, but nothing in between read it: `ai_agent` contained no reference to
the word, so the Scanner, Fixer and Validator were prompted identically no
matter what the caller asked for. The picker in the UI was decorative.

These tests pin the thread end to end so it cannot silently come apart again:
normalisation at the API boundary, the registry the UI renders from, and the
language actually appearing in each of the three prompts.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core import ai_agent, languages
from backend.core.schemas import ScanRequest, Severity, Vulnerability


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #

def test_registry_covers_the_four_advertised_languages():
    assert {lang.id for lang in languages.LANGUAGES} == {
        "python",
        "javascript",
        "typescript",
        "java",
    }


def test_every_language_has_at_least_one_extension():
    # The upload path maps extension -> language. An entry with no extension
    # would be unreachable from a file upload while still rendering in the UI.
    for lang in languages.LANGUAGES:
        assert lang.extensions, f"{lang.id} advertises no file extension"


def test_extensions_are_unambiguous():
    seen: dict[str, str] = {}
    for lang in languages.LANGUAGES:
        for ext in lang.extensions:
            assert ext not in seen, f"{ext} claimed by both {seen.get(ext)} and {lang.id}"
            seen[ext] = lang.id


@pytest.mark.parametrize(
    "extension,expected",
    [
        (".py", "python"),
        ("py", "python"),
        (".TS", "typescript"),
        (".java", "java"),
        (".jsx", "javascript"),
    ],
)
def test_for_extension_maps_source_files(extension, expected):
    lang = languages.for_extension(extension)
    assert lang is not None and lang.id == expected


@pytest.mark.parametrize("extension", [".md", ".txt", ".png", "", ".exe"])
def test_for_extension_returns_none_for_non_source(extension):
    # Callers skip these rather than mis-scanning them as Python.
    assert languages.for_extension(extension) is None


# --------------------------------------------------------------------------- #
# Normalisation at the API boundary
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("python", "python"),
        ("Java", "java"),
        ("TYPESCRIPT", "typescript"),
        ("  javascript  ", "javascript"),
        ("cobol", "python"),  # unknown -> default, never a rejected scan
        ("", "python"),
    ],
)
def test_scan_request_normalises_language(raw, expected):
    assert ScanRequest(code="x = 1", language=raw).language == expected


def test_scan_request_defaults_to_python():
    assert ScanRequest(code="x = 1").language == languages.DEFAULT_LANGUAGE


def test_language_changes_the_cache_key():
    """
    Two scans of identical bytes in different languages must not collide.

    The key already included language; this asserts it, because the field only
    started carrying meaning once the prompts read it — before that a collision
    would have been harmless, and now it would serve a Java user a Python scan.
    """
    from backend.core.cache import cache_key

    py = cache_key(ScanRequest(code="same bytes", language="python"))
    java = cache_key(ScanRequest(code="same bytes", language="java"))
    assert py != java


# --------------------------------------------------------------------------- #
# The prompts
# --------------------------------------------------------------------------- #

def _capture_scanner_prompt(monkeypatch, language: str) -> str:
    captured: dict[str, str] = {}

    async def fake_llm(*, agent, model, system_prompt, user_prompt, **kwargs):
        captured["prompt"] = user_prompt
        raise RuntimeError("stop after prompt construction")

    monkeypatch.setattr(ai_agent, "_call_llm", fake_llm)
    with pytest.raises(Exception):
        asyncio.run(ai_agent.run_scanner("x = 1", language=language))
    return captured["prompt"]


@pytest.mark.parametrize(
    "language,expected_name",
    [
        ("python", "Python"),
        ("java", "Java"),
        ("typescript", "TypeScript"),
        ("javascript", "JavaScript"),
    ],
)
def test_scanner_prompt_names_the_language(monkeypatch, language, expected_name):
    prompt = _capture_scanner_prompt(monkeypatch, language)
    assert expected_name in prompt


def test_scanner_prompt_differs_between_languages(monkeypatch):
    py = _capture_scanner_prompt(monkeypatch, "python")
    java = _capture_scanner_prompt(monkeypatch, "java")
    assert py != java, "scanner prompt is identical across languages"


def _vuln() -> list[Vulnerability]:
    return [
        Vulnerability(
            cwe_id="CWE-89",
            severity=Severity.HIGH,
            line_number=1,
            explanation="A parameterised query is required here; " * 2,
            confidence_score=0.9,
        )
    ]


@pytest.mark.parametrize(
    "language,expected_name",
    [("java", "Java"), ("typescript", "TypeScript"), ("python", "Python")],
)
def test_fixer_prompt_names_the_language(language, expected_name):
    prompt = ai_agent._build_fixer_prompt("code", _vuln(), None, language=language)
    assert prompt.startswith(f"Language: {expected_name}")
    assert f"idiomatic for {expected_name}" in prompt


def test_fixer_prompt_defaults_when_language_omitted():
    # Existing callers pass no language; they must keep working.
    prompt = ai_agent._build_fixer_prompt("code", _vuln(), None)
    assert prompt.startswith("Language: Python")


def test_validator_prompt_names_the_language(monkeypatch):
    captured: dict[str, str] = {}

    async def fake_llm(*, agent, model, system_prompt, user_prompt, **kwargs):
        captured["prompt"] = user_prompt
        raise RuntimeError("stop after prompt construction")

    monkeypatch.setattr(ai_agent, "_call_llm", fake_llm)
    from backend.core.schemas import FixResult

    fix = FixResult(
        patched_code="safe",
        diff_summary="parameterised the query",
        addressed_cwe_ids=["CWE-89"],
        model_used="test",
    )
    with pytest.raises(Exception):
        asyncio.run(ai_agent.run_validator("code", _vuln(), fix, language="java"))
    assert "Language: Java" in captured["prompt"]
