"""``run_meta.json`` — provenance for every result, per global.yaml.

"Each ``results.csv`` gets a ``run_meta.json``: git commit, instance type, Python
and library versions, dataset SHA-256, corpus type, config hashes, UTC start
time."

The point of this file is that a number in the paper can be traced back to the
tree, the corpus and the host that produced it. So every field is **read from the
environment, never supplied by a caller** — a provenance record that could be
passed the commit it claims would record what someone believed rather than what
ran.

:func:`reportability` collects the conditions required for a result to
be quotable. It returns the failing reasons rather than a bool, because
``run_meta.json`` records *why* a run was not reportable, and "reportable: false"
with no reason is not provenance.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from Common.crypto import environment_report  # noqa: E402
from Common.crypto.config import REPO_ROOT, verify_experiment_host  # noqa: E402

from .. import config as scheme_config  # noqa: E402

SCHEME_NAME = "ma_lb_pq_vdse"


#: Paths a run necessarily rewrites as it produces output, so their being
#: modified says nothing about whether the CODE was modified.
#:
#: This exclusion exists because the unscoped check could never fire usefully.
#: Result directories are git-TRACKED and not ignored, and ``fleet.sh deploy``
#: restores them onto every node before a run; the run then overwrites
#: ``results.csv`` / ``raw_runs.csv`` and stamps ``run_meta.json`` afterwards.
#: So ``git status --porcelain`` was already non-empty at stamp time and EVERY
#: fleet run recorded ``-dirty`` by construction -- the eight Exp. 7-8 result
#: dirs at 0536312 all carry it. A marker that is always on cannot distinguish
#: a modified scheme from an experiment writing its own output, which is the
#: one thing it exists to do.
_OUTPUT_ARTIFACTS = (
    "results.csv",
    "raw_runs.csv",
    "run_meta.json",
    # Written by the hold-out sweep into exp7_search_throughput/. It is a
    # run's output like any other, so regenerating it must not mark the tree
    # dirty -- and infra/fleet.sh's deploy-restore must preserve it for the same
    # reason. Keep the two lists in step.
    "lambda_sweep.csv",
)


def _is_own_output(path: str) -> bool:
    """True for a path that is a run's own output rather than its inputs."""
    parts = path.split("/")
    if parts[:1] == ["Plots"] and parts[1:2] == ["output"]:
        return True
    # Schemes/<scheme>/<exp-dir>/<artifact>
    return (
        len(parts) == 4
        and parts[0] == "Schemes"
        and parts[2].startswith("exp")
        and parts[3] in _OUTPUT_ARTIFACTS
    )


def _dirty_paths() -> List[str]:
    """Uncommitted paths that could actually change what a run measures.

    ``git status --porcelain`` over the whole repo, minus the run's own output.
    Anything else still counts -- source, config, dataset, infra -- so a genuinely
    modified tree is still caught.
    """
    # -uall: without it git collapses an untracked directory to a single
    # "Schemes/<scheme>/<exp-dir>/" entry, which _is_own_output cannot classify
    # (it has no filename) and which therefore counted as dirty. Measured on the
    # fleet: an untracked exp6 result dir marked a host dirty on its own.
    status = subprocess.run(
        ["git", "status", "--porcelain", "-uall"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    dirty = []
    for line in status.stdout.splitlines():
        if not line.strip():
            continue
        # "XY path" and "XY orig -> path" for renames; take the destination.
        path = line[3:].strip().split(" -> ")[-1].strip('"')
        if not _is_own_output(path):
            dirty.append(path)
    return dirty


def git_commit() -> str:
    """The commit that produced this result, or an explicit marker.

    ``unknown`` rather than a guess when git cannot answer: an invented hash in a
    provenance record is worse than an admitted gap. ``-dirty`` is appended when
    the tree has uncommitted changes, because a result from a modified tree cannot
    be reproduced from the commit alone -- EXCLUDING the run's own output, which
    every run rewrites and which therefore made the marker fire unconditionally.
    See :data:`_OUTPUT_ARTIFACTS`.
    """
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if commit.returncode != 0:
            return "unknown"
        sha = commit.stdout.strip()
        return f"{sha}-dirty" if _dirty_paths() else sha
    except Exception:
        return "unknown"


def library_versions() -> Dict[str, str]:
    """Versions of the libraries a measurement could depend on."""
    versions: Dict[str, str] = {}
    for name in ("numpy", "scipy", "bitarray", "mmh3", "cryptography", "pandas"):
        try:
            module = __import__(name)
            versions[name] = getattr(module, "__version__", "unknown")
        except Exception:
            versions[name] = "absent"
    return versions


@dataclass
class RunMetadata:
    """Everything global.yaml requires, plus why the run is or is not reportable."""

    scheme: str
    experiment: str
    started_utc: str
    finished_utc: Optional[str]
    git_commit: str
    python_version: str
    platform: str
    #: What the config PINNED, or "unknown" once c457e28 dropped the pin. It is
    #: not what the run executed on, so it cannot answer "which host produced
    #: this number" -- see ``experiment_host``.
    instance_type: str
    #: What the run ACTUALLY executed on, from the EC2 metadata service. Every
    #: baseline scheme has recorded this since c457e28; this scheme did not, so
    #: with the pin dropped its results carried no recoverable host at all. §VI
    #: discloses the host per scheme, which needs it recorded per run.
    experiment_host: Dict[str, Any]
    libraries: Dict[str, str]
    crypto_backends: Dict[str, Any]
    config_hashes: Dict[str, str]
    corpus_type: str
    corpus_sha256: Optional[str]
    dataset_records: Optional[int]
    thread_pinning: Dict[str, Optional[str]]
    runs: int
    warmups: int
    confidence: float
    reportable: bool
    not_reportable_because: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    #: The secondary metric names, in the order the experiment declares them.
    #:
    #: SINCE 2026-09-10 results.csv also carries them, as the column names
    #: themselves (``cross_node_forwards_mean``), so the plotter resolves a
    #: panel by name and this field is the cross-check rather than the only
    #: record. It remains load-bearing for BANKED files, whose columns are
    #: positional (``secondary_N_mean``) and whose meaning lives nowhere else.
    #:
    #: The positional layout made a panel label a claim nothing checked —
    #: Exp. 8's Fig. 8(c) was labelled
    #: "Cross-node forwards" while `cross_node_forwards` had been dropped from
    #: the metric list, so the column at that position was peak queue depth.
    #: Recording the names here lets Plots/generate_plots.py verify the label it
    #: is about to draw, and refuse rather than mislabel.
    secondary_metrics: List[str] = field(default_factory=list)

    #: WHICH CONSTRUCTION produced these numbers — ``option_d`` (``T = H(w)``)
    #: or ``psa`` (the manuscript's ``T = H(w ‖ PID ‖ PV ‖ Dom)``). The two time
    #: DIFFERENT functions at the same experiment number, so a results.csv that
    #: does not name its construction cannot be attributed to a scheme at all.
    #: It lived only in ``notes`` as free text, which no figure could check;
    #: Fig. 1 was drawn from ``psa`` while Figs. 2-8 were drawn from
    #: ``option_d`` and nothing in the artifacts said so.
    construction: str = "option_d"
    #: The ledger the run actually called (``memory`` or ``fabric``), from the
    #: same ``ABCD_LEDGER`` switch ``build_deployment`` reads. ``ledger_faithful``
    #: gates Exp. 4's reportability but was never recorded, so a banked Exp. 4
    #: number could not be attributed to a ledger after the fact.
    ledger_backend: str = "unknown"
    #: Digest of what this run MEASURES — see :func:`measurement_fingerprint`.
    #: Two artifacts with different fingerprints came from different
    #: measurements and must not be drawn on one axis.
    measurement_fingerprint: str = "unavailable"

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True) + "\n"

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        return path


def _expected_corpus_sha256() -> Optional[str]:
    """dataset.yaml's ``freeze.expected_corpus_sha256``, or None if unset.

    Returns None rather than raising when the config or key is absent: an
    unset pin means the campaign has not been frozen yet, which is a legitimate
    early state, not a reportability failure on its own.
    """
    try:
        from Common.crypto.config import load_dataset_config

        freeze = (load_dataset_config().get("freeze") or {})
        pinned = freeze.get("expected_corpus_sha256")
        return str(pinned) if pinned else None
    except Exception:  # noqa: BLE001 - a missing/unreadable pin must not crash a run
        return None


#: A result is superseded when the code that produced it is known to have had a
#: defect that changes what the experiment measures. This is a fact about this
#: repo's history, not a tunable, which is why it lives here rather than in
#: config: 5cf65f9 fixed three such defects in Exp. 7-8 -- every arm paid AASS's
#: cost vector, the queue feedback loop was dead so `least_loaded` collapsed onto
#: `no_lb`, and prepare() built 32 records against global.yaml's 10^5. Numbers from
#: before it are not comparable with numbers from after it.
#:
#: Judged at READ time from the commit the record already carries. A stamped
#: run_meta.json is never rewritten -- this module's contract is that provenance
#: is measured, never supplied, and editing a record to say what we now believe
#: would make it a statement of belief. So the record keeps saying what it said,
#: and the reader derives the consequence.
SUPERSEDED_BEFORE: Dict[int, str] = {
    7: "5cf65f9b8c0323252f604dd3ae2f8f0a599435b1",
    8: "5cf65f9b8c0323252f604dd3ae2f8f0a599435b1",
}


def _is_ancestor(older: str, newer: str) -> Optional[bool]:
    """True if ``older`` is an ancestor of ``newer``; None if git cannot say.

    None rather than False when the answer is unknown -- a shallow clone or a
    missing object must not silently downgrade to "not superseded".
    """
    try:
        proc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", older, newer],
            cwd=REPO_ROOT, capture_output=True, timeout=10,
        )
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None  # 128: unknown revision, shallow clone, not a repo


def superseded_reason(experiment_number: int, git_commit: str) -> Optional[str]:
    """Why an already-written result should not be quoted, or None.

    ``git_commit`` is taken verbatim from a run_meta.json, so it may carry the
    ``-dirty`` suffix; the suffix is stripped before the ancestry test because
    dirtiness is a separate question from staleness.
    """
    boundary = SUPERSEDED_BEFORE.get(experiment_number)
    if not boundary or not git_commit:
        return None
    sha = git_commit[:-len("-dirty")] if git_commit.endswith("-dirty") else git_commit
    if sha == "unknown":
        return (
            f"Exp. {experiment_number}: the producing commit is unknown, so it "
            f"cannot be shown to postdate {boundary[:9]}"
        )
    older = _is_ancestor(sha, boundary)
    if older is None:
        return (
            f"Exp. {experiment_number}: cannot determine whether {sha[:9]} "
            f"predates {boundary[:9]} (commit not present in this clone)"
        )
    if older and sha != boundary:
        return (
            f"Exp. {experiment_number}: produced at {sha[:9]}, which predates "
            f"{boundary[:9]} -- that commit fixed the shared costing overhead, "
            f"the dead queue loop and the 32-record index, so this result does "
            f"not measure what the experiment now measures"
        )
    return None


def _experiment_number(experiment: str) -> Optional[int]:
    """The sweep number an experiment directory belongs to, whatever its prefix.

    Every experiment-specific gate below used to match an exact Option D name
    (``"exp4_verification_overhead"``), so the policy-state-aware runs --
    ``psa_exp4_verification_overhead`` and the rest -- matched NONE of them and
    silently skipped the Fabric ledger check, the fixed-weights check and the
    independent-FSN-processes check. They came back ``reportable: true`` with an
    empty reason list because nothing had been asked of them.

    That was harmless only while the PSA track was a side experiment. It stops
    being harmless the moment those runs are the reported ones, so the gates key
    on the NUMBER and accept either prefix.
    """
    match = re.match(r"^(?:psa_)?exp(\d+)", experiment)
    return int(match.group(1)) if match else None


def reportability(
    config: scheme_config.Configuration,
    *,
    experiment: str,
    corpus_type: str,
    corpus_sha256: Optional[str],
    group_faithful: bool,
    token_scheme_keyed: Optional[bool],
    ledger_faithful: bool = False,
    fsn_processes: int = 0,
) -> Tuple[bool, List[str]]:
    """Every condition a quotable number must satisfy, and which ones failed.

    Each entry corresponds to a recorded author decision. Collected in
    one place so a runner cannot report a figure by forgetting a check, and so the
    reasons land in ``run_meta.json`` where a reader can see them.
    """
    reasons: List[str] = []

    host = verify_experiment_host()
    if not host["host_check_satisfied"]:
        reasons.append(
            f"not running on the pinned AWS experiment host: expected "
            f"{host['expected_instance_type']!r}, detected "
            f"{host['detected_instance_type'] or 'not EC2'!r} on "
            f"{host['platform']!r}"
        )

    if corpus_type not in config.corpus["reportable_types"]:
        reasons.append(
            f"corpus_type={corpus_type!r} is not reportable; dataset.yaml admits "
            f"only {list(config.corpus['reportable_types'])}"
        )
    if corpus_sha256 is None:
        reasons.append(
            "no corpus SHA-256: the corpus was not loaded and verified against "
            "the frozen pin"
        )
    else:
        # The message above promised a comparison against the frozen pin, but
        # nothing here performed one: a run against a DIFFERENT corpus than the
        # campaign was frozen on would have passed this gate and been marked
        # reportable, which is precisely the silent data swap
        # dataset.yaml's freeze pin exists to prevent (skill.md, "The corpus
        # is frozen"). Dataset/corpus.py guards its own loader, but a scheme
        # that obtains a digest another way bypassed that entirely. Checked
        # here so the gate matches what it claims. Added 2026-08-28.
        pinned = _expected_corpus_sha256()
        if pinned and pinned != corpus_sha256:
            reasons.append(
                f"corpus SHA-256 {corpus_sha256[:12]}... does not match "
                f"dataset.yaml's frozen pin {pinned[:12]}...; results from a "
                f"different corpus are not comparable to the campaign "
                f". Either restore the frozen corpus or re-freeze "
                f"and re-run EVERY scheme."
            )

    if not group_faithful:
        reasons.append(
            "the bilinear group came from an injected provider, not a faithful "
            "Type-III backend (crypto.yaml: backend_implemented: false)"
        )

    # Scoped to the experiments whose MEASURED path actually touches the chain,
    # rather than blanket. Exp. 1/2/3/5/6/7/8 never read or write the ledger on a
    # timed path, so blocking them on it would be a false blocker -- and a gate
    # that fires when it should not trains readers to ignore it.
    #
    # Exp. 6 WAS listed here (451df65, 2026-08-28) on the grounds that it "times
    # DIAS through to blockchain anchoring (global.yaml, Phase VII Step 5)". Removed
    # 2026-09-03: that justification cited a PROTOCOL STEP, not a measurement
    # boundary, and it does not hold against the code. ``sync/ias.py::synchronize``
    # takes ``ledger`` as OPTIONAL and anchors only inside ``if ledger is not
    # None``; Exp. 6's runner has never passed one, so Step 7 is not on its timed
    # path. Two independent sources agree it is outside the boundary: global.yaml
    # ends Exp. 6 at "until all affected FSNs report the new VID", and
    # ``tab:cost``'s authorization-synchronization row is O(delta)T_H +
    # O(log d)T_MT with no chain term. Exp. 4 keeps the gate because §5 puts
    # "chain consistency" INSIDE its boundary in as many words.
    #
    # §VI must state the exclusion and report anchoring separately -- the treatment
    # global.yaml already gives ML-KEM encapsulation in Exp. 1. If Phase VII Step 5
    # is ever brought inside the boundary, the runner must pass a ledger and
    # ``exp6_authorization_sync`` must come back into this tuple.
    if _experiment_number(experiment) == 4 and not ledger_faithful:
        # skill.md states the ledger is Hyperledger Fabric v2.5, but the
        # harness runs chain.ledger.InProcessLedger -- whose OWN docstring says
        # it is "NOT a substitute for Fabric once Fog Search Nodes become
        # independent processes" and that "Exp. 4 is where it starts to be
        # measured". Exp. 4 reports verification overhead (Merkle proof +
        # commitment recomputation + CHAIN CONSISTENCY), so an in-memory hash
        # chain understates the anchoring cost the paper claims. That gap was
        # documented in the ledger module but never reached run_meta.json, so a
        # figure could have been quoted without it travelling along. Added
        # 2026-08-28.
        reasons.append(
            "the ledger is an in-process hash chain, not the Hyperledger "
            "Fabric v2.5 deployment skill.md specifies; Exp. 4's chain-"
            "consistency cost is therefore understated (see "
            "chain/ledger.py::InProcessLedger)"
        )

    if token_scheme_keyed is False:
        reasons.append(
            "index tokens use an unkeyed H, which is invertible over the "
            "2,023-keyword vocabulary; the keyed/unkeyed choice is undecided"
        )
    elif token_scheme_keyed is None:
        reasons.append("no token scheme was resolved for this run")

    if _experiment_number(experiment) in (7, 8):
        if not config.scheduler.weights.is_fixed:
            reasons.append(
                f"AASS weights are {config.scheduler.weights.status!r}; "
                f"scheduler.yaml requires the documented hold-out sweep first"
            )
        # Was an UNCONDITIONAL blocker: the harness had no multi-process path,
        # so declaring the requirement in global.yaml could only ever fail it.
        # fsn/pool.py now runs one forked worker per node, so this checks
        # whether the run ACTUALLY used that path rather than whether the
        # requirement is declared. Passing fsn_processes=N (N>1) is the
        # evidence; the runner takes it from the live pool's worker PIDs, so it
        # cannot be asserted by a caller that did not spawn them.
        if config.topology.independent_processes and not fsn_processes:
            reasons.append(
                "skill.md requires each FSN to be an independent process; this "
                "run executed them in one interpreter, so a concurrency result "
                "would not measure the stated topology"
            )

    # WAS `!= 30`. Retargeted to 10 on the user's instruction, 2026-09-03, the
    # same day the campaign moved to 10 repetitions. Kept rather than removed:
    # this is the gate that stamps run_meta.json `reportable`, so its job is to
    # refuse any run whose replication count does not match what the manuscript
    # claims. That makes it the LAST line of defence against publishing a figure
    # whose n differs from section V's stated methodology.
    #
    # SECTION V MUST NOW SAY 10, NOT 30. If it still reads "the average of 30
    # independent runs" when the paper is submitted, this check is passing runs
    # that the text misdescribes -- which is the exact failure it exists to
    # prevent, just pointed the other way.
    #
    # Note the statistical consequence, which is not cosmetic: the 95% t
    # multiplier is 2.26 at n=10 against 2.05 at n=30, so every confidence
    # interval widens by roughly 10% before any change in the underlying
    # variance. Intervals in the new figures are not comparable to the banked
    # ones on width alone.
    if config.measurement.repetitions != 10:
        reasons.append(
            f"measurement.repetitions is {config.measurement.repetitions}, not 10"
        )

    # BLAS thread pinning. `config.verify_thread_pinning`'s own docstring says
    # "require=True is for reportable runs", but nothing ever called it that
    # way -- its only call site was a test passing require=False. So the pin was
    # configured in global.yaml (`blas_threads: 1`), exported by
    # provision.sh, RECORDED in run_meta.json by build_metadata below, and
    # gated nowhere. Every one of the 85 banked runs carries
    # `blas_thread_env: {OMP_NUM_THREADS: UNSET, ...}` and was still stamped
    # `reportable: true`.
    #
    # That matters because numpy's BLAS claims every core by default, so an
    # unpinned run's latency depends on the host's core count -- and this
    # campaign is no longer single-instance-type (`environment.instance_type`
    # was dropped 2026-08-30 so guo Exp. 2 could have the ~52 GB it needs).
    # Unpinned BLAS across 4-vCPU and 16-vCPU hosts is exactly the
    # cross-host incomparability the pin exists to remove.
    #
    # A BLOCKER rather than a note, matching the documented intent: an
    # unpinned run is discovered on its first point instead of after a
    # campaign. Export the variables before the run --
    # `export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
    #  NUMEXPR_NUM_THREADS=1 VECLIB_MAXIMUM_THREADS=1` -- or source
    # /etc/profile.d/malbpq-threads.sh, which a non-login ssh shell does not
    # read on its own.
    try:
        scheme_config.verify_thread_pinning(require=True)
    except scheme_config.ConfigError as exc:
        reasons.append(str(exc).splitlines()[0])

    return (not reasons), reasons


def measurement_fingerprint(experiment, construction: str) -> str:
    """A digest of WHAT THIS RUN MEASURES, so a stale artifact is detectable.

    Named columns (2026-09-10) closed most of the stale-data family: a metric
    added or renamed is now a MISSING COLUMN, which fails loudly. One case
    survives that — **names match, shape matches, behaviour changed underneath**.
    `exp5_keyword_update`'s `entries_rewritten` is the worked example: banked
    data reads 0.0 at every k, today's code returns 6 per record for the same
    call, and nothing in either artifact says they came from different code.

    This hashes the things that decide what a number MEANS: the construction,
    the metric names in order, the sweep values, and **the source of `prepare`
    and `measure`** — the part that actually changed in the `exp5` case and that
    no other field captures.

    Source text rather than a git commit, because a commit moves whenever
    anything in the repo moves: the 114 banked runs span four commits and most
    of those changes were irrelevant to the numbers. Hashing the measurement
    keeps the signal.

    Degrades to ``"unavailable"`` rather than raising — a provenance field must
    never be why a campaign dies.
    """
    import inspect

    parts = [f"construction={construction}"]
    primary = getattr(experiment, "primary", None)
    if primary is not None:
        parts.append(
            f"primary={getattr(primary, 'name', '')}:{getattr(primary, 'unit', '')}"
        )
    parts.append("secondaries=" + ",".join(
        f"{spec.name}:{getattr(spec, 'unit', '')}"
        for spec in getattr(experiment, "secondaries", ())
    ))
    parts.append("values=" + ",".join(
        str(v) for v in getattr(experiment, "values", ())
    ))
    for method in ("prepare", "measure"):
        fn = getattr(experiment, method, None)
        try:
            parts.append(f"{method}={inspect.getsource(fn)}" if fn else f"{method}=")
        except (OSError, TypeError):
            parts.append(f"{method}=unavailable")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def build_metadata(
    config: scheme_config.Configuration,
    *,
    experiment: str,
    corpus_type: str,
    corpus_sha256: Optional[str] = None,
    dataset_records: Optional[int] = None,
    group_faithful: bool = False,
    token_scheme_keyed: Optional[bool] = None,
    ledger_faithful: bool = False,
    fsn_processes: int = 0,
    runs: Optional[int] = None,
    warmups: Optional[int] = None,
    notes: Optional[List[str]] = None,
    secondary_metrics: Optional[Sequence[str]] = None,
    construction: str = "option_d",
    ledger_backend: str = "unknown",
    experiment_object: Any = None,
) -> RunMetadata:
    """Assemble ``run_meta.json`` at the start of a run."""
    reportable, reasons = reportability(
        config,
        experiment=experiment,
        corpus_type=corpus_type,
        corpus_sha256=corpus_sha256,
        group_faithful=group_faithful,
        token_scheme_keyed=token_scheme_keyed,
        ledger_faithful=ledger_faithful,
        fsn_processes=fsn_processes,
    )
    # THE HOST CAVEAT MUST TRAVEL WITH THE NUMBER.
    #
    # `verify_experiment_host()` is right to satisfy `host_check_satisfied`
    # vacuously when no pin is configured -- global.yaml dropped
    # `environment.instance_type` deliberately, so that guo (52 GB at N=10^6)
    # and thingom (vCPU-bound) can run on hosts sized for their work, and
    # failing the check would refuse every legitimate run.
    #
    # But the consequence was invisible: a run is stamped `reportable: true`
    # with NO host guarantee at all, while SVI states one `m6i.xlarge` for all
    # five schemes. The artifact recorded `pin_configured: false` three levels
    # down and nothing said what it meant. Recorded here as a note -- not a
    # blocking reason, because the pin was dropped on purpose -- so a reader
    # quoting the number sees the caveat attached to it.
    host_report = verify_experiment_host()
    run_notes = list(notes or ())
    if not host_report.get("pin_configured"):
        run_notes.append(
            "host NOT pinned: global.yaml sets no environment.instance_type, so "
            "the host check passed vacuously. Detected "
            f"{host_report.get('detected_instance_type') or 'no EC2 metadata'!r}. "
            "Cross-host latency comparisons are unsound until the pin returns; "
            "SVI must disclose the host per scheme rather than claim one type."
        )

    return RunMetadata(
        scheme=SCHEME_NAME,
        experiment=experiment,
        started_utc=datetime.now(timezone.utc).isoformat(),
        finished_utc=None,
        git_commit=git_commit(),
        python_version=platform.python_version(),
        platform=platform.platform(),
        instance_type=str(config.environment.get("instance_type", "unknown")),
        experiment_host=host_report,
        libraries=library_versions(),
        crypto_backends=environment_report(),
        config_hashes=scheme_config.config_hashes(),
        corpus_type=corpus_type,
        corpus_sha256=corpus_sha256,
        dataset_records=dataset_records,
        thread_pinning=scheme_config.thread_pinning_report(),
        runs=config.measurement.repetitions if runs is None else runs,
        warmups=config.measurement.warmup_runs if warmups is None else warmups,
        confidence=config.measurement.confidence_interval,
        reportable=reportable,
        not_reportable_because=reasons,
        notes=run_notes,
        secondary_metrics=list(secondary_metrics or ()),
        construction=construction,
        ledger_backend=ledger_backend,
        measurement_fingerprint=(
            measurement_fingerprint(experiment_object, construction)
            if experiment_object is not None else "unavailable"
        ),
    )


__all__ = [
    "SCHEME_NAME",
    "RunMetadata",
    "git_commit",
    "library_versions",
    "reportability",
    "build_metadata",
]
