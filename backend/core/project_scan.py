"""
Multi-file source collection for ZIP and repository scans.

What this module is
-------------------
The Scanner pipeline (`ai_agent.run_pipeline`) takes one string of code and
returns one `PipelineResult`. That does not change here. This module is the
step in front of it: turn a ZIP archive or a git repository into a bounded,
validated list of source files that the existing pipeline can be run over,
one file at a time.

It deliberately contains no agent logic and makes no LLM calls. Collection and
analysis stay separate so the limits below can be unit-tested without a key,
and so the pipeline keeps exactly one entry point.

Why the limits are hard limits
------------------------------
Every collected file becomes a full Scanner -> Fixer -> Validator run, which
is several paid model calls. An unbounded repository walk is therefore both a
cost incident and a denial-of-service vector. `MAX_FILES` is the ceiling, and
whatever is skipped is reported back to the caller rather than silently
dropped — a scan that quietly examined 25 of 4,000 files while reporting
"clean" would be a dishonest result.

Untrusted input
---------------
Archives and repository URLs come from outside. Both paths are treated as
hostile:

* **Zip slip** — an entry named `../../etc/cron.d/x` would escape the
  extraction directory. Entries are validated against the destination root and
  rejected, not sanitised.
* **Zip bombs** — a few KB can expand to gigabytes. Both the per-entry and the
  total *uncompressed* sizes are checked from the archive metadata before any
  byte is written, and again while reading.
* **Symlinks** — an archived symlink pointing at `/etc/shadow` would make a
  scan read host files. Symlink entries are skipped in archives and ignored
  when walking a clone.
* **SSRF and local file reads via repository URL** — only https:// URLs to an
  allowlist of public hosts are accepted, so `file:///`, `git://`, `ssh://`
  and internal addresses cannot be reached.
* **Argument injection** — git runs through `subprocess` with an argument
  list, never a shell, and with credential prompts disabled so a private
  repository fails fast instead of hanging.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from backend.core import languages

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Limits
# --------------------------------------------------------------------------- #

#: Largest single source file we will scan, in bytes. Matches
#: `ScanRequest.code`'s max_length so a collected file is always postable.
MAX_FILE_BYTES = 50_000

#: Largest total uncompressed size accepted from an archive.
MAX_TOTAL_BYTES = 20 * 1024 * 1024

#: Largest number of entries we will even look at inside an archive.
MAX_ARCHIVE_ENTRIES = 5_000

#: Hard ceiling on files actually scanned. Each one is a full pipeline run.
MAX_FILES = 25

#: Seconds allowed for `git clone`.
CLONE_TIMEOUT_S = 60

#: Hosts a repository may be cloned from.
ALLOWED_REPO_HOSTS = frozenset(
    {"github.com", "gitlab.com", "bitbucket.org", "codeberg.org"}
)

#: Directories that never contain first-party source worth scanning. Skipping
#: them is what keeps a real repository under MAX_FILES.
SKIP_DIRECTORIES = frozenset(
    {
        ".git", ".hg", ".svn", "node_modules", "venv", ".venv", "env",
        "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache",
        "dist", "build", "out", "target", ".next", ".nuxt", "coverage",
        "vendor", "site-packages", ".tox", ".gradle", ".idea", ".vscode",
        "bower_components", "jspm_packages", "migrations",
    }
)

#: Minified and generated files produce noise, not findings.
SKIP_FILENAME_MARKERS = (".min.js", ".min.ts", ".bundle.js", ".d.ts", "-lock.json")


class ProjectScanError(Exception):
    """Collection failed for a reason the caller should see."""


@dataclass(frozen=True)
class SourceFile:
    """One collected file, ready to hand to the pipeline."""

    path: str  # repository-relative, POSIX separators
    language: str  # a `languages` registry id
    code: str


@dataclass
class Collection:
    """What a collection produced, including what it deliberately left out."""

    files: list[SourceFile]
    total_source_files: int  # scannable files found before MAX_FILES was applied
    skipped_too_large: list[str]
    truncated: bool  # True when MAX_FILES capped the result

    def as_summary(self) -> dict:
        """Machine-readable account of the collection, for the API response."""
        return {
            "files_scanned": len(self.files),
            "source_files_found": self.total_source_files,
            "truncated": self.truncated,
            "max_files": MAX_FILES,
            "skipped_too_large": self.skipped_too_large,
        }


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #

def _is_skippable_path(parts: tuple[str, ...], filename: str) -> bool:
    if any(part in SKIP_DIRECTORIES for part in parts):
        return True
    lowered = filename.lower()
    if lowered.startswith("."):
        return True
    return any(lowered.endswith(marker) for marker in SKIP_FILENAME_MARKERS)


def _decode(raw: bytes) -> str | None:
    """
    Decode source bytes, or None when this is not text we can scan.

    Binary files sharing a source extension (a compiled `.pyc` renamed, an
    image with a `.java` name) must not reach the model as mojibake.
    """
    if b"\x00" in raw:
        return None
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, ValueError):
            continue
    return None


def _finalise(
    collected: list[SourceFile], skipped_too_large: list[str]
) -> Collection:
    """Apply MAX_FILES and record whether anything was left out."""
    # Deterministic order so the same input always produces the same report.
    collected.sort(key=lambda f: f.path)
    total = len(collected)
    truncated = total > MAX_FILES
    return Collection(
        files=collected[:MAX_FILES],
        total_source_files=total,
        skipped_too_large=sorted(skipped_too_large)[:50],
        truncated=truncated,
    )


# --------------------------------------------------------------------------- #
# ZIP
# --------------------------------------------------------------------------- #

def collect_from_zip(data: bytes) -> Collection:
    """
    Extract scannable source files from an in-memory ZIP archive.

    Nothing is written to disk: entries are read from the archive directly,
    which removes zip-slip as a filesystem concern entirely and keeps the
    path validation below as defence in depth for the *reported* paths.
    """
    try:
        archive = zipfile.ZipFile(_BytesReader(data))
    except zipfile.BadZipFile as exc:
        raise ProjectScanError("That file is not a valid ZIP archive.") from exc

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise ProjectScanError(
                f"Archive has {len(infos)} entries; the limit is {MAX_ARCHIVE_ENTRIES}."
            )

        # Read the declared uncompressed sizes before decompressing anything —
        # this is the check that stops a zip bomb.
        declared_total = sum(i.file_size for i in infos if not i.is_dir())
        if declared_total > MAX_TOTAL_BYTES:
            raise ProjectScanError(
                f"Archive expands to {declared_total // (1024 * 1024)} MB; "
                f"the limit is {MAX_TOTAL_BYTES // (1024 * 1024)} MB."
            )

        collected: list[SourceFile] = []
        skipped_too_large: list[str] = []
        running_total = 0

        for info in infos:
            if info.is_dir():
                continue

            # An archived symlink would let a scan read host files.
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                continue

            name = info.filename.replace("\\", "/")
            pure = Path(name)
            if pure.is_absolute() or ".." in pure.parts:
                # Zip slip. Reject rather than sanitise: a well-formed archive
                # never contains these, so their presence is a signal.
                continue

            parts = pure.parts[:-1]
            filename = pure.name
            if _is_skippable_path(parts, filename):
                continue

            lang = languages.for_extension(pure.suffix)
            if lang is None:
                continue

            if info.file_size > MAX_FILE_BYTES:
                skipped_too_large.append(name)
                continue

            running_total += info.file_size
            if running_total > MAX_TOTAL_BYTES:
                raise ProjectScanError("Archive exceeded the uncompressed size limit.")

            try:
                raw = archive.read(info)
            except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
                logger.warning("zip entry unreadable: %s (%s)", name, exc)
                continue

            # The metadata could lie; check the real bytes too.
            if len(raw) > MAX_FILE_BYTES:
                skipped_too_large.append(name)
                continue

            text = _decode(raw)
            if text is None or not text.strip():
                continue

            collected.append(SourceFile(path=name, language=lang.id, code=text))

    return _finalise(collected, skipped_too_large)


class _BytesReader:
    """Minimal seekable file-like wrapper so ZipFile can read a bytes object."""

    def __init__(self, data: bytes) -> None:
        import io

        self._buf = io.BytesIO(data)

    def __getattr__(self, item):
        return getattr(self._buf, item)


# --------------------------------------------------------------------------- #
# Repository
# --------------------------------------------------------------------------- #

def validate_repo_url(url: str) -> str:
    """
    Return a normalised clone URL, or raise `ProjectScanError`.

    This is the SSRF boundary. Only https:// to an allowlisted public host is
    accepted, so `file:///etc`, `git://`, `ssh://` and internal addresses are
    all unreachable, and a URL cannot smuggle git options because the scheme
    and host are parsed rather than pattern-matched.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise ProjectScanError("Repository URL is required.")
    if len(candidate) > 2048:
        raise ProjectScanError("Repository URL is too long.")

    parsed = urlparse(candidate)
    if parsed.scheme != "https":
        raise ProjectScanError(
            "Only https:// repository URLs are supported."
        )
    if parsed.username or parsed.password:
        raise ProjectScanError("Credentials in the URL are not accepted.")
    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_REPO_HOSTS:
        raise ProjectScanError(
            f"Host '{host or 'unknown'}' is not allowed. Supported: "
            + ", ".join(sorted(ALLOWED_REPO_HOSTS))
            + "."
        )
    if parsed.port is not None:
        raise ProjectScanError("A port may not be specified.")
    if not parsed.path.strip("/"):
        raise ProjectScanError("That URL does not name a repository.")

    # Rebuild from parsed components so nothing but scheme/host/path survives —
    # query strings and fragments never belong in a clone URL.
    return f"https://{host}{parsed.path}"


def clone_repository(url: str, destination: Path) -> None:
    """
    Shallow-clone `url` into `destination`.

    `--depth 1` and `--single-branch` keep it to one commit of one branch;
    history is irrelevant to a source scan and is the bulk of a clone. The
    environment disables every credential prompt so a private or missing
    repository fails inside the timeout instead of blocking on stdin.
    """
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "GCM_INTERACTIVE": "never",
    }
    try:
        subprocess.run(
            [
                "git", "clone",
                "--depth", "1",
                "--single-branch",
                "--no-tags",
                "--quiet",
                "--config", "core.symlinks=false",
                url,
                str(destination),
            ],
            check=True,
            capture_output=True,
            timeout=CLONE_TIMEOUT_S,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise ProjectScanError(
            f"Cloning timed out after {CLONE_TIMEOUT_S}s."
        ) from exc
    except subprocess.CalledProcessError as exc:
        # git's stderr can name the URL but never a credential, since prompts
        # are disabled and credentials in the URL were rejected upstream.
        detail = (exc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        reason = detail[-1] if detail else "unknown error"
        raise ProjectScanError(f"Could not clone the repository: {reason}") from exc
    except FileNotFoundError as exc:  # pragma: no cover - git is a hard dep
        raise ProjectScanError("git is not available on this server.") from exc


def collect_from_directory(root: Path) -> Collection:
    """Walk a checked-out tree and collect scannable source files."""
    collected: list[SourceFile] = []
    skipped_too_large: list[str] = []
    running_total = 0

    for dirpath, dirnames, filenames in os.walk(root):
        # Prune in place so os.walk never descends into them at all.
        dirnames[:] = [
            d for d in dirnames if d not in SKIP_DIRECTORIES and not d.startswith(".")
        ]

        for filename in filenames:
            full = Path(dirpath) / filename

            # Ignore symlinks entirely: a link out of the tree would let a
            # scan read host files.
            if full.is_symlink() or not full.is_file():
                continue

            relative = full.relative_to(root).as_posix()
            if _is_skippable_path(Path(relative).parts[:-1], filename):
                continue

            lang = languages.for_extension(full.suffix)
            if lang is None:
                continue

            try:
                size = full.stat().st_size
            except OSError:
                continue
            if size > MAX_FILE_BYTES:
                skipped_too_large.append(relative)
                continue

            running_total += size
            if running_total > MAX_TOTAL_BYTES:
                break

            try:
                raw = full.read_bytes()
            except OSError as exc:
                logger.warning("unreadable file %s (%s)", relative, exc)
                continue

            text = _decode(raw)
            if text is None or not text.strip():
                continue

            collected.append(SourceFile(path=relative, language=lang.id, code=text))

    return _finalise(collected, skipped_too_large)


def collect_from_repository(url: str) -> Collection:
    """Validate, clone into a temporary directory, collect, then clean up."""
    clone_url = validate_repo_url(url)
    workdir = Path(tempfile.mkdtemp(prefix="devguard-repo-"))
    try:
        target = workdir / "repo"
        clone_repository(clone_url, target)
        return collect_from_directory(target)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
