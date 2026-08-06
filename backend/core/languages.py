"""
Source-language registry for the Scanner pipeline.

Why this module exists
----------------------
`ScanRequest` has always carried a `language` field, and `cache.cache_key`
has always hashed it — but nothing between the API layer and the LLM prompts
ever read it. The Scanner's system prompt named no language, the frontend
never sent the field, and `ai_agent` contained no reference to it at all. The
language picker in the UI was therefore decorative: selecting JavaScript and
selecting Python produced byte-identical prompts.

This module is the single place that says which languages DevGuard claims to
support and what each one means to a prompt. It is deliberately small and
data-only: the pipeline stays in `ai_agent`, and adding a language is a
change to `LANGUAGES` rather than a change to any agent.

Honesty note
------------
Every language listed here is threaded end to end — the id reaches the
Scanner, Fixer and Validator prompts, and the file extensions are what the
upload path uses to infer a language. Do not add an entry here that the
pipeline cannot actually process: the registry is what the UI renders, so an
unsupported entry becomes a false claim on screen.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Language:
    """One supported source language."""

    id: str
    label: str
    # Shown to the model so it applies the right idioms and the right
    # vulnerability classes (e.g. prototype pollution is a JS/TS concern,
    # deserialization gadget chains are a Java one).
    prompt_name: str
    # Lowercase, dot-prefixed. First entry is the canonical extension used
    # when naming a snippet.
    extensions: tuple[str, ...] = field(default_factory=tuple)


LANGUAGES: tuple[Language, ...] = (
    Language("python", "Python", "Python", (".py", ".pyw")),
    Language("javascript", "JavaScript", "JavaScript (Node.js/browser)", (".js", ".jsx", ".mjs", ".cjs")),
    Language("typescript", "TypeScript", "TypeScript", (".ts", ".tsx", ".mts", ".cts")),
    Language("java", "Java", "Java", (".java",)),
)

DEFAULT_LANGUAGE = "python"

_BY_ID = {lang.id: lang for lang in LANGUAGES}
_BY_EXT = {ext: lang for lang in LANGUAGES for ext in lang.extensions}

#: Every extension the scanner knows how to read, for the upload paths.
SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(_BY_EXT)


def resolve(language_id: str | None) -> Language:
    """
    Return the registry entry for `language_id`, falling back to the default.

    Unknown ids fall back rather than raise. The value arrives from a request
    body, and an unrecognised language is not a reason to fail a scan — the
    pipeline is prompt-driven and degrades to a sensible default. The API
    layer validates separately when it wants to reject.
    """
    if not language_id:
        return _BY_ID[DEFAULT_LANGUAGE]
    return _BY_ID.get(language_id.strip().lower(), _BY_ID[DEFAULT_LANGUAGE])


def is_supported(language_id: str | None) -> bool:
    """True when `language_id` names a registry entry exactly."""
    return bool(language_id) and language_id.strip().lower() in _BY_ID


def for_extension(extension: str) -> Language | None:
    """
    Map a file extension (with or without the leading dot) to a language.

    Returns None for anything unknown so callers can skip non-source files
    rather than mis-scan them as Python.
    """
    ext = extension.strip().lower()
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    return _BY_EXT.get(ext)


def prompt_name(language_id: str | None) -> str:
    """The human-readable language name to put in an LLM prompt."""
    return resolve(language_id).prompt_name
