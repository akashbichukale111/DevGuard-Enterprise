"""
Collection limits and security boundaries for ZIP and repository scans.

`project_scan` is the step in front of the pipeline: it turns untrusted input
(an uploaded archive, a user-supplied repository URL) into a bounded list of
source files. It makes no model calls, which is what lets every limit below be
tested without an API key.

The security cases are the point of the module. A scanner that can be made to
read `/etc/shadow` by uploading a crafted archive, or to reach an internal
address by pasting a URL, is a vulnerability rather than a feature.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from backend.core import project_scan
from backend.core.project_scan import ProjectScanError


def make_zip(entries: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in entries.items():
            z.writestr(name, content)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# ZIP — happy path
# --------------------------------------------------------------------------- #

def test_collects_supported_sources_and_labels_language():
    data = make_zip(
        {
            "src/app.py": "import os\n",
            "src/index.js": "const a = 1;\n",
            "src/Main.java": "class Main {}\n",
            "src/types.ts": "const x: number = 1;\n",
        }
    )
    result = project_scan.collect_from_zip(data)
    assert {f.path for f in result.files} == {
        "src/app.py",
        "src/index.js",
        "src/Main.java",
        "src/types.ts",
    }
    assert {f.language for f in result.files} == {
        "python",
        "javascript",
        "java",
        "typescript",
    }
    assert result.truncated is False


def test_ignores_non_source_files():
    data = make_zip(
        {
            "README.md": "# docs\n",
            "logo.png": b"\x89PNG\r\n\x1a\n",
            "app.py": "import os\n",
        }
    )
    result = project_scan.collect_from_zip(data)
    assert [f.path for f in result.files] == ["app.py"]


def test_skips_dependency_and_build_directories():
    data = make_zip(
        {
            "node_modules/left-pad/index.js": "module.exports = 1;\n",
            "venv/lib/site.py": "x = 1\n",
            "dist/bundle.js": "var a=1;\n",
            "__pycache__/x.py": "x = 1\n",
            "src/real.py": "import os\n",
        }
    )
    result = project_scan.collect_from_zip(data)
    assert [f.path for f in result.files] == ["src/real.py"]


def test_skips_minified_and_generated_files():
    data = make_zip(
        {
            "a.min.js": "var a=1;\n",
            "types.d.ts": "declare const x: number;\n",
            "real.js": "const a = 1;\n",
        }
    )
    result = project_scan.collect_from_zip(data)
    assert [f.path for f in result.files] == ["real.js"]


def test_binary_content_with_a_source_extension_is_not_scanned():
    # A compiled artefact renamed to .py must never reach the model as mojibake.
    data = make_zip({"payload.py": b"\x00\x01\x02binary\x00"})
    assert project_scan.collect_from_zip(data).files == []


def test_empty_and_whitespace_only_files_are_skipped():
    data = make_zip({"a.py": "", "b.py": "   \n\t\n", "c.py": "import os\n"})
    result = project_scan.collect_from_zip(data)
    assert [f.path for f in result.files] == ["c.py"]


# --------------------------------------------------------------------------- #
# ZIP — security
# --------------------------------------------------------------------------- #

def test_zip_slip_entries_are_rejected():
    data = make_zip(
        {
            "../../../../etc/cron.d/pwned": "x = 1\n",
            "ok.py": "import os\n",
        }
    )
    result = project_scan.collect_from_zip(data)
    assert [f.path for f in result.files] == ["ok.py"]
    assert not any(".." in f.path for f in result.files)


def test_absolute_paths_are_rejected():
    data = make_zip({"/etc/passwd.py": "x = 1\n", "ok.py": "import os\n"})
    result = project_scan.collect_from_zip(data)
    assert [f.path for f in result.files] == ["ok.py"]


def test_windows_style_traversal_is_rejected():
    data = make_zip({"..\\..\\evil.py": "x = 1\n", "ok.py": "import os\n"})
    result = project_scan.collect_from_zip(data)
    assert [f.path for f in result.files] == ["ok.py"]


def test_archived_symlinks_are_skipped():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        info = zipfile.ZipInfo("link.py")
        info.external_attr = (0o120777 << 16)  # symlink mode bits
        z.writestr(info, "/etc/shadow")
        z.writestr("ok.py", "import os\n")
    result = project_scan.collect_from_zip(buf.getvalue())
    assert [f.path for f in result.files] == ["ok.py"]


def test_declared_zip_bomb_is_rejected_before_decompression():
    # One entry declaring more than the total cap must be refused outright.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("bomb.py", b"A" * (project_scan.MAX_TOTAL_BYTES + 1))
    with pytest.raises(ProjectScanError, match="expands to"):
        project_scan.collect_from_zip(buf.getvalue())


def test_too_many_entries_is_rejected():
    entries = {f"f{i}.py": "x = 1\n" for i in range(project_scan.MAX_ARCHIVE_ENTRIES + 1)}
    with pytest.raises(ProjectScanError, match="entries"):
        project_scan.collect_from_zip(make_zip(entries))


def test_oversized_single_file_is_reported_not_silently_dropped():
    data = make_zip(
        {
            "big.py": "x = 1\n" * 20_000,  # > MAX_FILE_BYTES
            "small.py": "import os\n",
        }
    )
    result = project_scan.collect_from_zip(data)
    assert [f.path for f in result.files] == ["small.py"]
    assert "big.py" in result.skipped_too_large


def test_corrupt_archive_raises_a_clear_error():
    with pytest.raises(ProjectScanError, match="not a valid ZIP"):
        project_scan.collect_from_zip(b"this is not a zip file")


# --------------------------------------------------------------------------- #
# Bounding
# --------------------------------------------------------------------------- #

def test_file_count_is_capped_and_truncation_is_reported():
    entries = {f"src/f{i:03d}.py": "import os\n" for i in range(project_scan.MAX_FILES + 10)}
    result = project_scan.collect_from_zip(make_zip(entries))
    assert len(result.files) == project_scan.MAX_FILES
    assert result.total_source_files == project_scan.MAX_FILES + 10
    assert result.truncated is True
    # The summary must state the shortfall rather than implying a full scan.
    summary = result.as_summary()
    assert summary["files_scanned"] == project_scan.MAX_FILES
    assert summary["source_files_found"] == project_scan.MAX_FILES + 10
    assert summary["truncated"] is True


def test_collection_order_is_deterministic():
    entries = {f"src/f{i:02d}.py": "import os\n" for i in range(30)}
    a = project_scan.collect_from_zip(make_zip(entries))
    b = project_scan.collect_from_zip(make_zip(entries))
    assert [f.path for f in a.files] == [f.path for f in b.files]


# --------------------------------------------------------------------------- #
# Repository URL validation — the SSRF boundary
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/owner/repo", "https://github.com/owner/repo"),
        ("https://github.com/owner/repo.git", "https://github.com/owner/repo.git"),
        ("  https://GitHub.com/owner/repo  ", "https://github.com/owner/repo"),
        ("https://gitlab.com/g/p", "https://gitlab.com/g/p"),
    ],
)
def test_valid_repository_urls_are_normalised(url, expected):
    assert project_scan.validate_repo_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "git://github.com/owner/repo",
        "ssh://git@github.com/owner/repo",
        "http://github.com/owner/repo",       # plaintext
        "https://169.254.169.254/latest/meta-data",  # cloud metadata
        "https://localhost/repo",
        "https://127.0.0.1/repo",
        "https://10.0.0.5/internal",
        "https://evil.example.com/repo",
        "https://github.com",                  # names no repository
        "https://user:token@github.com/o/r",   # embedded credentials
        "https://github.com:22/o/r",           # explicit port
        "",
    ],
)
def test_dangerous_repository_urls_are_rejected(url):
    with pytest.raises(ProjectScanError):
        project_scan.validate_repo_url(url)


def test_query_and_fragment_are_stripped_from_clone_url():
    # Nothing but scheme, host and path may survive into a git argument.
    out = project_scan.validate_repo_url("https://github.com/o/r?upload-pack=x#frag")
    assert out == "https://github.com/o/r"


# --------------------------------------------------------------------------- #
# Directory walking
# --------------------------------------------------------------------------- #

def test_directory_walk_skips_symlinks(tmp_path: Path):
    (tmp_path / "real.py").write_text("import os\n")
    try:
        (tmp_path / "link.py").symlink_to("/etc/hostname")
    except (OSError, NotImplementedError):  # pragma: no cover
        pytest.skip("symlinks unavailable")
    result = project_scan.collect_from_directory(tmp_path)
    assert [f.path for f in result.files] == ["real.py"]


def test_directory_walk_prunes_dependency_directories(tmp_path: Path):
    (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
    (tmp_path / "node_modules" / "pkg" / "i.js").write_text("var a = 1;\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "hook.py").write_text("x = 1\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("import os\n")
    result = project_scan.collect_from_directory(tmp_path)
    assert [f.path for f in result.files] == ["src/app.py"]
