"""
Tests for the retrieval ablation (the ablation) and the cost-accounting requirement (cost accounting), plus the generated-numbers rule's render check.

The three things worth pinning:

  * **The retrieval ablation's reporting discipline** — median plus min/max, N >= 5, never a single
    number. A summary that quietly degraded to a mean, or accepted N=1, would
    still produce a plausible-looking table.
  * **The interpretability flag.** `retrieval_could_affect_root_cause` is what
    stops a ~0 delta being read as "retrieval does not help" when the truth is
    "the thing retrieval helps was switched off". If that flag ever silently
    became True without a model call, the published conclusion would invert.
  * **The generated-numbers rule's generated-not-typed rule** — the README must match `timings.json`.
    This is the one-line diff check the generated-numbers rule explicitly asks CI to do.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from backend.v2.ablation import (
    ArmSummary, PhaseTiming, RunMetrics, Stopwatch, compare, usd_cost,
    write_timings,
)

TIMINGS = Path("examples/ablation/timings.json")
README = Path("examples/ablation/README.md")


def metrics(arm: str, index: int, ttrc: float, post: float, *, tools: int = 6,
            docs: int = 0, tokens: int = 0, model_calls: int = 0) -> RunMetrics:
    return RunMetrics(
        arm=arm, run_index=index, incident_id=f"AB{index}",
        time_to_root_cause_s=ttrc, post_detection_s=post, detection_s=ttrc - post,
        mcp_tool_calls=tools, evidence_items=10 + docs, documents_retrieved=docs,
        tokens_completion=tokens, model_calls=model_calls,
        usd_cost=usd_cost(0, tokens),
        root_cause_source="MODEL" if model_calls else "DERIVED",
        diagnostician_verdict="ROOT_CAUSE_IDENTIFIED" if model_calls
        else "REASONER_UNAVAILABLE",
        chain_is_sufficient=True,
    )


class TestReportingDiscipline:
    """The retrieval ablation: 'Report median plus min/max, not a single number.'"""

    def test_summary_reports_median_min_and_max(self):
        runs = [metrics("on", i, ttrc=float(t), post=1.0)
                for i, t in enumerate([5.0, 4.0, 9.0, 6.0, 5.5], start=1)]
        s = ArmSummary.of("on", runs)
        assert s.ttrc_median_s == 5.5
        assert s.ttrc_min_s == 4.0 and s.ttrc_max_s == 9.0

    def test_median_not_mean(self):
        """An outlier must not move the headline. 2.9863s cold start really happened."""
        runs = [metrics("off", i, ttrc=1.0, post=float(p))
                for i, p in enumerate([1.76, 1.81, 1.85, 1.86, 2.99], start=1)]
        s = ArmSummary.of("off", runs)
        assert s.post_detection_median_s == 1.85
        assert s.post_detection_max_s == 2.99, "the outlier must still be published"

    def test_empty_arm_raises_rather_than_reporting_zero(self):
        with pytest.raises(ValueError, match="no runs"):
            ArmSummary.of("on", [])

    def test_n_is_carried_so_sample_size_is_always_visible(self):
        assert ArmSummary.of("on", [metrics("on", 1, 1.0, 1.0)]).n == 1


class TestInterpretabilityFlag:
    """The field that keeps a ~0 delta from being misread."""

    def test_false_when_no_model_ran(self):
        on = ArmSummary.of("on", [metrics("on", 1, 5.0, 2.0)])
        off = ArmSummary.of("off", [metrics("off", 1, 4.9, 1.9)])
        result = compare(on, off)
        assert result["retrieval_could_affect_root_cause"] is False
        assert "says nothing about its benefit" in result["interpretation"]

    def test_true_when_a_model_ran(self):
        on = ArmSummary.of("on", [metrics("on", 1, 5.0, 2.0, tokens=900, model_calls=1)])
        off = ArmSummary.of("off", [metrics("off", 1, 6.0, 3.0, tokens=800, model_calls=1)])
        result = compare(on, off)
        assert result["retrieval_could_affect_root_cause"] is True
        assert "measured effect" in result["interpretation"]

    def test_deltas_are_on_minus_off(self):
        on = ArmSummary.of("on", [metrics("on", 1, 5.0, 2.0, tools=8, docs=5)])
        off = ArmSummary.of("off", [metrics("off", 1, 4.0, 1.0, tools=6, docs=0)])
        result = compare(on, off)
        assert result["delta_ttrc_median_s"] == 1.0
        assert result["delta_post_detection_median_s"] == 1.0
        assert result["delta_mcp_tool_calls_median"] == 2


class TestCostAccounting:
    """Cost accounting."""

    def test_zero_tokens_is_zero_cost(self):
        assert usd_cost(0, 0) == 0.0

    def test_cost_scales_with_tokens_when_rates_are_set(self):
        import backend.v2.ablation as ab
        original = ab.USD_PER_1K_COMPLETION_TOKENS
        try:
            ab.USD_PER_1K_COMPLETION_TOKENS = 0.8
            assert ab.usd_cost(0, 1000) == 0.8
        finally:
            ab.USD_PER_1K_COMPLETION_TOKENS = original

    def test_tokens_and_calls_are_summed_per_arm(self):
        runs = [metrics("on", 1, 5.0, 2.0, tokens=100, model_calls=1),
                metrics("on", 2, 5.0, 2.0, tokens=250, model_calls=1)]
        s = ArmSummary.of("on", runs)
        assert s.tokens_total == 350 and s.model_calls_total == 2


class TestTimingsAreMeasured:
    """The measured-numbers rule: every number comes from a real clock."""

    def test_stopwatch_records_phases_in_order(self):
        watch = Stopwatch()
        watch.mark("a")
        watch.mark("b", tool_calls=3)
        assert [p.name for p in watch.phases] == ["a", "b"]
        assert watch.phases[1].tool_calls == 3
        assert all(p.seconds >= 0 for p in watch.phases)

    def test_since_start_is_monotonic(self):
        watch = Stopwatch()
        first = watch.since_start()
        assert watch.since_start() >= first

    def test_run_metrics_has_no_settable_duration(self):
        """Durations are computed, never supplied by a caller."""
        assert "duration" not in {f for f in RunMetrics.__dataclass_fields__}

    def test_write_timings_round_trips(self, tmp_path):
        runs = [metrics("on", 1, 5.0, 2.0), metrics("off", 1, 4.0, 1.0)]
        summaries = {"on": ArmSummary.of("on", runs[:1]),
                     "off": ArmSummary.of("off", runs[1:])}
        path = write_timings(tmp_path / "t.json", runs, summaries,
                             compare(summaries["on"], summaries["off"]))
        payload = json.loads(Path(path).read_text())
        assert payload["schema"] == "devguard.timings/1"
        assert len(payload["runs"]) == 2
        assert set(payload["arms"]) == {"on", "off"}


@pytest.mark.skipif(not TIMINGS.exists(), reason="ablation not run in this checkout")
class TestPublishedAblation:
    """The committed artifact must say what the write-up says it says."""

    @pytest.fixture(scope="class")
    def timings(self) -> dict:
        return json.loads(TIMINGS.read_text())

    def test_n_is_at_least_five_per_arm(self, timings):
        """The retrieval ablation: 'N >= 5 runs per arm.'"""
        for arm, summary in timings["arms"].items():
            assert summary["n"] >= 5, f"arm {arm} has n={summary['n']}"

    def test_both_arms_present(self, timings):
        assert set(timings["arms"]) == {"on", "off"}

    def test_on_arm_retrieved_documents_and_off_arm_did_not(self, timings):
        """Otherwise the arms are not actually different."""
        on = [r for r in timings["runs"] if r["arm"] == "on"]
        off = [r for r in timings["runs"] if r["arm"] == "off"]
        assert all(r["documents_retrieved"] > 0 for r in on)
        assert all(r["documents_retrieved"] == 0 for r in off)

    def test_on_arm_made_more_tool_calls(self, timings):
        assert (timings["arms"]["on"]["mcp_tool_calls_median"]
                > timings["arms"]["off"]["mcp_tool_calls_median"])

    def test_no_model_ran_so_the_flag_is_false(self, timings):
        """If this ever fails, the published interpretation must be rewritten."""
        assert timings["comparison"]["retrieval_could_affect_root_cause"] is False
        assert all(r["model_calls"] == 0 for r in timings["runs"])
        assert all(r["root_cause_source"] == "DERIVED" for r in timings["runs"])

    def test_cost_model_explains_its_zeros(self, timings):
        note = timings["cost_model"]["note"]
        assert "not because inference is free" in note

    def test_every_run_has_a_proof_pack(self, timings):
        for run in timings["runs"]:
            assert run["proof_pack"], f"run {run['arm']}-{run['run_index']} has no pack"

    def test_raw_runs_are_published(self, timings):
        """The retrieval ablation: 'Publish raw runs to examples/ablation/.'"""
        raw = Path("examples/ablation/raw")
        assert raw.is_dir()
        assert len(list(raw.glob("*.json"))) == len(timings["runs"])


@pytest.mark.skipif(not TIMINGS.exists(), reason="ablation not run in this checkout")
class TestRenderedDocsMatchSource:
    """The generated-numbers rule's one-line diff check: rendered docs must match timings.json."""

    def test_readme_is_up_to_date(self):
        result = subprocess.run(
            [sys.executable, "scripts/render_ablation_readme.py", "--check"],
            capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    def test_readme_is_marked_generated(self):
        assert README.read_text().startswith("<!-- GENERATED FILE — DO NOT EDIT.")

    def test_readme_states_the_sample_size_limit(self):
        """The retrieval ablation: 'State the sample size and its limits in one honest sentence.'"""
        # Whitespace-normalised: the template hard-wraps prose, so a phrase can
        # straddle a newline. Asserting on the exact bytes would make this a
        # test of the line width rather than of the content.
        text = " ".join(README.read_text().split())
        assert "Sample size and its limits" in text
        assert "do not generalise" in text
        assert "measurement of overhead, not an estimate of retrieval's value" in text
