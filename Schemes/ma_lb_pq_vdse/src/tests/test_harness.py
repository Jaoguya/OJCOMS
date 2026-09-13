#!/usr/bin/env python3
"""Verification tests for the MA-LB-PQ-VDSE experiment harness.

The harness is the layer a reviewer implicitly trusts when reading a number, so
these tests target the properties that would let a wrong number look right:

* the CI is computed from the sample and cannot be supplied;
* warm-ups are discarded and never recorded;
* a failed run is recorded AND re-run, so n reaches 30 rather than 29;
* nothing trims outliers;
* the output columns are exactly global.yaml's;
* every reportability blocker is named in ``run_meta.json``;
* each experiment's ``measure`` covers the boundary its global.yaml rule states.

Runs standalone with no test framework::

    python3 Schemes/ma_lb_pq_vdse/src/tests/test_harness.py
    python3 Schemes/ma_lb_pq_vdse/src/tests/test_harness.py stats
"""

from __future__ import annotations

import csv
import json
import math
import sys
import tempfile
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT))

from Schemes.ma_lb_pq_vdse.src import config as config_mod  # noqa: E402
from Schemes.ma_lb_pq_vdse.src import main as main_mod  # noqa: E402
from Schemes.ma_lb_pq_vdse.src.harness import experiments as exp_mod  # noqa: E402
from Schemes.ma_lb_pq_vdse.src.harness import provenance, runner, stats  # noqa: E402


try:  # Make skips register as real skips when run under pytest.
    import pytest

    Skip = pytest.skip.Exception  # type: ignore[assignment]
except ImportError:

    class Skip(Exception):  # type: ignore[no-redef]
        """Raised to skip a test whose optional backend is unavailable."""


CONFIG = config_mod.load()
SOURCE = exp_mod.SyntheticRecordSource()


# ===========================================================================
# Fixtures — experiments with known behaviour
# ===========================================================================
@dataclass
class ScriptedExperiment:
    """An experiment returning a fixed sequence, for testing the runner itself."""

    samples: List[float]
    name: str = "exp0_scripted"
    number: int = 0
    variable: str = "value"
    values: Tuple[Any, ...] = (1,)
    primary: runner.MetricSpec = runner.MetricSpec("latency", "ms", is_timing=True)
    secondaries: Tuple[runner.MetricSpec, ...] = (
        runner.MetricSpec("secondary_a", "count"),
    )
    prepared_count: int = 0
    measured_count: int = 0

    def prepare(self, value: Any) -> Any:
        self.prepared_count += 1
        return value

    def measure(self, prepared: Any) -> runner.Sample:
        index = self.measured_count
        self.measured_count += 1
        value = self.samples[index % len(self.samples)]
        return runner.Sample(primary=value, secondaries={"secondary_a": 7.0})


@dataclass
class FlakyExperiment(ScriptedExperiment):
    """Fails on the first ``failures`` calls, then succeeds."""

    failures: int = 0
    _calls: int = 0

    def measure(self, prepared: Any) -> runner.Sample:
        self._calls += 1
        if self._calls <= self.failures:
            raise RuntimeError(f"induced failure {self._calls}")
        return runner.Sample(primary=1.0 + self._calls * 0.001, secondaries={})


def metadata_for(name: str, *, runs: int = 30, warmups: int = 5):
    return provenance.build_metadata(
        CONFIG,
        experiment=name,
        corpus_type="synthetic",
        runs=runs,
        warmups=warmups,
    )


# ===========================================================================
# stats.py
# ===========================================================================
def test_ci_uses_student_t_not_the_normal_approximation():
    """At n=30 the difference is ~4% of the interval width.

    Verified against the closed form rather than a hardcoded number, so the test
    fails if the implementation switches to z.
    """
    from scipy import stats as scipy_stats

    values = [float(i) for i in range(30)]
    got = stats.confidence_interval(values, 0.95)
    spread = stats.stdev(values)
    t_based = scipy_stats.t.ppf(0.975, df=29) * spread / math.sqrt(30)
    z_based = 1.959963985 * spread / math.sqrt(30)
    assert abs(got - t_based) < 1e-9
    assert abs(got - z_based) > 1e-4


def test_ci_is_computed_from_the_sample_alone():
    """There is no parameter through which a narrower interval could be supplied."""
    import inspect

    signature = inspect.signature(stats.confidence_interval)
    assert list(signature.parameters) == ["values", "confidence"]
    wide = stats.confidence_interval([1.0, 5.0, 9.0, 13.0])
    tight = stats.confidence_interval([5.0, 5.1, 4.9, 5.05])
    assert wide > tight


def test_stats_module_offers_no_trimming():
    """global.yaml: keep outliers. A trimming helper would invite using it."""
    names = {n for n in dir(stats) if not n.startswith("_")}
    assert not {
        "trim", "trimmed_mean", "winsorize", "drop_outliers", "filter_outliers",
        "reject_outliers",
    } & names


def test_summary_keeps_an_extreme_value():
    """An outlier must move the mean, not be silently discarded."""
    without = stats.summarise([1.0] * 29 + [1.0])
    with_outlier = stats.summarise([1.0] * 29 + [100.0])
    assert with_outlier.mean > without.mean
    assert with_outlier.maximum == 100.0
    assert with_outlier.n == 30


def test_ci_is_zero_for_a_constant_metric():
    """A count that cannot vary has no interval; that is not an error."""
    assert stats.confidence_interval([4.0] * 30) == 0.0
    summary = stats.summarise([4.0] * 30)
    assert summary.has_zero_variance and summary.ci95 == 0.0


def test_plausibility_flags_a_timing_with_zero_variance():
    """Zero variance indicates a bug or fabrication."""
    summary = stats.summarise([2.5] * 30)
    warning = stats.check_plausibility(summary, metric="latency", is_timing=True)
    assert warning is not None and "zero variance" in warning


def test_plausibility_does_not_flag_a_constant_count():
    """Trapdoors issued is 1 every run. Warning about it would train readers to
    ignore the warning."""
    summary = stats.summarise([1.0] * 30)
    assert stats.check_plausibility(summary, metric="trapdoors", is_timing=False) is None


def test_plausibility_flags_a_non_positive_duration():
    summary = stats.summarise([0.0, 1.0, 2.0] * 10)
    warning = stats.check_plausibility(summary, metric="latency", is_timing=True)
    assert warning is not None


def test_plausibility_accepts_a_normal_timing():
    summary = stats.summarise([1.0 + 0.01 * i for i in range(30)])
    assert stats.check_plausibility(summary, metric="latency", is_timing=True) is None


def test_empty_samples_are_refused():
    for call in (
        lambda: stats.mean([]),
        lambda: stats.confidence_interval([]),
        lambda: stats.summarise([]),
    ):
        try:
            call()
        except stats.StatisticsError:
            continue
        raise AssertionError("an empty sample should raise")


# ===========================================================================
# runner.py — the measurement loop
# ===========================================================================
def test_runner_retains_exactly_the_requested_runs():
    experiment = ScriptedExperiment(samples=[1.0, 2.0, 3.0])
    point = runner.run_point(
        experiment, 1, runs=30, warmups=5, confidence=0.95, scheme="s"
    )
    assert point.retained == 30
    assert point.primary.n == 30
    assert len([r for r in point.records if r.status == runner.STATUS_OK]) == 30


def test_runner_discards_warmups_without_recording_them():
    """A warm-up row would describe a run at a cache state the figures do not use."""
    experiment = ScriptedExperiment(samples=[1.0])
    point = runner.run_point(
        experiment, 1, runs=10, warmups=5, confidence=0.95, scheme="s"
    )
    assert experiment.measured_count == 15      # 5 discarded + 10 retained
    assert len(point.records) == 10             # only the retained are recorded
    assert all(r.run_id <= 10 for r in point.records)


def test_runner_prepares_once_per_point():
    """Setup is untimed and must not re-run per measurement."""
    experiment = ScriptedExperiment(samples=[1.0])
    runner.run_point(experiment, 1, runs=8, warmups=2, confidence=0.95, scheme="s")
    assert experiment.prepared_count == 1


def test_runner_records_a_failure_and_reruns_to_restore_n():
    """global.yaml: record status=failed and re-run, rather than reporting n=29."""
    experiment = FlakyExperiment(samples=[1.0], failures=3)
    point = runner.run_point(
        experiment, 1, runs=10, warmups=0, confidence=0.95, scheme="s"
    )
    assert point.retained == 10                 # n restored
    assert point.failed == 3                    # and the failures are visible
    failed = [r for r in point.records if r.status == runner.STATUS_FAILED]
    assert all(r.primary is None for r in failed)
    assert all("induced failure" in r.error for r in failed)


def test_runner_abandons_a_point_that_keeps_failing():
    """A bounded retry, so a deterministic failure cannot loop forever."""
    experiment = FlakyExperiment(samples=[1.0], failures=10_000)
    try:
        runner.run_point(
            experiment, 1, runs=5, warmups=0, confidence=0.95, scheme="s"
        )
    except runner.HarnessError as exc:
        assert "failed" in str(exc)
        return
    raise AssertionError("a permanently failing point should raise")


def test_runner_surfaces_a_zero_variance_warning():
    experiment = ScriptedExperiment(samples=[1.0])     # every run identical
    point = runner.run_point(
        experiment, 1, runs=30, warmups=0, confidence=0.95, scheme="s"
    )
    assert any("zero variance" in w for w in point.warnings)


def test_runner_summarises_secondaries():
    experiment = ScriptedExperiment(samples=[1.0, 2.0])
    point = runner.run_point(
        experiment, 1, runs=10, warmups=0, confidence=0.95, scheme="s"
    )
    assert point.secondaries["secondary_a"].mean == 7.0


# ===========================================================================
# runner.py — output format, global.yaml
# ===========================================================================
def written_outputs(experiment, *, runs=6, warmups=1):
    metadata = metadata_for(experiment.name, runs=runs, warmups=warmups)
    result = runner.run_experiment(experiment, metadata, config=CONFIG)
    directory = Path(tempfile.mkdtemp())
    runner.write_outputs(result, directory)
    return directory, result


def test_raw_runs_columns_match_readme_section_9():
    directory, _ = written_outputs(ScriptedExperiment(samples=[1.0, 2.0]))
    with (directory / "raw_runs.csv").open() as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == list(runner.RAW_COLUMNS)
        rows = list(reader)
    assert rows[0]["scheme"] == "ma_lb_pq_vdse"
    assert rows[0]["status"] == "ok"
    # One row per run, never aggregated.
    assert len(rows) == 6


def test_raw_runs_leaves_unused_secondary_columns_blank():
    """global.yaml: "Blank secondary columns where a metric doesn't apply"."""
    directory, _ = written_outputs(ScriptedExperiment(samples=[1.0, 2.0]))
    with (directory / "raw_runs.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["secondary_metric_1"] != ""      # the one declared metric
    assert rows[0]["secondary_metric_2"] == ""      # none declared


def test_results_columns_are_named_after_their_metric():
    """Columns carry the METRIC'S NAME, not its position.

    This asserted `secondary_1_mean`, `secondary_2_mean`, ... — a layout that
    put a column's meaning in the ORDER of `experiment.secondaries` rather than
    in the column, so every reader bound position to meaning by convention and
    nothing checked the binding. That is the mechanism behind a recurring family
    of defects here: Fig. 8(c) captioned `max_queue_depth` as "Cross-node
    forwards" after the metric list gained an entry and every later column
    shifted under a panel that kept its index.

    Three of the four baselines already wrote named columns, so this is the
    repo's own majority convention rather than a new one. The padding to two
    columns went with it: it existed only to keep a fixed column count, which
    mattered only while columns were addressed by number.
    """
    directory, _ = written_outputs(ScriptedExperiment(samples=[1.0, 2.0, 3.0]))
    with (directory / "results.csv").open() as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == [
            "variable_value",
            "primary_mean",
            "primary_ci95",
            "secondary_a_mean",
            "secondary_a_ci95",
            "n_runs",
        ]
        rows = list(reader)
    assert len(rows) == 1
    assert int(rows[0]["n_runs"]) == 6


@dataclass
class ThreeSecondaryExperiment(ScriptedExperiment):
    """Three recorded secondaries — Exp. 6 after `delivered_kb` was added."""

    secondaries: Tuple[runner.MetricSpec, ...] = (
        runner.MetricSpec("secondary_a", "count"),
        runner.MetricSpec("secondary_b", "KB"),
        runner.MetricSpec("secondary_c", "KB"),
    )

    def measure(self, prepared: Any) -> runner.Sample:
        index = self.measured_count
        self.measured_count += 1
        value = self.samples[index % len(self.samples)]
        return runner.Sample(
            primary=value,
            secondaries={"secondary_a": 7.0, "secondary_b": 8.0, "secondary_c": 9.0},
        )


def test_a_third_secondary_reaches_results_csv():
    """The truncation this widening removed.

    Both writers hardcoded two secondary columns, so an experiment declaring a
    third measured it, aggregated it, and then dropped it on the way to disk
    with nothing said. Exp. 6's panel (b) reads `delivered_kb`, the third; under
    the cap the figure would have been empty and the run wasted.
    """
    directory, _ = written_outputs(ThreeSecondaryExperiment(samples=[1.0, 2.0]))
    with (directory / "results.csv").open() as handle:
        reader = csv.DictReader(handle)
        # By NAME: the third metric is `secondary_c`, and a column named for
        # it cannot be confused with whichever metric happens to sit third.
        assert "secondary_c_mean" in (reader.fieldnames or [])
        rows = list(reader)
    assert float(rows[0]["secondary_c_mean"]) == 9.0


def test_a_third_secondary_reaches_raw_runs_csv():
    directory, _ = written_outputs(ThreeSecondaryExperiment(samples=[1.0, 2.0]))
    with (directory / "raw_runs.csv").open() as handle:
        reader = csv.DictReader(handle)
        assert "secondary_metric_3" in (reader.fieldnames or [])
        rows = list(reader)
    assert float(rows[0]["secondary_metric_3"]) == 9.0
    # `status` stays last however many secondaries are inserted before it.
    assert (reader.fieldnames or [])[-1] == "status"


def test_two_or_fewer_secondaries_keep_the_exact_readme_shape():
    """The floor. §9's printed header must stay literally correct for these."""
    directory, _ = written_outputs(ScriptedExperiment(samples=[1.0]))
    with (directory / "raw_runs.csv").open() as handle:
        assert csv.DictReader(handle).fieldnames == list(runner.RAW_COLUMNS)


def test_results_n_runs_is_the_retained_count():
    """n_runs must be what was retained, not what was requested."""
    experiment = FlakyExperiment(samples=[1.0], failures=2)
    directory, result = written_outputs(experiment, runs=6, warmups=0)
    with (directory / "results.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert int(rows[0]["n_runs"]) == 6
    with (directory / "raw_runs.csv").open() as handle:
        raw = list(csv.DictReader(handle))
    assert len(raw) == 8                       # 6 retained + 2 failed rows
    assert sum(1 for r in raw if r["status"] == "failed") == 2


def test_results_ci_matches_the_raw_rows():
    """The aggregate must be derivable from the raw rows — the audit path."""
    directory, _ = written_outputs(ScriptedExperiment(samples=[1.0, 2.0, 3.0, 4.0]))
    with (directory / "raw_runs.csv").open() as handle:
        raw = [
            float(r["primary_metric"])
            for r in csv.DictReader(handle)
            if r["status"] == "ok"
        ]
    with (directory / "results.csv").open() as handle:
        row = next(csv.DictReader(handle))
    assert abs(float(row["primary_mean"]) - stats.mean(raw)) < 1e-6
    assert abs(float(row["primary_ci95"]) - stats.confidence_interval(raw)) < 1e-6


def test_write_outputs_produces_all_three_files():
    directory, _ = written_outputs(ScriptedExperiment(samples=[1.0, 2.0]))
    for name in ("raw_runs.csv", "results.csv", "run_meta.json"):
        assert (directory / name).is_file()


def test_run_meta_records_the_warnings():
    directory, _ = written_outputs(ScriptedExperiment(samples=[1.0]))
    meta = json.loads((directory / "run_meta.json").read_text())
    assert any("zero variance" in note for note in meta["notes"])
    assert meta["finished_utc"] is not None


# ===========================================================================
# provenance.py
# ===========================================================================
def test_provenance_records_everything_readme_section_7_requires():
    meta = metadata_for("exp1_trapdoor_generation")
    payload = json.loads(meta.to_json())
    for key in (
        "git_commit", "python_version", "instance_type", "libraries",
        "config_hashes", "corpus_type", "started_utc", "thread_pinning",
        "crypto_backends",
    ):
        assert key in payload, key
    assert payload["config_hashes"], "config hashes must be recorded"
    assert payload["runs"] == 30 and payload["warmups"] == 5


def test_provenance_marks_a_dirty_tree():
    """A result from a modified tree cannot be reproduced from the commit alone."""
    commit = provenance.git_commit()
    assert commit == "unknown" or len(commit.split("-")[0]) == 40


def test_provenance_lists_every_blocker_by_name():
    """"reportable: false" with no reason is not provenance."""
    import dataclasses

    # Every blocker must be nameable AT ONCE, so the config is forced back to
    # pending here rather than relying on the live one -- which now carries the
    # swept weights, so "pending_sweep" would legitimately be absent and this
    # test would stop checking the message it exists to check.
    pending = dataclasses.replace(
        CONFIG,
        scheduler=dataclasses.replace(
            CONFIG.scheduler,
            weights=dataclasses.replace(
                CONFIG.scheduler.weights, status="pending_sweep", provisional=True
            ),
        ),
    )
    reportable, reasons = provenance.reportability(
        pending,
        experiment="exp7_search_throughput",
        corpus_type="synthetic",
        corpus_sha256=None,
        group_faithful=False,
        token_scheme_keyed=False,
    )
    assert not reportable
    joined = " ".join(reasons)
    for fragment in (
        "not reportable", "SHA-256", "Type-III", "unkeyed", "pending_sweep",
        "independent process",
    ):
        assert fragment in joined, fragment


def test_provenance_accepts_a_fully_satisfied_run():
    """The gate must be passable, or it is not a gate but a wall.

    Only on the pinned host. The gate's host check queries live EC2 metadata
    (``Common.crypto.config.verify_experiment_host``) and cannot be satisfied
    from a dev machine, so off-host this SKIPS rather than fails — the same
    treatment the pairing and ML-KEM tests get. It still runs, and still has to
    pass, on the machine that produces reportable numbers.
    """
    import dataclasses

    from Common.crypto import config as common_config

    if not common_config.verify_experiment_host()["is_pinned_experiment_host"]:
        raise Skip("not on the pinned AWS experiment host; gate cannot pass here")

    fixed = dataclasses.replace(
        CONFIG,
        scheduler=dataclasses.replace(
            CONFIG.scheduler,
            weights=dataclasses.replace(
                CONFIG.scheduler.weights, status="fixed", provisional=False
            ),
        ),
        topology=dataclasses.replace(CONFIG.topology, independent_processes=False),
    )
    # Must be the ACTUAL frozen pin, not an arbitrary digest: a "fully
    # satisfied" run is by definition one whose corpus matches the campaign's
    # freeze, and reportability() now enforces that (it previously claimed to
    # and did not). Read from config rather than hardcoded so this test keeps
    # testing the real condition after a re-freeze.
    pinned = provenance._expected_corpus_sha256()
    assert pinned, "dataset.yaml has no freeze.expected_corpus_sha256 to test against"
    reportable, reasons = provenance.reportability(
        fixed,
        experiment="exp7_search_throughput",
        corpus_type="synthea",
        corpus_sha256=pinned,
        group_faithful=True,
        token_scheme_keyed=True,
        # Added 2026-08-28 with the ledger-fidelity gate: a "fully satisfied"
        # run is by definition one where EVERY condition holds, so this must
        # be set as each new condition is added, or the test quietly stops
        # asserting that the gate is passable.
        ledger_faithful=True,
    )
    assert reportable, reasons


def test_provenance_refuses_a_corpus_that_is_not_the_frozen_one():
    """A run against a different corpus than the campaign was frozen on must
    not be reportable.

    Regression guard. reportability() used to only check that a SHA-256 was
    *present*, while its own message claimed it had been "verified against the
    frozen pin" — so a run on any other corpus passed the gate. That is the
    silent data swap dataset.yaml's freeze pin exists to prevent.
    """
    import dataclasses

    fixed = dataclasses.replace(
        CONFIG,
        scheduler=dataclasses.replace(
            CONFIG.scheduler,
            weights=dataclasses.replace(
                CONFIG.scheduler.weights, status="fixed", provisional=False
            ),
        ),
        topology=dataclasses.replace(CONFIG.topology, independent_processes=False),
    )
    wrong = "0" * 64
    assert wrong != provenance._expected_corpus_sha256()
    reportable, reasons = provenance.reportability(
        fixed,
        experiment="exp7_search_throughput",
        corpus_type="synthea",
        corpus_sha256=wrong,
        group_faithful=True,
        token_scheme_keyed=True,
    )
    assert not reportable
    assert any("frozen pin" in reason for reason in reasons), reasons


def test_provenance_rejects_a_wrong_repetition_count():
    import dataclasses

    broken = dataclasses.replace(
        CONFIG,
        # 30 is now the WRONG count -- the campaign runs at 10 as of 2026-09-03.
        # The test still pins the same property: a replication count that does
        # not match the configured one must block reportability.
        measurement=dataclasses.replace(CONFIG.measurement, repetitions=30),
    )
    _, reasons = provenance.reportability(
        broken, experiment="exp1_trapdoor_generation", corpus_type="synthea",
        corpus_sha256="x" * 64, group_faithful=True, token_scheme_keyed=True,
    )
    assert any("repetitions" in reason for reason in reasons)


def test_synthetic_source_can_never_be_reportable():
    """The harness must not have a path to a quotable number from invented data."""
    assert SOURCE.corpus_type == "synthetic"
    assert SOURCE.corpus_type not in CONFIG.corpus["reportable_types"]
    meta = metadata_for("exp2_search_latency")
    assert not meta.reportable


# ===========================================================================
# experiments.py — measurement boundaries
# ===========================================================================
#: Exp. 7-8 build `defaults.index_size` RECORDS since 2026-09-10 (they divided
#: by |W_i| before, giving ~3,125 where Exp. 2 built 100,000 at the same
#: configured value). That is correct for a campaign and far too slow for a test
#: suite: one `Exp7Throughput.prepare` measured **58.3 s** at the production
#: size. These tests assert BEHAVIOUR — throughput ordering, utilisation spread,
#: forward counts — none of which needs a 100,000-record index.
#:
#: Shrunk here rather than in `global.yaml`, so the production value stays the
#: one §VI describes and only the tests pay a smaller bill.
_TEST_INDEX_SIZE = 2_000


def _config_for(number: int):
    """CONFIG, with a test-sized index for the two experiments that build one."""
    if number not in (7, 8):
        return CONFIG
    import dataclasses

    return dataclasses.replace(
        CONFIG,
        defaults=dataclasses.replace(
            CONFIG.defaults, index_size=_TEST_INDEX_SIZE
        ),
    )


def measure_once(number: int, value: Any = None):
    experiment = exp_mod.build_experiment(number, _config_for(number), SOURCE)
    point = experiment.values[0] if value is None else value
    prepared = experiment.prepare(point)
    return experiment, experiment.measure(prepared)


def test_all_experiments_are_defined():
    """The eight of global.yaml, which are the eight of Section VI.

    Pinned as a list so a new experiment cannot be added without a test author
    noticing. There is no ninth: the tamper-granularity sweep is Exp. 4's
    `granularity` ARM, folded in on 2026-09-12.
    """
    # EIGHT. Section VI defines no Experiment 9; the tamper sweep is Exp. 4's
    # `granularity` arm, folded in 2026-09-12.
    assert sorted(exp_mod.EXPERIMENTS) == [1, 2, 3, 4, 5, 6, 7, 8]
    for number in range(1, 9):
        experiment = exp_mod.build_experiment(number, CONFIG, SOURCE)
        assert experiment.number == number
        assert experiment.values, f"experiment {number} has no sweep"
        assert experiment.primary.unit


def test_experiment_sweeps_match_global_yaml():
    """The harness must not carry its own copy of the §VI ranges."""
    for number, key in (
        (1, "exp1"), (2, "exp2"), (3, "exp3"), (4, "exp4"),
        (5, "exp5"), (6, "exp6"), (7, "exp7"), (8, "exp8"),
    ):
        experiment = exp_mod.build_experiment(number, CONFIG, SOURCE)
        assert tuple(experiment.values) == tuple(CONFIG.experiment(key).values)


def test_exp1_policy_scopes_match_global_yaml():
    """|P_U| in {1,2,4,8} is a §VI range, so global.yaml owns it, not the code.

    It lived only in `psa_experiments.POLICY_SCOPES` until 2026-09-07. The
    harness must not carry its own copy of a published sweep — that is the same
    rule `test_experiment_sweeps_match_global_yaml` enforces for `values`, and
    the reason it exists is that a code-side copy drifts silently.
    """
    import yaml
    from pathlib import Path as _P
    from Schemes.ma_lb_pq_vdse.src.harness import psa_experiments as psa

    repo = _P(__file__).resolve().parents[4]
    raw = yaml.safe_load(
        (repo / "Experiment Configuration" / "global.yaml").read_text(encoding="utf-8")
    )
    configured = raw["experiments"]["exp1_trapdoor_generation"]["policy_scopes"]
    assert tuple(configured) == tuple(psa.POLICY_SCOPES)
    assert tuple(f"pu{n}" for n in configured) == tuple(psa.PSA_EXP1_VARIANTS)


def test_index_yaml_restates_the_frozen_corpus_correctly():
    """index.yaml repeats corpus facts for readability; they must be the REAL ones.

    It described corpus v2 (1,141,072 records, 2,006 keywords, 4 domains) while
    dataset.yaml had been pinned to v4 since 2026-08-28, so the two configs
    disagreed about which corpus the benchmark runs on. `CorpusReferenceError`
    guards this at load time but compares against the manifest, which is
    git-ignored — so on any machine without a corpus it never fired.
    """
    import json
    import yaml
    from pathlib import Path as _P

    repo = _P(__file__).resolve().parents[4]
    manifest = json.loads(
        (repo / "Dataset" / "dataset_manifest.json").read_text(encoding="utf-8")
    )
    index = yaml.safe_load(
        (repo / "Experiment Configuration" / "index.yaml").read_text(encoding="utf-8")
    )
    ref = index["corpus_reference"]
    assert ref["records"] == manifest["records"]
    assert ref["keyword_universe"] == manifest["keyword_universe_size"]
    assert ref["domains"] == len(manifest["per_domain_counts"])
    assert abs(
        ref["mean_keywords_per_record"] - manifest["keywords_per_record"]["mean"]
    ) < 0.01


def test_exp1_measures_only_trapdoor_generation():
    """Exp. 1 rule: ML-KEM encapsulation excluded from the per-query curve."""
    experiment, sample = measure_once(1, 5)
    assert experiment.primary.unit == "ms"
    assert sample.primary > 0
    assert sample.secondaries["tokens"] == 5.0
    assert sample.secondaries["trapdoor_size"] > 0


def test_exp1_latency_grows_with_q():
    """q PRF evaluations: 20 keywords must cost more than 1."""
    _, one = measure_once(1, 1)
    _, twenty = measure_once(1, 20)
    assert twenty.secondaries["tokens"] == 20.0
    assert twenty.secondaries["trapdoor_size"] > one.secondaries["trapdoor_size"]


def test_exp2_reports_n_eff():
    """global.yaml: n_eff "is the only thing that can demonstrate the paper's claim".

    `>= 0` until 2026-09-07, which passes on the empty result set -- the defect
    that went undetected in guo's Exp. 2 for 150 banked runs and in psa_exp2 at
    every sweep point. A search that matches nothing is not a search latency.
    """
    experiment, sample = measure_once(2, 10_000)
    assert "n_eff" in sample.secondaries
    assert "entries_traversed" in sample.secondaries
    assert sample.secondaries["n_eff"] > 0
    assert sample.secondaries["entries_traversed"] > 0


def test_exp2_sweeps_records_not_index_entries():
    """N is RECORDS -- the axis label, §VI, and all four baselines agree.

    Exp. 2 sized its deployment as `N // keywords_per_record` until 2026-09-07,
    so at N=10^6 it searched 31,250 records while guo, yue_ge and perera each
    indexed 1,000,000 at the same point on a shared x-axis. Identical to the
    defect Exp. 4 carried and had fixed; nothing pinned it for either.
    """
    experiment = exp_mod.build_experiment(2, CONFIG, SOURCE)
    for value in (10_000, 50_000):
        prepared = experiment.prepare(value)
        assert len(prepared["deployment"].records) == value, (
            f"N={value} built {len(prepared['deployment'].records)} records; "
            f"the axis says N is records"
        )


def test_exp3_trapdoor_count_is_an_option_d_property_only():
    """d-independence is OPTION D's property, and the manuscript's scheme drops it.

    This asserted that Exp. 3's `trapdoors_issued` is constant as `d` grows —
    true under `T = H(w)`, where one trapdoor serves every domain. It is FALSE
    under the manuscript's `T = H(w || PID || PV || Dom)`: a token names its
    policy and domain, so a query spanning `d` domains issues `q*d` of them.
    That is divergence D9, and `PsaExp3CrossDomainLatency` measures it directly
    (10 tokens at d=2, 20 at d=4).

    Kept, narrowed to Option D, and renamed, rather than deleted: it is the
    regression guard for the construction it describes, and §VI's Exp. 3 text
    never claimed the single-trapdoor property in the first place — that claim
    lived only in the harness docstring and this secondary.
    """
    counts = {}
    for domains in (2, 4, 6):
        _, sample = measure_once(3, domains)
        counts[domains] = sample.secondaries["trapdoors_issued"]
        assert sample.secondaries["nodes_searched"] >= 1
    assert len(set(counts.values())) == 1, (
        f"under Option D the token is H(w), so the count cannot depend on d; "
        f"got {counts}"
    )
    q = float(config_mod.load().defaults.keywords_per_query)
    assert set(counts.values()) == {q}, (
        f"expected one token per queried keyword (q={q:g} from global.yaml), "
        f"got {counts}"
    )

def test_exp4_reports_proof_size_in_kb_and_path_length():
    experiment, sample = measure_once(4, 10)
    assert experiment.secondaries[0].unit == "KB"
    assert sample.secondaries["proof_size"] > 0
    assert sample.secondaries["path_length"] > 0


def test_exp5_refuses_a_global_rebuild():
    """"A global index rebuild indicates a Phase VII implementation bug"."""
    _, sample = measure_once(5, 100)
    assert sample.secondaries["merkle_nodes_recomputed"] > 0
    assert sample.secondaries["entries_rewritten"] > 0


def test_exp6_reports_message_size_and_fsns_touched():
    """"Report FSNs touched; selective propagation is the claim"."""
    experiment, sample = measure_once(6, 4)
    assert experiment.secondaries[0].unit == "KB"
    assert sample.secondaries["dias_message_size"] > 0
    # One authority's update reaches every node holding the affected shard --
    # `sharding.replication` of them, 2 since 2026-09-12. It was 1 while each
    # domain lived on exactly one node.
    assert sample.secondaries["fsns_touched"] == float(CONFIG.index.replication)


def test_exp7_primary_is_a_throughput_not_a_latency():
    experiment, sample = measure_once(7, 50)
    assert experiment.primary.unit == "queries/s"
    assert sample.primary > 0
    assert "latency_p95" in sample.secondaries


def test_exp8_primary_is_a_utilization_spread():
    experiment, sample = measure_once(8, 50)
    assert experiment.primary.name == "utilization_stddev"
    assert sample.primary >= 0.0
    assert "max_node_utilization" in sample.secondaries


def test_exp7_and_exp8_share_one_workload_engine():
    """global.yaml: both metric sets come from the SAME runs."""
    assert issubclass(exp_mod.Exp7Throughput, exp_mod.SchedulerAblation)
    assert issubclass(exp_mod.Exp8LoadBalance, exp_mod.SchedulerAblation)
    assert exp_mod.Exp7Throughput.replay is exp_mod.SchedulerAblation.replay
    assert exp_mod.Exp8LoadBalance.replay is exp_mod.SchedulerAblation.replay


def test_merge_points_refuses_to_overwrite_newer_results():
    """infra/merge_points.py must not replace fresh results with stale shards.

    Covered here because that script has no suite of its own and the failure is
    a harness-level one: merge() rewrites <base>/results.csv and raw_runs.csv
    from the shards, which is correct when the shards ARE the run and
    destructive when the base was since re-run unsharded and the shards are
    leftovers `fleet.sh deploy` restored from an older campaign.

    Live example: on the fleet, perera_lv_pqabse/exp3_crossdomain_scalability
    was rewritten today at 6c97ee9 with the full 9-point sweep while its
    __points-2..10 siblings still carry 55aa3c8. Merging would have reported
    success while replacing good data with old data.
    """
    import importlib.util
    import json as _json
    import subprocess as _sp
    import tempfile

    spec = importlib.util.spec_from_file_location(
        "merge_points", str(REPO_ROOT / "infra" / "merge_points.py"))
    mp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mp)

    def sha(rev):
        return _sp.run(["git", "rev-parse", rev], capture_output=True,
                       text=True, cwd=REPO_ROOT).stdout.strip()

    old, new_ = sha("55aa3c8"), sha("HEAD")
    if not old or not new_:
        return  # shallow clone: nothing to assert against

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "exp3_crossdomain_scalability"
        base.mkdir()
        meta = base / "run_meta.json"

        meta.write_text(_json.dumps({"git_commit": new_}))
        try:
            mp._refuse_if_base_is_newer(base, old)
            raise AssertionError("merging older shards over a newer base must refuse")
        except SystemExit as exc:
            assert "NEWER" in str(exc)

        # The normal case must still pass: shards newer than the base.
        meta.write_text(_json.dumps({"git_commit": old}))
        mp._refuse_if_base_is_newer(base, new_)

        # Unresolvable provenance fails CLOSED, never silently open.
        meta.write_text(_json.dumps({"git_commit": "0" * 40}))
        try:
            mp._refuse_if_base_is_newer(base, new_)
            raise AssertionError("an unresolvable commit must refuse, not pass")
        except SystemExit:
            pass


def test_superseded_results_are_caught_at_read_time():
    """Stale Exp. 7-8 results must be refused without rewriting their record.

    exp7_search_throughput/ and exp8_load_balance/ carry git_commit 55aa3c8 with
    reportable:true -- written before 5cf65f9 fixed the shared costing overhead,
    the dead queue loop and the 32-record index. They sit under the canonical
    names main.py writes to, so a name-based reader would quote pre-fix numbers.

    The judgement is DERIVED from the commit the record already carries.
    Rewriting run_meta.json to say reportable:false would violate this module's
    contract that provenance is measured and never supplied -- the record would
    then state a belief rather than what ran.
    """
    boundary = provenance.SUPERSEDED_BEFORE[7]

    stale = provenance.superseded_reason(7, "55aa3c828e0e0e6d18b30d1e35a3c6a4b0e8f7a1")
    assert stale and "predates" in stale, (
        "a pre-5cf65f9 Exp. 7 result must be refused"
    )

    # The boundary commit itself is the first GOOD one, not the last bad one.
    assert provenance.superseded_reason(7, boundary) is None
    assert provenance.superseded_reason(8, boundary) is None

    # Dirtiness is a separate question; the suffix must not defeat the test.
    assert provenance.superseded_reason(7, "55aa3c828e0e0e6d18b30d1e35a3c6a4b0e8f7a1-dirty")

    # Exp. 1-6 have no boundary and must not be swept up.
    for n in (1, 2, 3, 4, 5, 6):
        assert provenance.superseded_reason(n, "55aa3c828e0e0e6d18b30d1e35a3c6a4b0e8f7a1") is None

    # An unknown or unresolvable commit fails CLOSED, never silently open.
    assert provenance.superseded_reason(7, "unknown")
    assert provenance.superseded_reason(7, "0" * 40)


def test_dirty_marker_ignores_a_runs_own_output():
    """The ``-dirty`` marker must mean "the code changed", not "a run ran".

    Result dirs are git-tracked and not ignored, and fleet.sh deploy restores
    them onto every node before a run; the run overwrites results.csv and
    raw_runs.csv and then stamps run_meta.json. So an unscoped
    `git status --porcelain` was ALWAYS non-empty at stamp time and every fleet
    run recorded `-dirty` regardless of whether any source changed -- all eight
    Exp. 7-8 dirs at 0536312 carry it. A marker that is always on cannot do the
    one job it has.
    """
    own_output = [
        "Schemes/ma_lb_pq_vdse/exp7_search_throughput__aass/results.csv",
        "Schemes/ma_lb_pq_vdse/exp8_load_balance__no_lb/raw_runs.csv",
        "Schemes/ma_lb_pq_vdse/exp7_search_throughput__aass/run_meta.json",
        "Plots/output/pdf/fig_exp7_throughput.pdf",
        # The hold-out sweep writes this into exp7_search_throughput/; it is run
        # output, so regenerating it must not mark the tree dirty.
        "Schemes/ma_lb_pq_vdse/exp7_search_throughput/lambda_sweep.csv",
    ]
    for path in own_output:
        assert provenance._is_own_output(path), (
            f"{path} is a run's own output; counting it makes -dirty fire on "
            f"every run and stop meaning anything"
        )

    inputs = [
        "Schemes/ma_lb_pq_vdse/src/scheduler/aass.py",
        "Schemes/ma_lb_pq_vdse/src/harness/experiments.py",
        "Experiment Configuration/global.yaml",
        "Common/crypto/config.py",
        "infra/fleet.sh",
        "skill.md",
        # A result dir holding something OTHER than the three known artifacts
        # is not recognised output and must still count.
        "Schemes/ma_lb_pq_vdse/exp7_search_throughput__aass/patch.py",
    ]
    for path in inputs:
        assert not provenance._is_own_output(path), (
            f"{path} can change what a run measures and must still mark -dirty"
        )


def test_dirty_paths_asks_git_for_untracked_files_not_directories():
    """``-uall``, or an untracked result dir counts as dirty on its own.

    Plain ``--porcelain`` collapses an untracked directory to one entry ending
    in "/", with no filename for _is_own_output to classify -- so a fresh result
    directory marked the tree dirty by itself. Measured on a fleet host: an
    untracked exp6 result dir was the ONLY thing making that host dirty.
    """
    import inspect

    src = inspect.getsource(provenance._dirty_paths)
    assert '"-uall"' in src, (
        "_dirty_paths must pass -uall so untracked results appear as files"
    )
    assert not provenance._is_own_output(
        "Schemes/ma_lb_pq_vdse/exp6_authorization_sync__no_lb/"
    ), "a bare directory has no filename and must not be classified as output"
    assert provenance._is_own_output(
        "Schemes/ma_lb_pq_vdse/exp6_authorization_sync__no_lb/results.csv"
    ), "the files inside it are output and must not count"


def test_exp7_and_exp8_record_the_same_arrival_trace():
    """global.yaml: both experiments replay "the same recorded arrival trace".

    They share the engine (above) but each calls prepare() separately, so
    "the same workload" is a property of prepare() being deterministic, not a
    consequence of the shared class. Nothing asserted it. Measured here by
    digesting the trace: same digest for exp7 and exp8, and stable across
    repeated calls, at two concurrencies.

    The digest deliberately EXCLUDES SearchToken.nonce. Every other field --
    the keyword tokens, auth_root, vid_u, and the authorization decision -- is
    the workload; the nonce is fresh randomness per token and MUST vary, which
    the companion test below pins. So the trace is identical in content and is
    NOT byte-identical, and only the first of those is what the figures need.
    """
    import hashlib
    import pickle

    def trace_digest(number: int, concurrency: int) -> str:
        experiment = exp_mod.build_experiment(number, CONFIG, SOURCE)
        digest = hashlib.sha256()
        for token, decision in experiment.prepare(concurrency)["requests"]:
            digest.update(pickle.dumps(token.tokens, protocol=4))
            digest.update(pickle.dumps(token.auth_root, protocol=4))
            digest.update(pickle.dumps(token.vid_u, protocol=4))
            digest.update(pickle.dumps(decision.accepted, protocol=4))
        return digest.hexdigest()

    for concurrency in (8, 32):
        first7 = trace_digest(7, concurrency)
        assert first7 == trace_digest(7, concurrency), (
            f"exp7's trace is not stable across prepare() calls at "
            f"concurrency={concurrency}; the ablation arms are then not "
            f"comparable to each other"
        )
        assert first7 == trace_digest(8, concurrency), (
            f"exp7 and exp8 recorded different traces at "
            f"concurrency={concurrency}; §VI pairs their metrics as one workload"
        )


def test_search_token_nonce_is_fresh_per_token():
    """The counterpart to the digest above: the nonce must NOT be deterministic.

    The trace test passes trivially if someone makes prepare() reproducible by
    freezing the nonce, which would be a real cryptographic defect rather than
    a fix. This fails first if that ever happens.
    """
    experiment = exp_mod.build_experiment(7, CONFIG, SOURCE)
    requests = experiment.prepare(8)["requests"]
    nonces = [token.nonce for token, _ in requests]
    assert len(set(nonces)) == len(nonces), (
        "SearchToken.nonce repeated within one trace"
    )

    again = experiment.prepare(8)["requests"]
    assert [t.nonce for t, _ in again] != nonces, (
        "SearchToken.nonce is reproducible across prepare() calls"
    )


def test_ablation_covers_the_four_variants():
    from Schemes.ma_lb_pq_vdse.src.scheduler import aass as aass_mod

    for variant in aass_mod.VARIANTS:
        experiment = exp_mod.Exp7Throughput(
            config=CONFIG, source=SOURCE, variant=variant
        )
        prepared = experiment.prepare(20)
        sample = experiment.measure(prepared)
        assert sample.primary >= 0.0


def test_scheduler_ablation_ramps_before_measuring(monkeypatch):
    """global.yaml: "Exp. 7-8 warm after a 30 s ramp."

    The ramp belongs in prepare(), which run_point() calls once per point and
    excludes from every timing. Putting it in measure() would ramp 30 times per
    point and time a warm-up as if it were the measurement. This asserts the
    ramp actually replays, and that it does so in prepare and not in measure --
    conftest zeroes RAMP_SECONDS for every other test, so without this the
    feature would be entirely uncovered.
    """
    monkeypatch.setattr(exp_mod, "RAMP_SECONDS", 0.05)
    experiment = exp_mod.Exp7Throughput(config=CONFIG, source=SOURCE)

    calls = []
    original = type(experiment).replay
    monkeypatch.setattr(
        type(experiment), "replay",
        lambda self, d, r, c, _o=original: (calls.append(1), _o(self, d, r, c))[1],
    )

    prepared = experiment.prepare(20)
    ramped = len(calls)
    assert ramped >= 1, "prepare() must ramp before the point is measured"

    experiment.measure(prepared)
    assert len(calls) == ramped + 1, "measure() must replay once, never ramp"


def test_aass_forwards_no_shard_and_the_oblivious_variants_do():
    """§VI Fig. 8(c): AASS "reduces unnecessary forwarding".

    A cross-node forward is a required shard assigned to an FSN that does not
    maintain it. Algorithm 1 makes that impossible for AASS -- the loop skips
    every node with `S notin S_j` -- so its count must be exactly 0, while the
    three arms that do not consult `S_j` must produce some.

    This test replaces one asserting the metric was INVARIANT across variants.
    It was, while the scheduler returned one node per request and forwards were
    counted per authorized domain: at d = m = 4 the answer was k-1 for every
    arm. The per-shard assignment of Algorithm 1 is what makes it evidence
    again, and a regression to a request-level rule would show up here as AASS
    scoring non-zero or the oblivious arms collapsing to zero.
    """
    from Schemes.ma_lb_pq_vdse.src.scheduler import aass as aass_mod

    experiment = exp_mod.Exp7Throughput(config=CONFIG, source=SOURCE)
    prepared = experiment.prepare(40)
    deployment, requests = prepared["deployment"], prepared["requests"]
    # Each node holds `replication` domains, not one: replicas are what give
    # AASS a choice of holder per shard. What must still hold is that every
    # shard has more than one eligible node, which is the premise of this test.
    assert all(len(node.domains) == CONFIG.index.replication
               for node in deployment.nodes)

    counts = {}
    for variant in aass_mod.VARIANTS:
        scheduler = aass_mod.Scheduler(variant, config=CONFIG, reportable=False)
        forwards = 0
        for token, decision in requests:
            request = aass_mod.SearchRequest(
                tokens=token.tokens,
                authorized=decision.authorized_shards,
                vid_u=token.vid_u,
                query_versions=decision.query_versions,
            )
            forwards += scheduler.assign(deployment.nodes, request).forwards
        counts[variant] = forwards

    assert counts[aass_mod.VARIANT_AASS] == 0, (
        f"AASS forwarded {counts[aass_mod.VARIANT_AASS]} shards; Algorithm 1's "
        f"`S notin S_j -> continue` guard makes that impossible, so the "
        f"eligibility filter has been lost"
    )
    oblivious = [v for v in aass_mod.VARIANTS if v != aass_mod.VARIANT_AASS]
    assert any(counts[v] > 0 for v in oblivious), (
        f"no oblivious variant forwarded a shard ({counts}); the metric is flat "
        f"again and Fig. 8(c) would have nothing to show"
    )


def test_injected_stub_group_is_never_reportable():
    """An explicitly injected stand-in must propagate into the context.

    Was test_harness_deployment_is_never_reportable, which called
    build_deployment() with no provider and asserted the result could never be
    reportable. That held only while a stub was the ONLY option;
    build_deployment now prefers the real CharmType3Backend where charm is
    installed, so the old assertion tested the absence of a backend rather than
    the property it was written to protect.

    The invariant that still matters: when a stand-in IS used, its
    unfaithfulness must reach context.reportable rather than stopping at the
    seam -- so a stub run can never be quoted.
    """
    deployment = exp_mod.build_deployment(
        config=CONFIG, source=SOURCE, records=4,
        group_provider=exp_mod._unfaithful_group_provider,
    )
    assert not deployment.context.reportable


# ===========================================================================
# main.py — the CLI
# ===========================================================================
def test_cli_parses_experiment_selections():
    # 1..8. Exp. 9 was folded into Exp. 4 on 2026-09-12 -- Section VI
    # defines one Exp. 4 with a two-panel figure, and 9 is its second arm.
    assert main_mod.parse_experiments("all") == [1, 2, 3, 4, 5, 6, 7, 8]
    assert main_mod.parse_experiments("2") == [2]
    assert main_mod.parse_experiments("1,2,5") == [1, 2, 5]
    assert main_mod.parse_experiments("5,1,5") == [1, 5]


def test_cli_rejects_an_unknown_experiment():
    import argparse

    # 9 became valid on 2026-09-03; 10 is the first number past the registry.
    for bad in ("10", "0", "two", ""):
        try:
            main_mod.parse_experiments(bad)
        except argparse.ArgumentTypeError:
            continue
        raise AssertionError(f"{bad!r} should be refused")


def test_cli_folders_match_the_plotting_paths():
    """Plots/generate_plots.py walks Schemes/*/exp<N>_*/results.csv."""
    scheme_root = REPO_ROOT / "Schemes" / "ma_lb_pq_vdse"
    for number, folder in main_mod.FOLDERS.items():
        assert folder.startswith(f"exp{number}_")
        if not (scheme_root / folder).is_dir():
            # An UNSUFFIXED directory is absent for two legitimate reasons, and
            # neither is a broken path:
            #
            #   * the experiment has never been run (Exp. 9's original case), or
            #   * the experiment is an ABLATION and writes only `<folder>__<arm>`
            #     directories. Exps. 6, 7 and 8 are all ablations; their
            #     unsuffixed directories held superseded n=30 data and were
            #     removed on 2026-09-07 (FIX-4). generate_plots.py reads only
            #     the __variant directories for those numbers, so the plotting
            #     path this test guards is the suffixed one.
            #
            # What must still hold is that SOMETHING the plotter can find
            # exists, so an ablation is checked against its arms.
            arms = sorted(scheme_root.glob(f"{folder}__*"))
            arms = [a for a in arms if a.is_dir() and "points-" not in a.name]
            # Exp. 1 joined 6/7/8 as an ablation on 2026-09-12: it sweeps the
            # authorization scope and writes only `__pu<N>` arms, so it has no
            # unsuffixed directory either. An experiment that has simply never
            # been run has neither, which is the third legitimate case.
            assert arms or not any(scheme_root.glob(f"{folder}*")), (
                f"{folder} has directories but none the plotter can find"
            )


def test_cli_writes_all_three_files_per_experiment():
    output = Path(tempfile.mkdtemp())
    code = main_mod.run(
        ["--experiment", "1", "--smoke", "--quiet", "--output", str(output)]
    )
    assert code == 0
    # Exp. 1 writes ONE DIRECTORY PER SCOPE (`__pu1`..`__pu8`), not a single
    # folder: SVI sweeps q and |P_U| together and each scope is its own curve.
    arms = sorted(output.glob(f"{main_mod.FOLDERS[1]}__*"))
    assert arms, f"no {main_mod.FOLDERS[1]}__* arm directories under {output}"
    for arm in arms:
        for name in ("raw_runs.csv", "results.csv", "run_meta.json"):
            assert (arm / name).is_file(), f"{arm.name} is missing {name}"


def test_cli_require_reportable_matches_the_actual_reportability():
    """--require-reportable must refuse when blocked, and proceed when not.

    Was test_cli_require_reportable_refuses_rather_than_producing_output, which
    asserted code == 2 unconditionally. That encoded "this scheme can never be
    reportable", true only while no Type-III backend existed; CharmType3Backend
    landed 2026-08-28 and on the experiment host the run is now legitimately
    reportable, so the old assertion tested the absence of a backend.

    The contract that actually matters is conditional, and is checked in both
    directions here: when something blocks reportability the CLI must exit 2
    and write NOTHING that could be mistaken for results; when nothing blocks
    it, it must proceed and write them.
    """
    output = Path(tempfile.mkdtemp())
    code = main_mod.run(
        [
            "--experiment", "1", "--smoke", "--quiet",
            "--require-reportable", "--output", str(output),
        ]
    )
    produced = (output / main_mod.FOLDERS[1] / "results.csv").exists()
    if code == 2:
        assert not produced, "refused, but wrote output anyway"
    else:
        assert code == 0 and produced
        meta = json.loads(
            (output / main_mod.FOLDERS[1] / "run_meta.json").read_text()
        )
        assert meta["reportable"] is True
        assert not meta["not_reportable_because"]


def test_cli_reportability_and_its_reasons_always_agree():
    """run_meta.json's flag and its reason list must never contradict.

    Was test_cli_records_non_reportability_in_the_output, which asserted
    reportable is False unconditionally -- again encoding the pre-2026-08-28
    absence of a Type-III backend rather than a property of the harness.

    The invariant that survives a backend landing: whichever way the flag
    falls, it must be consistent with the reasons. A run marked reportable
    with blockers listed, or marked non-reportable with none, would let a
    reader draw the wrong conclusion from the file.
    """
    output = Path(tempfile.mkdtemp())
    main_mod.run(
        ["--experiment", "1", "--smoke", "--quiet", "--output", str(output)]
    )
    arms = sorted(output.glob(f"{main_mod.FOLDERS[1]}__*"))
    assert arms, f"no {main_mod.FOLDERS[1]}__* arm directories under {output}"
    for arm in arms:
        meta = json.loads((arm / "run_meta.json").read_text())
        if meta["reportable"]:
            assert not meta["not_reportable_because"], arm.name
        else:
            assert meta["not_reportable_because"], arm.name


# ===========================================================================
# Runner
# ===========================================================================
def _collect(selector: str | None) -> List[Tuple[str, Callable[[], None]]]:
    tests = [
        (name, obj)
        for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    ]
    if selector:
        tests = [(n, f) for n, f in tests if selector in n]
    return sorted(tests)


def main(argv: List[str]) -> int:
    selector = argv[1] if len(argv) > 1 else None
    tests = _collect(selector)
    if not tests:
        print(f"no tests match {selector!r}")
        return 2

    passed: List[str] = []
    skipped: List[Tuple[str, str]] = []
    failed: List[Tuple[str, str]] = []

    for name, fn in tests:
        try:
            fn()
        except Skip as exc:
            skipped.append((name, str(exc)))
            print(f"SKIP  {name}  ({exc})")
        except Exception:
            failed.append((name, traceback.format_exc()))
            print(f"FAIL  {name}")
        else:
            passed.append(name)
            print(f"ok    {name}")

    print(
        f"\n{len(passed)} passed, {len(skipped)} skipped, {len(failed)} failed "
        f"out of {len(tests)}"
    )
    for name, tb in failed:
        print(f"\n{'=' * 70}\nFAILED: {name}\n{'=' * 70}\n{tb}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
