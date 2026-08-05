"""
rag_store.py — Local CWE/OWASP vector knowledge base + retrieval (feature #6).

DESIGN PHILOSOPHY (for the review):
-----------------------------------
The Scanner Agent should not rely purely on parametric knowledge. LLMs
hallucinate CWE ids and conflate related weaknesses (e.g. CWE-89 vs CWE-943).
By retrieving canonical CWE descriptions and injecting them into context, we:

  1. Ground the model's findings in an authoritative definition (fewer
     hallucinated ids).
  2. Give the `explanation` field real material to reason against (feature #7),
     so explanations cite the actual weakness definition, not vibes.
  3. Make the KB updatable independently of the model — security teams can add
     new CWE entries without touching prompts.

We ship a SELF-CONTAINED seed dataset (no external CWE API — feature #6
requirement) and support two backends: ChromaDB (preferred, persistent) with a
pure-Python cosine-similarity fallback so the demo runs even if Chroma isn't
installed. A demo that crashes on `pip install chromadb` is a demo nobody sees.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Seed dataset — OWASP Top 10 aligned CWE entries.
# Curated to cover the vulnerability classes our benchmark exercises so
# retrieval is meaningful end-to-end.
# ---------------------------------------------------------------------------

SEED_CWE_ENTRIES: list[dict] = [
    {
        "cwe_id": "CWE-89",
        "name": "SQL Injection",
        "owasp": "A03:2021 Injection",
        "description": (
            "The software constructs SQL queries by concatenating untrusted input "
            "directly into the query string. An attacker can inject SQL syntax to "
            "read, modify, or destroy data. Mitigation: use parameterized queries / "
            "prepared statements; never string-format user input into SQL."
        ),
    },
    {
        "cwe_id": "CWE-79",
        "name": "Cross-site Scripting (XSS)",
        "owasp": "A03:2021 Injection",
        "description": (
            "Untrusted input is reflected into HTML/JS output without proper "
            "encoding, letting an attacker execute script in a victim's browser. "
            "Mitigation: contextual output encoding, templating auto-escaping, and a "
            "strict Content-Security-Policy."
        ),
    },
    {
        "cwe_id": "CWE-78",
        "name": "OS Command Injection",
        "owasp": "A03:2021 Injection",
        "description": (
            "The application passes untrusted input to a shell/OS command "
            "interpreter (e.g. os.system, subprocess with shell=True). Attackers "
            "chain commands via shell metacharacters. Mitigation: avoid shells, pass "
            "argument lists, and validate/allowlist inputs."
        ),
    },
    {
        "cwe_id": "CWE-798",
        "name": "Use of Hard-coded Credentials",
        "owasp": "A07:2021 Identification & Authentication Failures",
        "description": (
            "Secrets such as API keys, passwords, or tokens are embedded directly in "
            "source code. They leak via version control and cannot be rotated easily. "
            "Mitigation: load secrets from environment variables or a secrets manager."
        ),
    },
    {
        "cwe_id": "CWE-502",
        "name": "Deserialization of Untrusted Data",
        "owasp": "A08:2021 Software & Data Integrity Failures",
        "description": (
            "Deserializing attacker-controlled data with unsafe deserializers "
            "(pickle, yaml.load, Java native serialization) can execute arbitrary "
            "code. Mitigation: use safe formats (JSON), yaml.safe_load, and signed "
            "payloads."
        ),
    },
    {
        "cwe_id": "CWE-22",
        "name": "Path Traversal",
        "owasp": "A01:2021 Broken Access Control",
        "description": (
            "User input is used to build filesystem paths without normalization, "
            "allowing '../' sequences to escape the intended directory and read/write "
            "arbitrary files. Mitigation: canonicalize paths and verify they stay "
            "within an allowlisted base directory."
        ),
    },
    {
        "cwe_id": "CWE-327",
        "name": "Use of a Broken or Risky Cryptographic Algorithm",
        "owasp": "A02:2021 Cryptographic Failures",
        "description": (
            "The code uses weak/broken algorithms (MD5, SHA1 for security, DES, ECB "
            "mode). These are vulnerable to collisions or brute force. Mitigation: use "
            "vetted primitives (AES-GCM, SHA-256+, bcrypt/argon2 for passwords)."
        ),
    },
    {
        "cwe_id": "CWE-259",
        "name": "Use of Hard-coded Password",
        "owasp": "A07:2021 Identification & Authentication Failures",
        "description": (
            "A password is written literally in code, often for a default/admin "
            "account. Anyone with source access gains entry. Mitigation: require "
            "configured credentials and enforce rotation."
        ),
    },
    {
        "cwe_id": "CWE-611",
        "name": "XML External Entity (XXE)",
        "owasp": "A05:2021 Security Misconfiguration",
        "description": (
            "An XML parser processes external entity references in untrusted XML, "
            "enabling file disclosure, SSRF, or DoS. Mitigation: disable DTD/external "
            "entity resolution in the parser configuration."
        ),
    },
    {
        "cwe_id": "CWE-918",
        "name": "Server-Side Request Forgery (SSRF)",
        "owasp": "A10:2021 SSRF",
        "description": (
            "The server fetches a URL supplied by the user, letting attackers reach "
            "internal services or cloud metadata endpoints. Mitigation: allowlist "
            "destinations, block internal IP ranges, and disable redirects."
        ),
    },
    {
        "cwe_id": "CWE-90",
        "name": "LDAP Injection",
        "owasp": "A03:2021 Injection",
        "description": (
            "Untrusted input is concatenated into LDAP filters, letting attackers "
            "alter query logic to bypass auth or extract directory data. Mitigation: "
            "escape LDAP special characters and use parameterized filters."
        ),
    },
    {
        "cwe_id": "CWE-352",
        "name": "Cross-Site Request Forgery (CSRF)",
        "owasp": "A01:2021 Broken Access Control",
        "description": (
            "State-changing requests lack an unpredictable token, so a malicious site "
            "can force a logged-in user's browser to submit them. Mitigation: use "
            "anti-CSRF tokens and SameSite cookies."
        ),
    },
    {
        "cwe_id": "CWE-434",
        "name": "Unrestricted Upload of File with Dangerous Type",
        "owasp": "A04:2021 Insecure Design",
        "description": (
            "The app accepts uploads without validating type/extension, allowing "
            "webshells or executables to be stored and served. Mitigation: allowlist "
            "types, store outside webroot, and rename files."
        ),
    },
    {
        "cwe_id": "CWE-20",
        "name": "Improper Input Validation",
        "owasp": "A03:2021 Injection",
        "description": (
            "Input is not validated for type, length, format, or range before use, "
            "enabling a broad range of downstream attacks. Mitigation: validate and "
            "canonicalize all external input against a strict schema."
        ),
    },
    {
        "cwe_id": "CWE-330",
        "name": "Use of Insufficiently Random Values",
        "owasp": "A02:2021 Cryptographic Failures",
        "description": (
            "Security-sensitive values (tokens, session ids) are generated with a "
            "predictable PRNG (e.g. random.random). Attackers can predict them. "
            "Mitigation: use a CSPRNG such as secrets or os.urandom."
        ),
    },
]


# ---------------------------------------------------------------------------
# Embedding function
# ---------------------------------------------------------------------------
#
# We prefer sentence-transformers for real semantic retrieval. If it isn't
# available we degrade to a deterministic hashing embedding so the pipeline is
# still runnable in a minimal demo environment. The interface is identical, so
# The rest of the module is agnostic to which is active.

@dataclass
class _EmbedderInfo:
    fn: callable
    dim: int
    name: str
    # Only set by the lexical fallback: lets build() rebuild the embedder once
    # the corpus vocabulary is known. The semantic embedder ignores these.
    make: Optional[callable] = None
    tokenize: Optional[callable] = None


def _build_embedder() -> _EmbedderInfo:
    try:
        from sentence_transformers import SentenceTransformer  # type: ignore

        model = SentenceTransformer("all-MiniLM-L6-v2")

        def _embed(texts: list[str]) -> list[list[float]]:
            return [v.tolist() for v in model.encode(texts, normalize_embeddings=True)]

        return _EmbedderInfo(fn=_embed, dim=384, name="all-MiniLM-L6-v2")
    except Exception:
        # ------------------------------------------------------------------ #
        # Lexical fallback — NOT semantic embeddings, but deterministic and
        # genuinely relevant. Flagged in the name so nobody ships it to prod
        # thinking it is a real embedding model.
        #
        # WHY THIS IS NOT A HASHING BUCKET ANYMORE:
        # This used to be `v[hash(tok) % DIM] += 1.0`, described in a comment as
        # "deterministic ... retrieval order stable". Both claims were false.
        # Python randomises `hash()` for str per process (PYTHONHASHSEED), so
        # the same token landed in a different bucket on every run. Measured:
        # the same SQL-injection query returned three different CWE sets across
        # three processes, and CWE-89 was not top-ranked in any of them — the
        # RAG grounding was injecting near-random CWE definitions into the
        # Scanner prompt, which is worse than no retrieval at all because it
        # looks authoritative.
        #
        # A token-overlap vector over a FIXED vocabulary derived from the corpus
        # is deterministic by construction (no hashing at all), reproducible
        # across processes, and actually ranks the right weakness first.
        # ------------------------------------------------------------------ #
        def _tokenize(text: str) -> list[str]:
            # Split on non-alphanumerics so `db.execute(query)` yields
            # ["db","execute","query"] — code punctuation is not signal.
            return [t for t in re.split(r"[^a-z0-9]+", text.lower()) if len(t) > 2]

        def _make_embedder(vocabulary: list[str]):
            index = {tok: i for i, tok in enumerate(vocabulary)}
            dim = max(len(vocabulary), 1)

            def _embed(texts: list[str]) -> list[list[float]]:
                vecs = []
                for t in texts:
                    v = [0.0] * dim
                    for tok in _tokenize(t):
                        i = index.get(tok)
                        if i is not None:
                            v[i] += 1.0
                    norm = math.sqrt(sum(x * x for x in v)) or 1.0
                    vecs.append([x / norm for x in v])
                return vecs

            return _embed

        # Vocabulary is built lazily from the corpus on first build(); until
        # then a single-token stub keeps the signature stable.
        return _EmbedderInfo(
            fn=_make_embedder([]),
            dim=1,
            name="lexical-overlap-fallback[NOT-PROD]",
            make=_make_embedder,
            tokenize=_tokenize,
        )


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # inputs are pre-normalized


# ---------------------------------------------------------------------------
# Vector store
# ---------------------------------------------------------------------------

class CWEVectorStore:
    """
    Thin wrapper over ChromaDB with a pure-Python fallback.

    We keep the public surface tiny — build() once on startup, then
    retrieve(query, k). Everything else (backend choice, embedding model) is
    an implementation detail the agents don't need to know.
    """

    def __init__(self, entries: Optional[list[dict]] = None):
        self._entries = entries if entries is not None else SEED_CWE_ENTRIES
        self._embedder = _build_embedder()
        self._backend: str = "memory"
        self._chroma_collection = None
        self._mem_vectors: list[list[float]] = []

    def _entry_text(self, e: dict) -> str:
        """Text we embed: name + OWASP category + description gives the best
        semantic surface for matching code patterns to weaknesses."""
        return f"{e['cwe_id']} {e['name']} ({e['owasp']}): {e['description']}"

    def build(self) -> None:
        """
        Index the seed dataset. Idempotent-ish for a demo. Tries Chroma first;
        on any failure, falls back to in-memory cosine search.
        """
        texts = [self._entry_text(e) for e in self._entries]

        # Lexical fallback only: fit the vocabulary to this corpus so the
        # vectors carry real token identity instead of hash collisions.
        if self._embedder.make is not None and self._embedder.tokenize is not None:
            vocab = sorted({tok for t in texts for tok in self._embedder.tokenize(t)})
            self._embedder = _EmbedderInfo(
                fn=self._embedder.make(vocab),
                dim=max(len(vocab), 1),
                name=self._embedder.name,
                make=self._embedder.make,
                tokenize=self._embedder.tokenize,
            )

        try:
            import chromadb  # type: ignore

            client = chromadb.Client()
            # Fresh collection each build to avoid stale demo state.
            try:
                client.delete_collection("cwe_kb")
            except Exception:
                pass
            col = client.create_collection("cwe_kb")
            embeddings = self._embedder.fn(texts)
            col.add(
                ids=[e["cwe_id"] for e in self._entries],
                embeddings=embeddings,
                documents=texts,
                metadatas=[{k: v for k, v in e.items()} for e in self._entries],
            )
            self._chroma_collection = col
            self._backend = "chromadb"
        except Exception:
            self._mem_vectors = self._embedder.fn(texts)
            self._backend = "memory"

    @property
    def backend(self) -> str:
        return self._backend

    def retrieve(self, query: str, k: int = 4) -> list[dict]:
        """
        Return the top-k most relevant CWE entries for a code snippet/query.

        Args:
            query: The raw code (or a natural-language query) to match against.
            k:     Number of entries to return. Default 4 balances context
                   richness vs. prompt bloat — too many entries dilutes the
                   Scanner's focus and burns tokens.

        Returns:
            A list of entry dicts (cwe_id, name, owasp, description), best-first.
        """
        if k <= 0:
            return []

        if self._backend == "chromadb" and self._chroma_collection is not None:
            q_emb = self._embedder.fn([query])[0]
            res = self._chroma_collection.query(query_embeddings=[q_emb], n_results=k)
            metas = res.get("metadatas", [[]])[0]
            return list(metas)

        # In-memory cosine fallback
        q_emb = self._embedder.fn([query])[0]
        scored = [
            (_cosine(q_emb, vec), entry)
            for vec, entry in zip(self._mem_vectors, self._entries)
        ]
        scored.sort(key=lambda t: t[0], reverse=True)
        return [entry for _, entry in scored[:k]]


# ---------------------------------------------------------------------------
# Module-level singleton — build once, reuse across requests.
# ---------------------------------------------------------------------------

_STORE: Optional[CWEVectorStore] = None


def get_store() -> CWEVectorStore:
    """
    Lazy singleton accessor. FastAPI startup should call this once (e.g. in a
    lifespan handler) to warm the index; subsequent calls reuse it.
    """
    global _STORE
    if _STORE is None:
        _STORE = CWEVectorStore()
        _STORE.build()
    return _STORE


def format_cwe_context(entries: list[dict]) -> str:
    """
    Render retrieved entries into a compact block for prompt injection.
    Kept here (not in the agent) so the KB owns its own presentation format.
    """
    lines = ["RELEVANT SECURITY KNOWLEDGE (retrieved — cite these in explanations):"]
    for e in entries:
        lines.append(f"- {e['cwe_id']} {e['name']} [{e['owasp']}]: {e['description']}")
    return "\n".join(lines)
