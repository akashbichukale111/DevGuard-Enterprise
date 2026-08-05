"""
groq_client.py — Thin wrapper exposing a module-level ``groq_client`` object
with an async ``.chat.completions.create(...)`` interface, matching what
``backend/core/ai_agent.py`` expects.

WHY THIS FILE EXISTS:
ai_agent.py was written assuming an async Groq client is importable as
``from groq_client import groq_client``. The real Groq Python SDK exposes
``AsyncGroq`` — we expose it under the expected name here.

WHY THE CLIENT IS LAZY:
This module used to construct ``AsyncGroq`` at import time and ``raise`` when
``GROQ_API_KEY`` was unset. Because ai_agent.py imports this module at module
scope, that made the *entire backend package* unimportable without a live API
key — so no test could import it, no CI could typecheck it, and `import
backend.main` failed outright on a clean checkout.

The client is therefore built on first *use* rather than on import. Importing
this module is now always safe; the missing-key error surfaces at the first
actual LLM call, with the same message it always had. The public interface is
unchanged: ``groq_client.chat.completions.create(...)`` behaves exactly as
before.
"""

from __future__ import annotations

import os
import threading
from typing import Any

from groq import AsyncGroq

_MISSING_KEY_MESSAGE = (
    "GROQ_API_KEY environment variable is not set. "
    'Set it with: $env:GROQ_API_KEY="your_key_here" (PowerShell), '
    'export GROQ_API_KEY="your_key_here" (bash), '
    "or add it to your .env file (see .env.example)."
)

_client: AsyncGroq | None = None
_lock = threading.Lock()


def get_groq_client() -> AsyncGroq:
    """Return the process-wide ``AsyncGroq``, constructing it on first use.

    Raises ``RuntimeError`` with an actionable message if ``GROQ_API_KEY`` is
    not set. Prefer this over constructing ``AsyncGroq`` directly so the whole
    process shares one client and one place to swap config or mocks in tests.
    """
    global _client
    if _client is None:
        with _lock:
            # Re-check inside the lock: two coroutines can race to here.
            if _client is None:
                api_key = os.environ.get("GROQ_API_KEY")
                if not api_key:
                    raise RuntimeError(_MISSING_KEY_MESSAGE)
                _client = AsyncGroq(api_key=api_key)
    return _client


def reset_groq_client() -> None:
    """Drop the cached client. Intended for tests that swap the API key."""
    global _client
    with _lock:
        _client = None


class _LazyGroqClient:
    """Attribute-forwarding proxy so ``groq_client.chat.completions.create``
    resolves the real client at call time instead of at import time.

    Deliberately minimal: it forwards attribute access and nothing else, so it
    cannot silently diverge from the SDK's surface.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(get_groq_client(), name)

    def __repr__(self) -> str:
        state = "initialised" if _client is not None else "not yet initialised"
        return f"<LazyGroqClient ({state})>"


groq_client = _LazyGroqClient()
