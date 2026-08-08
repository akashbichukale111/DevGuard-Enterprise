"""
MANIFEST.json describes what is on disk, including after a partial re-shoot.

`docs/screenshots/datahub/MANIFEST.json` is the record the screenshot
documentation is written from: which of the 23 panels were captured, and for any
that were not, why. A re-shoot of two panels used to rewrite that file to
describe two captures while twenty-three PNGs sat beside it. A record of what is
on disk that disagrees with what is on disk is worse than no record — it is a
claim, and this project's screenshots are only evidence because the claim about
them can be checked.

So a `--only` run merges. These tests hold the merge to three properties:
untouched rows survive verbatim, the file stays in the documented shot order,
and no timestamp ever overstates how fresh the contents are. The failure path
matters as much: a partial run that dies at login must not clobber a full
manifest on its way out.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, "scripts")
import capture_datahub_screenshots as cap  # noqa: E402

REPO_MANIFEST = Path("docs/screenshots/datahub/MANIFEST.json")


def _full_run(out: Path) -> list[cap.Capture]:
    """Write a manifest for every documented shot, as a full run does."""
    captures = [
        cap.Capture(s.slug, s.title, f"http://localhost:9002/{s.slug}",
                    s.shows, True, f"{s.slug}.png", 100)
        for s in cap.shots()
    ]
    cap.write_manifest(out, captures, merge=False, frontend="http://localhost:9002",
                       partial_run=None)
    return captures


def _rows(out: Path) -> dict[str, dict]:
    manifest = json.loads((out / "MANIFEST.json").read_text(encoding="utf-8"))
    return {r["slug"]: r for r in manifest["captures"]}


class TestPartialReshootMerges:
    def test_a_two_panel_reshoot_keeps_all_twenty_three_rows(self, tmp_path):
        """The bug this exists for: two captures replacing twenty-three."""
        full = _full_run(tmp_path)
        assert len(full) == len(cap.shots())

        reshot = [c for c in full if c.slug in ("02-home", "18-analytics")]
        cap.write_manifest(
            tmp_path,
            [cap.Capture(c.slug, c.title, c.url, c.shows, True, c.file, 999)
             for c in reshot],
            merge=True, frontend="http://localhost:9002",
            partial_run=["02-home", "18-analytics"],
        )
        assert len(_rows(tmp_path)) == len(cap.shots())

    def test_rows_that_were_not_reshot_survive_verbatim(self, tmp_path):
        """A merge must not quietly rewrite what it did not re-photograph."""
        _full_run(tmp_path)
        before = _rows(tmp_path)

        cap.write_manifest(
            tmp_path,
            [cap.Capture("02-home", "Home", "u", "shows", True, "02-home.png", 999)],
            merge=True, frontend="http://localhost:9002", partial_run=["02-home"],
        )
        after = _rows(tmp_path)

        for slug in before:
            if slug != "02-home":
                assert after[slug] == before[slug], f"{slug} changed under a merge"
        assert after["02-home"]["bytes"] == 999, "the re-shot row was not updated"

    def test_the_merged_file_stays_in_documented_shot_order(self, tmp_path):
        """The docs index reads in this order regardless of what was re-shot."""
        _full_run(tmp_path)
        cap.write_manifest(
            tmp_path,
            [cap.Capture("18-analytics", "Analytics", "u", "s", True, "f.png", 1)],
            merge=True, frontend="f", partial_run=["18-analytics"],
        )
        manifest = json.loads((tmp_path / "MANIFEST.json").read_text())
        assert [r["slug"] for r in manifest["captures"]] == [s.slug for s in cap.shots()]

    def test_a_full_run_replaces_rather_than_accumulating(self, tmp_path):
        """merge=False is how a shrunk shot list drops rows that no longer exist."""
        _full_run(tmp_path)
        cap.write_manifest(
            tmp_path,
            [cap.Capture("02-home", "Home", "u", "s", True, "02-home.png", 5)],
            merge=False, frontend="f", partial_run=None,
        )
        assert list(_rows(tmp_path)) == ["02-home"]


class TestTimestampsNeverOverstateFreshness:
    def test_each_row_carries_its_own_capture_time(self, tmp_path):
        _full_run(tmp_path)
        assert all(r.get("captured_at") for r in _rows(tmp_path).values())

    def test_the_headline_time_never_outruns_the_newest_row(self, tmp_path):
        """
        A single run-level timestamp would re-date every panel a re-shoot did not
        touch — the manifest would claim 23 fresh captures on the strength of 2.
        """
        _full_run(tmp_path)
        cap.write_manifest(
            tmp_path,
            [cap.Capture("02-home", "Home", "u", "s", True, "02-home.png", 9)],
            merge=True, frontend="f", partial_run=["02-home"],
        )
        manifest = json.loads((tmp_path / "MANIFEST.json").read_text())
        newest = max(r["captured_at"] for r in manifest["captures"])
        assert manifest["captured_at"] == newest
        assert manifest["manifest_written_at"] >= newest

    def test_rows_predating_per_shot_stamps_inherit_the_old_run_time(self, tmp_path):
        """
        The committed manifest was written before shots were stamped
        individually. Merging into it must date those rows to the run that
        actually took them, not to the re-shoot that kept them.
        """
        legacy = {
            "captured_at": "2020-01-01T00:00:00+00:00",
            "frontend": "http://localhost:9002",
            "captures": [{"slug": "05-dataset", "title": "Dataset", "url": "u",
                          "shows": "s", "captured": True, "file": "05-dataset.png",
                          "bytes": 100, "reason": "", "console_errors": []}],
        }
        (tmp_path / "MANIFEST.json").write_text(json.dumps(legacy), encoding="utf-8")

        cap.write_manifest(
            tmp_path,
            [cap.Capture("02-home", "Home", "u", "s", True, "02-home.png", 9)],
            merge=True, frontend="f", partial_run=["02-home"],
        )
        rows = _rows(tmp_path)
        assert rows["05-dataset"]["captured_at"] == "2020-01-01T00:00:00+00:00"
        assert rows["02-home"]["captured_at"] > "2020-01-01T00:00:00+00:00"


class TestFailurePathsDoNotDestroyTheRecord:
    def test_a_partial_run_dying_at_login_keeps_the_existing_rows(self, tmp_path):
        """The login-failure write is a manifest write too, and merges the same."""
        _full_run(tmp_path)
        cap.write_manifest(tmp_path, [], merge=True, frontend="f",
                           login_failed="Timeout waiting for /login")
        manifest = json.loads((tmp_path / "MANIFEST.json").read_text())
        assert len(manifest["captures"]) == len(cap.shots())
        assert manifest["login_failed"] == "Timeout waiting for /login"

    def test_an_unreadable_existing_manifest_does_not_abort_the_write(
            self, tmp_path, capsys):
        """Corruption costs the old rows, but must not also cost the new ones."""
        (tmp_path / "MANIFEST.json").write_text("{not json", encoding="utf-8")
        cap.write_manifest(
            tmp_path,
            [cap.Capture("02-home", "Home", "u", "s", True, "02-home.png", 9)],
            merge=True, frontend="f", partial_run=["02-home"],
        )
        assert list(_rows(tmp_path)) == ["02-home"]
        assert "could not merge" in capsys.readouterr().out

    def test_a_failed_capture_is_recorded_with_its_reason(self, tmp_path):
        """Honest failure: recorded with captured=false, never omitted."""
        cap.write_manifest(
            tmp_path,
            [cap.Capture("15-policies", "Policies", "u", "s", False,
                         reason="TimeoutError: nav timeout")],
            merge=False, frontend="f", partial_run=None,
        )
        row = _rows(tmp_path)["15-policies"]
        assert row["captured"] is False
        assert "TimeoutError" in row["reason"]


@pytest.mark.skipif(not REPO_MANIFEST.is_file(), reason="manifest not present")
class TestTheCommittedManifest:
    def test_it_describes_every_documented_shot_and_all_are_captured(self):
        manifest = json.loads(REPO_MANIFEST.read_text(encoding="utf-8"))
        rows = {r["slug"]: r for r in manifest["captures"]}
        assert set(rows) == {s.slug for s in cap.shots()}
        uncaptured = [s for s, r in rows.items() if not r["captured"]]
        assert not uncaptured, f"documented as complete, but missing: {uncaptured}"

    def test_every_row_points_at_a_png_that_exists_with_the_recorded_size(self):
        """The claim the whole directory rests on: the files are really there."""
        manifest = json.loads(REPO_MANIFEST.read_text(encoding="utf-8"))
        for row in manifest["captures"]:
            png = REPO_MANIFEST.parent / row["file"]
            assert png.is_file(), f"{row['slug']} names a missing file {row['file']}"
            assert png.stat().st_size == row["bytes"], (
                f"{row['slug']} records {row['bytes']} bytes, file has "
                f"{png.stat().st_size}")
