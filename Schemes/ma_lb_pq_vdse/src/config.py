"""Configuration loader for MA-LB-PQ-VDSE.

``Common/crypto/config.py`` reads ``crypto.yaml`` and ``dataset.yaml`` — the
files every scheme shares. This module reads the four that describe the
*benchmark* rather than the primitives (``global.yaml``, ``index.yaml``,
``scheduler.yaml``, ``workload/``) and hands the scheme typed objects instead of
nested dicts, so a mistyped key fails at load with the full key path rather than
surfacing as a ``None`` inside a measured loop.

Three responsibilities beyond parsing, each of which exists because a silent
default here would end up in a published number:

* **Cross-file validation** (:meth:`Configuration.validate`). The same quantity
  appears in several files — ``m = 4`` is in ``global.yaml`` twice and implied by
  ``index.yaml``'s shard count — and nothing but a check keeps them equal.
* **Reportability gating.** ``scheduler.yaml`` carries
  ``weights.status: pending_sweep``, and `scheduler.yaml` records the AASS
  weights as undetermined. :meth:`SchedulerConfig.require_fixed` turns that from
  a comment into a refusal, so Exp. 7-8 cannot quietly report figures produced
  by the provisional uniform vector.
* **Provenance.** :func:`config_hashes` hashes the workload files too, which
  ``Common``'s version does not (it globs the config directory's top level
  only). The workload trace determines the Exp. 7-8 numbers, so its hash belongs
  in ``run_meta.json``.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from Common.crypto.config import (  # noqa: E402
    CONFIG_DIR,
    REPO_ROOT,
    ConfigError,
    load_dataset_config,
)
from Common.crypto.config import get as crypto_get  # noqa: E402

SCHEME_NAME = "ma_lb_pq_vdse"

GLOBAL_CONFIG_PATH = CONFIG_DIR / "global.yaml"
INDEX_CONFIG_PATH = CONFIG_DIR / "index.yaml"
SCHEDULER_CONFIG_PATH = CONFIG_DIR / "scheduler.yaml"
WORKLOAD_DIR = CONFIG_DIR / "workload"

# The four scheduler variants of the Exp. 7-8 ablation. Fixed as a
# set: a missing variant would silently shrink the ablation, and an extra one is
# a variant the manuscript does not describe.
REQUIRED_SCHEDULER_VARIANTS = frozenset(
    {"no_lb", "round_robin", "least_loaded", "aass"}
)

# Experiment `variable` names that also have a value in `defaults`. For these,
# the default must appear in the sweep, so every experiment's curve passes
# through the operating point the other experiments hold fixed.
_VARIABLES_WITH_DEFAULTS = {
    "keywords_per_query",
    "domains",
    "index_size",
    "returned_results",
}

_cache: Dict[Path, Dict[str, Any]] = {}
_cache_lock = threading.Lock()


class SchedulerWeightsPendingError(ConfigError):
    """Raised when a reportable run needs AASS weights that are not yet fixed."""


class CorpusReferenceError(ConfigError):
    """Raised when index.yaml's restated corpus facts disagree with the manifest."""


# ===========================================================================
# Raw loading
# ===========================================================================
def _load_yaml(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        raise ConfigError(
            f"config file not found: {path}\n"
            f"Expected it in '{CONFIG_DIR.name}/'."
        )
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ConfigError(f"config file is not a YAML mapping: {path}")
    return data


def load_raw(path: Path, *, reload: bool = False) -> Dict[str, Any]:
    """Parsed YAML, cached per path.

    Cached for the same reason ``Common``'s loader caches: re-reading YAML
    between repetitions of a measurement loop would land in the timings.
    """
    with _cache_lock:
        if reload or path not in _cache:
            _cache[path] = _load_yaml(path)
        return _cache[path]


def _require(node: Mapping[str, Any], *keys: str, source: str) -> Any:
    """Fetch a nested key, naming the full path when it is absent."""
    walked: List[str] = []
    current: Any = node
    for key in keys:
        walked.append(key)
        if not isinstance(current, Mapping) or key not in current:
            raise ConfigError(f"missing key {'.'.join(walked)!r} in {source}")
        current = current[key]
    return current


# ===========================================================================
# Typed views
# ===========================================================================
@dataclass(frozen=True)
class Defaults:
    """global.yaml default parameters. Each experiment varies one of these."""

    keywords_per_query: int
    domains: int
    fog_search_nodes: int
    index_size: int
    returned_results: int

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "Defaults":
        block = _require(raw, "defaults", source="global.yaml")
        return cls(
            keywords_per_query=int(_require(block, "keywords_per_query", source="global.yaml")),
            domains=int(_require(block, "domains", source="global.yaml")),
            fog_search_nodes=int(_require(block, "fog_search_nodes", source="global.yaml")),
            index_size=int(_require(block, "index_size", source="global.yaml")),
            returned_results=int(_require(block, "returned_results", source="global.yaml")),
        )


@dataclass(frozen=True)
class Measurement:
    """global.yaml measurement methodology."""

    repetitions: int
    confidence_interval: float
    warmup_runs: int
    latency_unit: str
    size_unit: str
    throughput_unit: str
    drop_outliers: bool

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "Measurement":
        block = _require(raw, "measurement", source="global.yaml")
        return cls(
            repetitions=int(_require(block, "repetitions", source="global.yaml")),
            confidence_interval=float(
                _require(block, "confidence_interval", source="global.yaml")
            ),
            warmup_runs=int(_require(block, "warmup_runs", source="global.yaml")),
            latency_unit=str(_require(block, "latency_unit", source="global.yaml")),
            size_unit=str(_require(block, "size_unit", source="global.yaml")),
            throughput_unit=str(_require(block, "throughput_unit", source="global.yaml")),
            drop_outliers=bool(block.get("drop_outliers", False)),
        )


@dataclass(frozen=True)
class AuthorityTopology:
    """N_AA and the attribute universe — Phase I Step 2 / Phase II Step 2.

    Both values are ``benchmark`` provenance: §VI states neither. Fixed by team
    decision on 2026-08-08 (one authority per administrative domain).
    """

    count: int
    attributes_per_authority: int
    authority_to_domain: str
    disjoint_attribute_universes: bool
    initial_vid: int

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "AuthorityTopology":
        block = _require(raw, "authorities", source="global.yaml")
        return cls(
            count=int(_require(block, "count", source="global.yaml")),
            attributes_per_authority=int(
                _require(block, "attributes_per_authority", source="global.yaml")
            ),
            authority_to_domain=str(
                _require(block, "authority_to_domain", source="global.yaml")
            ),
            disjoint_attribute_universes=bool(
                _require(block, "disjoint_attribute_universes", source="global.yaml")
            ),
            initial_vid=int(_require(block, "initial_vid", source="global.yaml")),
        )


@dataclass(frozen=True)
class Topology:
    """Fog-cloud topology — global.yaml."""

    fog_search_nodes: int
    cloud_servers: int
    independent_processes: bool
    utilization_sample_interval_ms: int

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "Topology":
        block = _require(raw, "topology", source="global.yaml")
        return cls(
            fog_search_nodes=int(
                _require(block, "fog_search_nodes", source="global.yaml")
            ),
            cloud_servers=int(_require(block, "cloud_servers", source="global.yaml")),
            independent_processes=bool(
                _require(block, "independent_processes", source="global.yaml")
            ),
            utilization_sample_interval_ms=int(
                _require(block, "utilization_sample_interval_ms", source="global.yaml")
            ),
        )


@dataclass(frozen=True)
class ExperimentSpec:
    """One row of global.yaml: the variable, its sweep, and who participates."""

    name: str
    variable: str
    values: Tuple[Any, ...]
    schemes: Tuple[str, ...]
    shares_runs_with: Optional[str] = None
    #: Parameters this experiment PINS while it sweeps ``variable`` -- e.g. Exp. 9
    #: fixes ``returned_results`` at 20,000 and sweeps the tamper count. Kept in
    #: global.yaml rather than in each scheme's runner so all three schemes read
    #: one number; a per-runner constant is how the three drift apart.
    held_constant: Optional[Any] = None
    #: Exp. 4's second sweep. Section VI's Fig. 4 has two panels over two
    #: different variables; `values` is panel (a)'s r, this is panel (b)'s t.
    tamper_values: Tuple[Any, ...] = ()
    #: global.yaml's warm-up ramp, seconds. Only Exp. 7-8 declare one; every other
    #: experiment is plain warm and leaves this None.
    ramp_seconds: Optional[float] = None

    @property
    def participates(self) -> bool:
        return SCHEME_NAME in self.schemes


@dataclass(frozen=True)
class SchedulerWeights:
    """lambda_1..lambda_4 of the AASS score (Phase VI Step 3).

    FOUR, matching ``eq:search-cost``. A fifth weight on ``C^auth`` was carried
    from the previous manuscript revision; see ``scheduler/aass.py::CostVector``.
    """

    index: float
    verify: float
    sync: float
    queue: float
    status: str
    provisional: bool

    @property
    def is_fixed(self) -> bool:
        return self.status == "fixed"

    def as_tuple(self) -> Tuple[float, float, float, float]:
        return (self.index, self.verify, self.sync, self.queue)

    def total(self) -> float:
        return sum(self.as_tuple())


@dataclass(frozen=True)
class SchedulerConfig:
    """scheduler.yaml — the AASS score, its weights, and the ablation variants."""

    weights: SchedulerWeights
    variants: Tuple[str, ...]
    normalization_method: str
    degenerate_term_value: float
    epsilon: float
    refuse_reportable_runs_while_pending: bool
    allow_per_experiment_override: bool
    sweep_workload: str
    reported_workload: str

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "SchedulerConfig":
        weights_block = _require(raw, "weights", source="scheduler.yaml")
        norm = _require(raw, "normalization", source="scheduler.yaml")
        enforcement = _require(raw, "enforcement", source="scheduler.yaml")
        sweep = _require(raw, "sweep", source="scheduler.yaml")
        return cls(
            weights=SchedulerWeights(
                index=float(_require(weights_block, "lambda_1_index", source="scheduler.yaml")),
                verify=float(_require(weights_block, "lambda_2_verify", source="scheduler.yaml")),
                sync=float(_require(weights_block, "lambda_3_sync", source="scheduler.yaml")),
                queue=float(_require(weights_block, "lambda_4_queue", source="scheduler.yaml")),
                status=str(_require(weights_block, "status", source="scheduler.yaml")),
                provisional=bool(weights_block.get("provisional", True)),
            ),
            variants=tuple(sorted(_require(raw, "variants", source="scheduler.yaml"))),
            normalization_method=str(_require(norm, "method", source="scheduler.yaml")),
            degenerate_term_value=float(
                _require(norm, "degenerate_term_value", source="scheduler.yaml")
            ),
            epsilon=float(_require(norm, "epsilon", source="scheduler.yaml")),
            refuse_reportable_runs_while_pending=bool(
                _require(
                    enforcement,
                    "refuse_reportable_runs_while_pending",
                    source="scheduler.yaml",
                )
            ),
            allow_per_experiment_override=bool(
                _require(
                    enforcement, "allow_per_experiment_override", source="scheduler.yaml"
                )
            ),
            sweep_workload=str(_require(sweep, "workload", source="scheduler.yaml")),
            reported_workload=str(
                _require(sweep, "reported_workload", source="scheduler.yaml")
            ),
        )

    def require_fixed(self, *, context: str) -> SchedulerWeights:
        """Return the weights, or refuse if they are still provisional.

        global.yaml requires the weights to be chosen once by a documented
        procedure and left alone; `scheduler.yaml` records them as
        undetermined. Reporting Exp. 7-8 from the provisional uniform vector
        would present an untuned scheduler as the paper's AASS, so this raises
        instead.
        """
        if self.weights.is_fixed:
            return self.weights
        if not self.refuse_reportable_runs_while_pending:
            return self.weights
        raise SchedulerWeightsPendingError(
            f"{context}: AASS weights are '{self.weights.status}', not 'fixed'.\n"
            f"scheduler.yaml carries a provisional uniform vector; reportable "
            f"Exp. 7-8 runs are refused until the documented hold-out sweep "
            f"({self.sweep_workload}) has run and its chosen vector is "
            f"committed with weights.status: fixed.\n"
            f"See scheduler.yaml's weights block. Pass reportable=False to "
            f"run the sweep itself or a smoke test."
        )


@dataclass(frozen=True)
class IndexConfig:
    """index.yaml — the PDSI structure parameters (global.yaml, bitmap/Bloom)."""

    token_hash: str
    token_bits: int
    bitmap_enabled: bool
    bitmap_granularity: str
    bitmap_word_bits: int
    bloom_enabled: bool
    bloom_bits_per_entry: int
    bloom_num_hashes: int
    bloom_target_fpr: float
    shards: int
    shard_key: str
    replication: int
    merkle_hash: str
    merkle_batch_scope: str
    merkle_incremental_update: bool
    corpus_reference: Mapping[str, Any]

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "IndexConfig":
        dsi = _require(raw, "dsi", source="index.yaml")
        bitmap = _require(raw, "bitmap", source="index.yaml")
        bloom = _require(raw, "bloom", source="index.yaml")
        sharding = _require(raw, "sharding", source="index.yaml")
        merkle = _require(raw, "merkle", source="index.yaml")
        return cls(
            token_hash=str(_require(dsi, "token_hash", source="index.yaml")),
            token_bits=int(_require(dsi, "token_bits", source="index.yaml")),
            bitmap_enabled=bool(_require(bitmap, "enabled", source="index.yaml")),
            bitmap_granularity=str(_require(bitmap, "granularity", source="index.yaml")),
            bitmap_word_bits=int(_require(bitmap, "word_bits", source="index.yaml")),
            bloom_enabled=bool(_require(bloom, "enabled", source="index.yaml")),
            bloom_bits_per_entry=int(
                _require(bloom, "bits_per_entry", source="index.yaml")
            ),
            bloom_num_hashes=int(_require(bloom, "num_hashes", source="index.yaml")),
            bloom_target_fpr=float(
                _require(bloom, "target_false_positive_rate", source="index.yaml")
            ),
            shards=int(_require(sharding, "shards", source="index.yaml")),
            shard_key=str(_require(sharding, "key", source="index.yaml")),
            replication=int(_require(sharding, "replication", source="index.yaml")),
            merkle_hash=str(_require(merkle, "hash", source="index.yaml")),
            merkle_batch_scope=str(_require(merkle, "batch_scope", source="index.yaml")),
            merkle_incremental_update=bool(
                _require(merkle, "incremental_update", source="index.yaml")
            ),
            corpus_reference=dict(_require(raw, "corpus_reference", source="index.yaml")),
        )


@dataclass(frozen=True)
class WorkloadConfig:
    """One workload trace definition from ``workload/``."""

    name: str
    role: str
    reportable: bool
    concurrency: Tuple[int, ...]
    repetitions: int
    ramp_seconds: int
    steady_state_seconds: int
    trace_seed: int
    trace_path: str
    replay_identical_to_all_variants: bool
    verify_sha256: bool

    @classmethod
    def from_raw(cls, name: str, raw: Mapping[str, Any]) -> "WorkloadConfig":
        source = f"workload/{name}"
        meta = _require(raw, "meta", source=source)
        generator = _require(raw, "generator", source=source)
        trace = _require(raw, "trace", source=source)
        return cls(
            name=name,
            role=str(_require(meta, "role", source=source)),
            # A workload is reportable only if it says so; the hold-out trace
            # sets it False, and defaulting to True would let a sweep trace
            # produce a figure.
            reportable=bool(meta.get("reportable", True)),
            concurrency=tuple(
                int(c) for c in _require(generator, "concurrency", source=source)
            ),
            repetitions=int(_require(generator, "repetitions", source=source)),
            ramp_seconds=int(_require(generator, "ramp_seconds", source=source)),
            steady_state_seconds=int(
                _require(generator, "steady_state_seconds", source=source)
            ),
            trace_seed=int(_require(trace, "seed", source=source)),
            trace_path=str(_require(trace, "path", source=source)),
            replay_identical_to_all_variants=bool(
                _require(trace, "replay_identical_to_all_variants", source=source)
            ),
            verify_sha256=bool(_require(trace, "verify_sha256", source=source)),
        )


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    """Recursive mapping merge; ``override`` wins at every leaf."""
    merged: Dict[str, Any] = dict(base)
    for key, value in override.items():
        if (
            key in merged
            and isinstance(merged[key], Mapping)
            and isinstance(value, Mapping)
        ):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_workload(name: str, *, reload: bool = False) -> WorkloadConfig:
    """Load one workload file, resolving ``meta.inherits``.

    The hold-out trace inherits from the reported one and overrides only its
    seed and run counts, so the two cannot drift into structurally different
    workloads — which would defeat the point of holding one out.
    """
    if not name.endswith(".yaml"):
        name = f"{name}.yaml"
    raw = load_raw(WORKLOAD_DIR / name, reload=reload)
    parent_name = (raw.get("meta") or {}).get("inherits")
    if parent_name:
        parent = load_raw(WORKLOAD_DIR / str(parent_name), reload=reload)
        # Drop the parent's meta so its role/reportable do not leak into the
        # child, which is precisely what must differ.
        parent = {k: v for k, v in parent.items() if k != "meta"}
        raw = _deep_merge(parent, raw)
    return WorkloadConfig.from_raw(name, raw)


def load_workloads(*, reload: bool = False) -> Dict[str, WorkloadConfig]:
    """Every workload definition in ``workload/``, keyed by file name."""
    if not WORKLOAD_DIR.is_dir():
        raise ConfigError(f"workload directory not found: {WORKLOAD_DIR}")
    files = sorted(WORKLOAD_DIR.glob("*.yaml"))
    if not files:
        raise ConfigError(f"no workload definitions in {WORKLOAD_DIR}")
    return {path.name: load_workload(path.name, reload=reload) for path in files}


# ===========================================================================
# Aggregate
# ===========================================================================
@dataclass(frozen=True)
class Configuration:
    """Every configuration file this scheme reads, parsed and cross-validated."""

    defaults: Defaults
    measurement: Measurement
    topology: Topology
    authorities: AuthorityTopology
    experiments: Tuple[ExperimentSpec, ...]
    index: IndexConfig
    scheduler: SchedulerConfig
    workloads: Mapping[str, WorkloadConfig]
    crypto: Mapping[str, Any]
    environment: Mapping[str, Any]
    corpus: Mapping[str, Any]
    paths: Mapping[str, Any]

    # -- lookups ------------------------------------------------------------
    def experiment(self, name: str) -> ExperimentSpec:
        for spec in self.experiments:
            if spec.name == name or spec.name.startswith(f"{name}_"):
                return spec
        raise ConfigError(f"no experiment named {name!r} in global.yaml")

    def our_experiments(self) -> Tuple[ExperimentSpec, ...]:
        return tuple(spec for spec in self.experiments if spec.participates)

    @property
    def reported_workload(self) -> WorkloadConfig:
        for workload in self.workloads.values():
            if workload.role == "reported":
                return workload
        raise ConfigError("no workload in workload/ declares role: reported")

    @property
    def holdout_workload(self) -> WorkloadConfig:
        for workload in self.workloads.values():
            if workload.role == "sweep_holdout":
                return workload
        raise ConfigError("no workload in workload/ declares role: sweep_holdout")

    def resolved_path(self, key: str) -> Path:
        return REPO_ROOT / str(_require(self.paths, key, source="global.yaml"))

    # -- validation ---------------------------------------------------------
    def validate(self) -> None:
        """Cross-file consistency. Raises :class:`ConfigError` on the first fault.

        Deliberately does NOT touch the corpus: this must pass on a development
        host that holds no derived corpus. Corpus agreement is checked by
        :func:`verify_corpus_reference` when a manifest is actually loaded.
        """
        # m appears in global.yaml twice and again as index.yaml's shard count.
        if self.defaults.fog_search_nodes != self.topology.fog_search_nodes:
            raise ConfigError(
                f"FSN count disagrees within global.yaml: "
                f"defaults.fog_search_nodes={self.defaults.fog_search_nodes} vs "
                f"topology.fog_search_nodes={self.topology.fog_search_nodes}"
            )
        if self.index.shards != self.topology.fog_search_nodes:
            raise ConfigError(
                f"index.yaml sharding.shards={self.index.shards} but there are "
                f"{self.topology.fog_search_nodes} Fog Search Nodes; each FSN "
                f"maintains one shard set (§VI)"
            )
        # One authority per administrative domain (decision of 2026-08-08).
        if (
            self.authorities.authority_to_domain == "one_to_one"
            and self.authorities.count != self.defaults.domains
        ):
            raise ConfigError(
                f"authorities.authority_to_domain is 'one_to_one' but "
                f"count={self.authorities.count} != domains="
                f"{self.defaults.domains}"
            )
        if self.authorities.attributes_per_authority < 1:
            raise ConfigError("authorities.attributes_per_authority must be >= 1")
        if self.authorities.initial_vid < 0:
            raise ConfigError("authorities.initial_vid must be >= 0")

        # Methodology.
        #
        # WAS `!= 30`. Reduced to 10 on the user's instruction, 2026-09-03. The
        # check is kept rather than deleted because its job is to stop the config
        # and the MANUSCRIPT drifting apart: §VI currently says "the average of 30
        # independent runs" and must be changed to 10, or the paper states a
        # replication count the data does not have. Fewer runs also widen every
        # confidence interval -- with n=10 the t-multiplier is 2.26 against 2.05
        # at n=30, so intervals grow ~10% before any change in variance.
        if self.measurement.repetitions != 10:
            raise ConfigError(
                f"measurement.repetitions is {self.measurement.repetitions}; the "
                f"campaign is configured for 10 independent runs (§VI must match)"
            )
        if not 0.0 < self.measurement.confidence_interval < 1.0:
            raise ConfigError("measurement.confidence_interval must be in (0, 1)")
        if self.measurement.drop_outliers:
            raise ConfigError(
                "measurement.drop_outliers is true; global.yaml requires outliers "
                "to be kept and failed runs re-run to restore n=10"
            )

        # Each experiment's sweep must contain the default operating point, so
        # the curves cross where the other experiments hold that value fixed.
        for spec in self.experiments:
            if not spec.values:
                raise ConfigError(f"{spec.name}: empty sweep")
            if spec.variable in _VARIABLES_WITH_DEFAULTS:
                default_value = getattr(self.defaults, spec.variable)
                if default_value not in spec.values:
                    raise ConfigError(
                        f"{spec.name}: default {spec.variable}={default_value} is "
                        f"not in the sweep {list(spec.values)}"
                    )
            for scheme in spec.schemes:
                if not (REPO_ROOT / "Schemes" / scheme).is_dir():
                    raise ConfigError(
                        f"{spec.name}: unknown scheme {scheme!r} (no "
                        f"Schemes/{scheme}/ directory)"
                    )

        # Exp. 7 and Exp. 8 must come from the same runs.
        exp8 = self.experiment("exp8")
        exp7 = self.experiment("exp7")
        if exp8.shares_runs_with != exp7.name:
            raise ConfigError(
                f"{exp8.name} must declare shares_runs_with: {exp7.name} — "
                f"global.yaml requires both metrics from one set of runs"
            )
        if exp7.values != exp8.values:
            raise ConfigError(
                "exp7 and exp8 sweep different concurrency points but are "
                "supposed to be the same runs"
            )

        # Scheduler.
        missing = REQUIRED_SCHEDULER_VARIANTS - set(self.scheduler.variants)
        extra = set(self.scheduler.variants) - REQUIRED_SCHEDULER_VARIANTS
        if missing or extra:
            raise ConfigError(
                f"scheduler.yaml variants must be exactly "
                f"{sorted(REQUIRED_SCHEDULER_VARIANTS)}; missing={sorted(missing)} "
                f"unexpected={sorted(extra)}"
            )
        if self.scheduler.allow_per_experiment_override:
            raise ConfigError(
                "scheduler.yaml allows a per-experiment weight override; "
                "the weights are fixed across Exp. 7-8"
            )
        raw_scheduler = load_raw(SCHEDULER_CONFIG_PATH)
        if (raw_scheduler.get("weights") or {}).get("sum_to_one"):
            total = self.scheduler.weights.total()
            if abs(total - 1.0) > 1e-9:
                raise ConfigError(
                    f"scheduler weights declare sum_to_one but total {total}"
                )
        for weight in self.scheduler.weights.as_tuple():
            if weight < 0.0:
                raise ConfigError("scheduler weights must be non-negative")

        # Workloads. The hold-out exists so the weights are not fitted on the
        # trace that produces the figures; an equal seed would silently undo it.
        reported, holdout = self.reported_workload, self.holdout_workload
        if not reported.reportable:
            raise ConfigError(f"{reported.name}: role 'reported' but reportable false")
        if holdout.reportable:
            raise ConfigError(
                f"{holdout.name}: the sweep hold-out must not be reportable"
            )
        if reported.trace_seed == holdout.trace_seed:
            raise ConfigError(
                f"the reported and hold-out workloads share seed "
                f"{reported.trace_seed}; the hold-out would then be the same "
                f"trace the weights are reported on"
            )
        if reported.trace_path == holdout.trace_path:
            raise ConfigError("the reported and hold-out traces share a path")
        if reported.repetitions != self.measurement.repetitions:
            raise ConfigError(
                f"{reported.name}: repetitions {reported.repetitions} != "
                f"measurement.repetitions {self.measurement.repetitions}"
            )
        for workload in (reported, holdout):
            if not workload.replay_identical_to_all_variants:
                raise ConfigError(
                    f"{workload.name}: all four variants must replay one trace, "
                    f"or Exp. 7-8 is not an ablation"
                )
        if tuple(exp7.values) != reported.concurrency:
            raise ConfigError(
                f"exp7 sweeps {list(exp7.values)} but the reported workload "
                f"defines concurrency {list(reported.concurrency)}"
            )
        if self.scheduler.sweep_workload.split("/")[-1] != holdout.name:
            raise ConfigError(
                f"scheduler.yaml sweep.workload points at "
                f"{self.scheduler.sweep_workload!r}, not the hold-out "
                f"{holdout.name!r}"
            )

        # Index / PDSI.
        if self.index.token_bits % 8 or not 0 < self.index.token_bits <= 256:
            raise ConfigError(
                f"index.yaml token_bits={self.index.token_bits} must be a whole "
                f"number of bytes in 1..256"
            )
        # §VI names SHA-256 for both hashing and Merkle construction, and
        # crypto.yaml sets our digest width to 256 bits. Both must agree with
        # index.yaml, or a commitment would be built from a hash the manuscript
        # does not describe.
        if self.index.merkle_hash != "sha256" or self.index.token_hash != "sha256":
            raise ConfigError(
                f"index.yaml specifies merkle.hash={self.index.merkle_hash!r} and "
                f"dsi.token_hash={self.index.token_hash!r}; §VI specifies sha256 "
                f"for both"
            )
        crypto_hash_bits = int(
            _require(self.crypto, "hash_output_bits", source="crypto.yaml")
        )
        if self.index.token_bits != crypto_hash_bits:
            raise ConfigError(
                f"index.yaml dsi.token_bits={self.index.token_bits} disagrees with "
                f"crypto.yaml ma_lb_pq_vdse.hash_output_bits={crypto_hash_bits}"
            )
        if not self.index.merkle_incremental_update:
            raise ConfigError(
                "index.yaml merkle.incremental_update is false; a full rebuild "
                "is a Phase VII bug (global.yaml, Exp. 5 rule)"
            )
        # WAS `!= 1`, refusing any replication with "replicas would give the
        # scheduler a choice the paper does not describe". Inverted on
        # 2026-09-12: at replication 1 the eligible set for any shard is a
        # SINGLETON, so Algorithm 1's `S notin S_j` guard forces AASS to the one
        # holder and it cannot balance load at all. Measured that way, Exp. 8
        # panel (a) ranks the arms by how evenly they spread work while ignoring
        # whether the chosen node can serve the shard -- `least_loaded` came out
        # 7x better than AASS on utilization spread while paying 2,984
        # cross-node forwards against AASS's 0. The scheduler ablation needs a
        # choice to ablate, which is what `OJCOMS.md` says and what the old
        # rationale here contradicted.
        #
        # Replication is a DEPLOYMENT parameter, not a construction claim: the
        # manuscript's silence on replica-based availability is not a
        # prohibition, and nothing cryptographic depends on how many nodes hold
        # a shard. It stays a recorded benchmark decision (open decision 3).
        if self.index.replication < 2:
            raise ConfigError(
                f"index.yaml sharding.replication={self.index.replication}; "
                f"below 2 every shard has exactly one eligible node, so AASS "
                f"has no scheduling freedom and Exp. 7-8 compare arms that "
                f"cannot differ on merit"
            )
        if self.index.replication > self.topology.fog_search_nodes:
            raise ConfigError(
                f"index.yaml sharding.replication={self.index.replication} "
                f"exceeds topology.fog_search_nodes="
                f"{self.topology.fog_search_nodes}"
            )

        # Crypto: our pairing must stay Type-III (Phase I Step 1).
        pairing = _require(self.crypto, "pairing", source="crypto.yaml")
        if str(_require(pairing, "type", source="crypto.yaml")) != "type-3":
            raise ConfigError(
                f"crypto.yaml ma_lb_pq_vdse.pairing.type is "
                f"{pairing.get('type')!r}; Phase I Step 1 publishes "
                f"e : G_1 x G_2 -> G_T"
            )
        if pairing.get("allow_symmetric_backend"):
            raise ConfigError(
                "crypto.yaml allows a symmetric pairing backend for this scheme; "
                "that changes group-element sizes and pairing cost"
            )

        # Corpus reportability.
        corpus_type = str(_require(self.corpus, "type", source="global.yaml"))
        reportable_types = _require(self.corpus, "reportable_types", source="global.yaml")
        if corpus_type not in reportable_types:
            raise ConfigError(
                f"corpus.type={corpus_type!r} is not in reportable_types "
                f"{list(reportable_types)}"
            )

        # global.yaml vs dataset.yaml, which owns the corpus range.
        dataset = load_dataset_config()
        dataset_corpus = dataset.get("corpus") or {}
        # These are two DIFFERENT quantities and equality was the wrong test.
        # `global.yaml defaults.domains` is the published §VI default (4,
        # "four administrative healthcare domains") used by Exp. 1/2/4/5/6.
        # `dataset.yaml corpus.domains` is how many domains the corpus can
        # SUPPLY, which must cover the largest d that Exp. 3 sweeps (10). The
        # corpus was rebuilt with 10 domains on 2026-08-28 precisely so Exp. 3
        # could run its published range; requiring equality would have forced
        # the §VI default to 10 as well, contradicting the paper and changing
        # every other experiment's configuration.
        #
        # The real constraint is coverage: the corpus must supply at least the
        # default, and at least the widest sweep point.
        if "domains" in dataset_corpus:
            available = int(dataset_corpus["domains"])
            if available < self.defaults.domains:
                raise ConfigError(
                    f"dataset.yaml corpus.domains={available} cannot supply "
                    f"global.yaml's default domains={self.defaults.domains}"
                )
            try:
                widest = max(int(v) for v in self.experiment("exp3").values)
            except Exception:  # noqa: BLE001 - exp3 may be absent in a subset config
                widest = self.defaults.domains
            if available < widest:
                raise ConfigError(
                    f"dataset.yaml corpus.domains={available} cannot supply "
                    f"exp3's widest sweep point d={widest}. Rebuild the corpus "
                    f"with --domains {widest} (and re-freeze), or narrow the "
                    f"exp3 sweep in global.yaml."
                )
        exp2 = self.experiment("exp2")
        low, high = min(exp2.values), max(exp2.values)
        if "min_records" in dataset_corpus and low < int(dataset_corpus["min_records"]):
            raise ConfigError(
                f"exp2 sweeps down to {low} but dataset.yaml corpus.min_records "
                f"is {dataset_corpus['min_records']}"
            )
        if "max_records" in dataset_corpus and high > int(dataset_corpus["max_records"]):
            raise ConfigError(
                f"exp2 sweeps up to {high} but dataset.yaml corpus.max_records "
                f"is {dataset_corpus['max_records']}"
            )


def load(*, reload: bool = False, validate: bool = True) -> Configuration:
    """Load and cross-validate every configuration file this scheme reads."""
    raw_global = load_raw(GLOBAL_CONFIG_PATH, reload=reload)
    raw_index = load_raw(INDEX_CONFIG_PATH, reload=reload)
    raw_scheduler = load_raw(SCHEDULER_CONFIG_PATH, reload=reload)

    experiments = []
    for name, block in _require(raw_global, "experiments", source="global.yaml").items():
        experiments.append(
            ExperimentSpec(
                name=name,
                variable=str(_require(block, "variable", source="global.yaml")),
                values=tuple(_require(block, "values", source="global.yaml")),
                schemes=tuple(_require(block, "schemes", source="global.yaml")),
                shares_runs_with=block.get("shares_runs_with"),
                held_constant=block.get("held_constant"),
                tamper_values=tuple(block.get("tamper_values", ()) or ()),
                ramp_seconds=(
                    None if block.get("ramp_seconds") is None
                    else float(block["ramp_seconds"])
                ),
            )
        )

    config = Configuration(
        defaults=Defaults.from_raw(raw_global),
        measurement=Measurement.from_raw(raw_global),
        topology=Topology.from_raw(raw_global),
        authorities=AuthorityTopology.from_raw(raw_global),
        experiments=tuple(sorted(experiments, key=lambda spec: spec.name)),
        index=IndexConfig.from_raw(raw_index),
        scheduler=SchedulerConfig.from_raw(raw_scheduler),
        workloads=load_workloads(reload=reload),
        crypto=crypto_get(SCHEME_NAME),
        environment=_require(raw_global, "environment", source="global.yaml"),
        corpus=_require(raw_global, "corpus", source="global.yaml"),
        paths=_require(raw_global, "paths", source="global.yaml"),
    )
    if validate:
        config.validate()
    return config


# ===========================================================================
# Corpus cross-check and provenance
# ===========================================================================
def verify_corpus_reference(manifest: Mapping[str, Any]) -> None:
    """Check ``index.yaml``'s restated corpus facts against a loaded manifest.

    ``index.yaml`` restates records, keyword universe, pair count and domain
    count so its Bloom and shard sizing can be checked without opening another
    file, and says code must read them from the manifest instead. Restated facts
    drift, so this compares the two and raises on disagreement.

    Called by experiment runners once a corpus is loaded, NOT by
    :meth:`Configuration.validate` — validation has to pass on a host with no
    corpus.
    """
    reference = IndexConfig.from_raw(load_raw(INDEX_CONFIG_PATH)).corpus_reference
    checks = (
        ("records", "records"),
        ("keyword_universe", "keyword_universe_size"),
        ("keyword_document_pairs", "keyword_document_pairs"),
        ("domains", "domains"),
    )
    mismatches = []
    for reference_key, manifest_key in checks:
        if reference_key not in reference or manifest_key not in manifest:
            continue
        expected, actual = reference[reference_key], manifest[manifest_key]
        if int(expected) != int(actual):
            mismatches.append(f"  {reference_key}: index.yaml {expected} vs manifest {actual}")
    if mismatches:
        raise CorpusReferenceError(
            "index.yaml corpus_reference disagrees with the loaded manifest:\n"
            + "\n".join(mismatches)
            + "\nOne of the two is stale. index.yaml's Bloom and shard sizing "
            "was derived from its values, so this must be resolved before the "
            "sizing can be justified."
        )


def config_hashes() -> Dict[str, str]:
    """SHA-256 of every config file this scheme reads, for ``run_meta.json``.

    Includes ``workload/*.yaml``, which ``Common.crypto.config.config_hashes``
    omits because it globs only the config directory's top level. The workload
    trace determines the Exp. 7-8 numbers, so leaving it out of provenance would
    make those figures untraceable to the trace that produced them.
    """
    hashes: Dict[str, str] = {}
    paths: List[Path] = sorted(CONFIG_DIR.glob("*.yaml"))
    if WORKLOAD_DIR.is_dir():
        paths.extend(sorted(WORKLOAD_DIR.glob("*.yaml")))
    for path in paths:
        # as_posix(): the key is provenance, and str() would emit
        # "workload\x.yaml" on Windows and "workload/x.yaml" on Linux —
        # the same file under two keys, so run_meta records would not compare.
        key = path.relative_to(CONFIG_DIR).as_posix()
        hashes[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def thread_pinning_report() -> Dict[str, Optional[str]]:
    """The BLAS thread environment actually in force."""
    raw_global = load_raw(GLOBAL_CONFIG_PATH)
    variables = _require(raw_global, "environment", "thread_env_vars", source="global.yaml")
    return {str(name): os.environ.get(str(name)) for name in variables}


def verify_thread_pinning(*, require: bool = False) -> Dict[str, Optional[str]]:
    """Check BLAS threads are pinned to the configured count.

    numpy claims every core by default, which would make Ref[52]'s latency
    depend on core count. ``require=True`` is for reportable runs;
    a development host that has not sourced ``provision.sh`` should not be
    blocked from running tests.
    """
    report = thread_pinning_report()
    expected = str(
        _require(load_raw(GLOBAL_CONFIG_PATH), "environment", "blas_threads", source="global.yaml")
    )
    wrong = {name: value for name, value in report.items() if value != expected}
    if wrong and require:
        raise ConfigError(
            f"BLAS threads are not pinned to {expected}: {wrong}\n"
            f"Export the variables in global.yaml environment.thread_env_vars "
            f"(infra/provision.sh does this) before a reportable run."
        )
    return report


__all__ = [
    "SCHEME_NAME",
    "GLOBAL_CONFIG_PATH",
    "INDEX_CONFIG_PATH",
    "SCHEDULER_CONFIG_PATH",
    "WORKLOAD_DIR",
    "ConfigError",
    "SchedulerWeightsPendingError",
    "CorpusReferenceError",
    "Defaults",
    "Measurement",
    "Topology",
    "AuthorityTopology",
    "ExperimentSpec",
    "SchedulerWeights",
    "SchedulerConfig",
    "IndexConfig",
    "WorkloadConfig",
    "Configuration",
    "load",
    "load_raw",
    "load_workload",
    "load_workloads",
    "verify_corpus_reference",
    "config_hashes",
    "thread_pinning_report",
    "verify_thread_pinning",
]
