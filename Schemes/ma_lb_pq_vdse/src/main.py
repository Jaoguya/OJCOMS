"""CLI entry point for the MA-LB-PQ-VDSE experiment harness.

Documented in ``skill.md``::

    python3 -m Schemes.ma_lb_pq_vdse.src.main --experiment all \
        --config "Experiment Configuration/global.yaml" \
        --dataset Dataset/derived --runs 10

Writes ``raw_runs.csv``, ``results.csv`` and ``run_meta.json`` into each
experiment's folder, per ``skill.md``.

**Nothing this produces is reportable today**, and the reasons travel with the
output rather than living only here: ``run_meta.json`` carries
``not_reportable_because``, populated by ``harness.provenance.reportability``.
The blockers — the missing v2 dataset manifest, the
absent Type-III pairing backend, the undecided keyed/unkeyed ``H``, and the
unswept λ weights. ``--require-reportable`` refuses to run rather than producing
output that could be mistaken for results.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from Schemes.ma_lb_pq_vdse.src import config as scheme_config  # noqa: E402
from Schemes.ma_lb_pq_vdse.src.harness import experiments as experiments_mod  # noqa: E402
from Schemes.ma_lb_pq_vdse.src.harness import psa_experiments as psa_mod  # noqa: E402
from infra import sweep  # noqa: E402
from Schemes.ma_lb_pq_vdse.src.harness import provenance, runner  # noqa: E402
from Schemes.ma_lb_pq_vdse.src.chain import select as chain_select  # noqa: E402

SCHEME_NAME = "ma_lb_pq_vdse"

#: Output folder per experiment — the paths ``Plots/generate_plots.py`` walks.
FOLDERS = {
    1: "exp1_trapdoor_generation",
    2: "exp2_search_latency",
    3: "exp3_crossdomain_scalability",
    4: "exp4_verification_overhead",
    5: "exp5_keyword_update",
    6: "exp6_authorization_sync",
    7: "exp7_search_throughput",
    8: "exp8_load_balance",
}

#: One construction, so one folder map. `PSA_FOLDERS` was a second name for
#: this same dict once the `psa_` prefix went; keeping both invites them to
#: drift apart.
PSA_FOLDERS = FOLDERS


def _run_notes(
    number: int, variant: str, *, construction: str, experiment=None
) -> List[str]:
    """What run_meta.json must record to keep a directory identifiable.

    The construction is recorded on EVERY run, including the default, because
    "no note" is what every banked Option D run already says and a reader
    cannot tell absence-of-note from not-yet-forked. Stating it always makes the
    two eras distinguishable without rewriting any existing run_meta.

    """
    notes = [f"construction={construction}"]
    # `scheduler_variant` is the key the plotter and every banked run already
    # use for "which arm produced this"; Exp. 1's |P_U| and Exp. 6's
    # propagation rule are arms in exactly that sense, whatever the key is
    # called. Renaming it would orphan 85 banked run_meta.json files.
    if variant and number in (1, 4, 6, 7, 8):
        notes.append(f"scheduler_variant={variant}")
    if construction == "psa":
        notes.append(
            "policy-state-aware construction (divergences D1-D9): "
            "T = H(w || PID || PV || Dom), PV a version-VECTOR digest"
        )
        if number == 4:
            # The LIVE selection, not a literal: a psa Exp. 4 stamped 'fabric'
            # must have talked to Fabric. chain_select.make_ledger() refuses to
            # fall back, so the two cannot disagree.
            notes.append(f"ledger_backend={chain_select.ledger_backend()}")
    return notes


def parse_experiments(value: str, construction: str = "psa") -> List[int]:
    """``all``, ``2``, or ``1,2,5`` — resolved against THIS construction's set.

    The two constructions do not cover the same experiment numbers: Option D has
    9 (tamper granularity, the Exp. 4 companion) and no 10; PSA has 10 (the D9
    token-count companion to §VI's Fig. 3) and no 9.

    This ignored ``construction`` and always returned ``sorted(FOLDERS)``, which
    was wrong in both directions once the sets diverged: ``--experiment all
    --construction psa`` tried 9, which has no PSA form and raises, and never
    reached 10 at all — so a full PSA campaign would silently omit an experiment
    while failing on one that does not exist for it.
    """
    known = PSA_FOLDERS if construction == "psa" else FOLDERS
    if value.strip().lower() == "all":
        return sorted(known)
    numbers = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or int(part) not in known:
            raise argparse.ArgumentTypeError(
                f"unknown experiment {part!r} for construction "
                f"{construction!r}; valid: {sorted(known)}"
            )
        numbers.append(int(part))
    if not numbers:
        raise argparse.ArgumentTypeError("no experiments selected")
    return sorted(set(numbers))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m Schemes.ma_lb_pq_vdse.src.main",
        description="Run the MA-LB-PQ-VDSE experiments.",
    )
    parser.add_argument("--experiment", default="all", help="all, 2, or 1,2,5")
    parser.add_argument("--config", default=None, help="path to global.yaml (informational)")
    parser.add_argument("--dataset", default=None, help="derived corpus directory")
    parser.add_argument(
        "--runs", type=int, default=None,
        help="retained runs per point (global.yaml fixes 10)",
    )
    parser.add_argument(
        "--warmups", type=int, default=None,
        help="discarded warm-up runs (global.yaml fixes 5)",
    )
    parser.add_argument(
        "--output", default=None,
        help="output root; defaults to this scheme's folder",
    )
    parser.add_argument(
        "--require-reportable", action="store_true",
        help="refuse to run unless every reportability condition holds",
    )
    parser.add_argument(
        "--smoke", action="store_true",
        help="one tiny sweep point and few runs, to exercise the pipeline",
    )
    parser.add_argument(
        "--construction", default="psa",
        choices=("psa",),
        help=(
            "kept for compatibility; there is one construction. The scheme "
            "implements the manuscript's policy-state-aware form and "
            "nothing else, so this accepts only 'psa'."
        ),
    )
    parser.add_argument(
        "--variant", default=None,
        help=(
            "ablation variant, or 'all' to run each in turn. Exp. 1 takes the "
            "AUTHORIZATION SCOPES (pu1, pu2, pu4, pu8; default pu1). Exp. 6 "
            "takes the DIAS PROPAGATION variants (dias, incremental_all, "
            "full_state; default dias) -- Section VI's DIAS, Incremental-All "
            "and Full-State Synchronization. Exp. 7-8 take the SCHEDULER "
            "variants (no_lb, round_robin, least_loaded, aass; default aass). "
            "Ignored for Exp. 2-5. "
            "The three vocabularies are not interchangeable: a scheduler "
            "decides which FSN serves a QUERY and has no effect on how an "
            "authorization change propagates."
        ),
    )
    parser.add_argument(
        "--points", default=None,
        help=(
            "run only these sweep values, so one experiment can be split "
            "across instances (e.g. '100,500' or '2-5'). Each shard writes to "
            "its own directory; infra/merge_points.py reassembles them. "
            "Exp. 7-8's 20 configs at ~0.9h each are the largest single job in "
            "the campaign, and this is what makes them parallel."
        ),
    )
    parser.add_argument("--quiet", action="store_true")
    return parser


def run(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    config = scheme_config.load()
    numbers = parse_experiments(args.experiment, "psa")
    scheme_root = Path(__file__).resolve().parent.parent
    output_root = Path(args.output) if args.output else scheme_root

    def log(message: str) -> None:
        if not args.quiet:
            print(message, flush=True)

    # --dataset used to be accepted and then silently ignored: the source was
    # hardcoded to SyntheticRecordSource, so every run stamped
    # corpus_type='synthetic' and could never be reportable no matter what the
    # caller passed or how the corpus freeze was resolved. Prefer the real
    # corpus, and fall back only when it genuinely cannot be read -- saying so
    # rather than degrading quietly.
    source: Any
    try:
        source = experiments_mod.CorpusRecordSource(corpus_dir=args.dataset)
        log(f"corpus: {source.corpus_type} "
            f"sha256={(source.corpus_sha256 or 'none')[:12]}... "
            f"|W_i|~{source.keywords_per_record} "
            f"domains={len(source.domains)} vocab={source.vocabulary}")
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        source = experiments_mod.SyntheticRecordSource()
        log(f"corpus unavailable ({type(exc).__name__}: {exc})")
        log("  falling back to SyntheticRecordSource -- runs will be marked "
            "NOT REPORTABLE (dataset.yaml admits only corpus_type 'synthea')")
        if args.require_reportable:
            log("REFUSED: --require-reportable was passed but the verified "
                "corpus could not be loaded")
            return 2

    # Determined from what the host can ACTUALLY build, not hardcoded. This was
    # pinned to False while no Type-III backend existed; leaving it hardcoded
    # after adding CharmType3Backend would have kept the scheme permanently
    # non-reportable for a reason that was no longer true.
    try:
        from Common.crypto import pairing as _pm

        _b = _pm.get_backend("ma_lb_pq_vdse", reportable=True)
        group_faithful = _b.pairing_type == "type-3"
        log(f"pairing: {_b.name} ({_b.pairing_type}, {_b.curve}) faithful={group_faithful}")
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        group_faithful = False
        log(f"pairing: no faithful Type-III backend ({type(exc).__name__}: {exc})")

    # Evidence for the skill.md topology gate. Independent FSN processes need
    # fork (fsn/pool.py); on a platform without it the harness falls back to the
    # single-interpreter path, and reporting a process count here would assert a
    # topology the run did not use.
    import multiprocessing as _mp

    if config.topology.independent_processes and "fork" in _mp.get_all_start_methods():
        fsn_processes = int(config.topology.fog_search_nodes)
        log(f"topology: {fsn_processes} independent FSN processes (fork)")
    else:
        fsn_processes = 0
        log("topology: single interpreter — Exp. 7-8 will not be reportable")

    runs = args.runs if args.runs is not None else config.measurement.repetitions
    warmups = (
        args.warmups if args.warmups is not None else config.measurement.warmup_runs
    )
    if args.smoke:
        runs, warmups = min(runs, 3), min(warmups, 1)

    from Schemes.ma_lb_pq_vdse.src.scheduler import aass as _aass
    ALL_VARIANTS = (_aass.VARIANT_NO_LB, _aass.VARIANT_ROUND_ROBIN,
                    _aass.VARIANT_LEAST_LOADED, _aass.VARIANT_AASS)

    def _variants_for(number: int):
        """Which variants to run for this experiment.

        Exp. 6 and Exp. 7-8 have DIFFERENT variant vocabularies. Exp. 7-8 ablate
        the SCHEDULER (no_lb / round_robin / least_loaded / aass); Exp. 6 ablates
        DIAS PROPAGATION (ias / broadcast / full_rebuild). They were previously
        conflated: passing a scheduler variant to Exp. 6 ran the same measurement
        under a different directory name, because the scheduler decides which FSN
        serves a QUERY and plays no part in propagating an authorization change.
        The stale ``exp6_authorization_sync__no_lb`` directory is what that
        produced -- within 3% of the main run at every point.
        """
        if number == 4:
            # Exp. 4 is ONE experiment with two arms, because SVI's Fig. 4 makes
            # one claim in two panels over two different variables: (a) sweeps
            # `r` and asks what verification costs, (b) pins `r` and sweeps the
            # tamper count `t`, asking what it buys. Default runs both, because
            # a figure with one panel missing is not the figure SVI describes.
            if args.variant in (None, "") or args.variant.lower() == "all":
                return [""] + list(psa_mod.PSA_EXP4_VARIANTS)
            picked = [v.strip() for v in args.variant.split(",") if v.strip()]
            bad = [v for v in picked if v and v not in psa_mod.PSA_EXP4_VARIANTS]
            if bad:
                raise SystemExit(
                    f"unknown Exp. 4 arm(s) {bad}; valid: "
                    f"{', '.join(psa_mod.PSA_EXP4_VARIANTS)}, or 'all'"
                )
            return picked
        if number == 1:
            # §VI varies q AND |P_U|; the runner sweeps one variable, so |P_U|
            # is the arm. Defaults to every scope, because a single-arm run
            # would silently reproduce the D7 defect it exists to fix.
            if args.variant in (None, "") or args.variant.lower() == "all":
                return list(psa_mod.PSA_EXP1_VARIANTS)
            picked = [v.strip() for v in args.variant.split(",") if v.strip()]
            bad = [v for v in picked if v not in psa_mod.PSA_EXP1_VARIANTS]
            if bad:
                raise SystemExit(
                    f"unknown PSA Exp. 1 variant(s) {bad}; valid: "
                    f"{', '.join(psa_mod.PSA_EXP1_VARIANTS)}"
                )
            return picked
        if number == 6:
            if True:
                if args.variant in (None, ""):
                    return [psa_mod.VARIANT_DIAS]
                if args.variant.lower() == "all":
                    return list(psa_mod.PSA_EXP6_VARIANTS)
                picked = [v.strip() for v in args.variant.split(",") if v.strip()]
                bad = [v for v in picked if v not in psa_mod.PSA_EXP6_VARIANTS]
                if bad:
                    raise SystemExit(
                        f"unknown PSA Exp. 6 variant(s) {bad}; valid: "
                        f"{', '.join(psa_mod.PSA_EXP6_VARIANTS)}"
                    )
                return picked
            if args.variant in (None, ""):
                return [experiments_mod.VARIANT_DIAS]
            if args.variant.lower() == "all":
                return list(experiments_mod.EXP6_VARIANTS)
            chosen = [v.strip() for v in args.variant.split(",") if v.strip()]
            unknown = [
                v for v in chosen if v not in experiments_mod.EXP6_VARIANTS
            ]
            if unknown:
                raise SystemExit(
                    f"unknown Exp. 6 variant(s) {unknown}; valid: "
                    f"{', '.join(experiments_mod.EXP6_VARIANTS)}, or 'all'. "
                    f"The scheduler variants ({', '.join(ALL_VARIANTS)}) belong "
                    f"to Exp. 7-8 and have no effect on DIAS propagation."
                )
            return chosen
        if number not in (7, 8):
            return [None]                 # scheduler is not on their path
        if args.variant in (None, ""):
            return [_aass.VARIANT_AASS]   # unchanged default
        if args.variant.lower() == "all":
            return list(ALL_VARIANTS)
        chosen = [v.strip() for v in args.variant.split(",") if v.strip()]
        unknown = [v for v in chosen if v not in ALL_VARIANTS]
        if unknown:
            raise SystemExit(
                f"unknown scheduler variant(s) {unknown}; "
                f"valid: {', '.join(ALL_VARIANTS)}, or 'all'"
            )
        return chosen

    # One construction: the manuscript's. Kept as a name rather than
    # inlined, because the branches below read as what they are.
    psa = True
    if psa:
        unsupported = [n for n in numbers if n not in PSA_FOLDERS]
        if unsupported:
            log(
                f"REFUSED: --construction psa covers experiments "
                f"{sorted(PSA_FOLDERS)}; {unsupported} have no "
                f"policy-state-aware form. D1-D9 are construction and "
                f"experiment-design divergences, not a fork of the whole "
                f"harness -- see harness/psa_experiments.py."
            )
            return 2

    exit_code = 0
    for number in numbers:
      for variant in _variants_for(number):
        if psa:
            experiment = psa_mod.build(
                number, config, variant=variant or "", source=source
            )
        else:
            experiment = experiments_mod.build_experiment(
                number, config, source, variant=variant
            )
        # A PSA experiment builds its world in-process (`build_world`) and
        # never reads the corpus, so it must NOT inherit the outer source's
        # provenance. On the dev host that was harmless -- the corpus is absent
        # and `corpus_type` was already 'synthetic' -- but on the CAMPAIGN host
        # the corpus IS present, so these runs came back stamped
        # `corpus_type: synthea`, carrying a corpus SHA they never used, and
        # `reportable: true`. psa_experiments.py's docstring asserts the
        # opposite ("reportability() refuses it"), and that assertion was only
        # ever true by accident of the corpus being missing.
        # A corpus-backed PSA experiment reads the SAME records Option D reads,
        # so it inherits the corpus provenance and can be reportable. One that
        # builds its world with `build_world()` cannot, and says so.
        psa_corpus = (
            source.corpus_type
            if getattr(experiment, "CORPUS_BACKED", False)
            else "psa_in_process"
        )
        metadata = provenance.build_metadata(
            config,
            experiment=experiment.name,
            corpus_type=psa_corpus if psa else source.corpus_type,
            corpus_sha256=(
                source.corpus_sha256
                if not psa or getattr(experiment, "CORPUS_BACKED", False)
                else None
            ),
            group_faithful=group_faithful,
            fsn_processes=fsn_processes,
            token_scheme_keyed=True,
            # Must describe the object the experiment CALLED, not a
            # wish: read from the same switch build_deployment uses.
            ledger_faithful=chain_select.ledger_is_faithful(),
            runs=runs,
            warmups=warmups,
            # Which scheduler produced these numbers. Exp. 7-8 is a four-way
            # ablation, so a result that does not name its variant is
            # unidentifiable the moment the directory is renamed or merged.
            notes=_run_notes(
                number, variant,
                construction=args.construction, experiment=experiment,
            ),
            # The secondary column ORDER, so a figure panel can verify the
            # metric it is about to draw against the name that produced it
            # rather than trusting a position. See RunMetadata.secondary_metrics.
            secondary_metrics=[
                spec.name for spec in getattr(experiment, "secondaries", ())
            ],
            # Machine-readable, not a note: a figure must be able to refuse to
            # draw two constructions on one axis, and free text in `notes`
            # cannot be checked. See RunMetadata.construction.
            construction=args.construction,
            ledger_backend=chain_select.ledger_backend(),
            # The experiment ITSELF, so provenance can fingerprint the source of
            # `prepare`/`measure`. Passing the object rather than a precomputed
            # digest keeps the definition of "what this measures" in one place.
            experiment_object=experiment,
        )
        if args.require_reportable and not metadata.reportable:
            log(f"REFUSED {experiment.name}: not reportable")
            for reason in metadata.not_reportable_because:
                log(f"  - {reason}")
            exit_code = 2
            continue

        if args.smoke:
            values = experiment.values[:1]
        else:
            try:
                selected = sweep.select(experiment.values, args.points)
            except sweep.SweepSelectionError as exc:
                log(f"REFUSED {experiment.name}: {exc}")
                exit_code = 2
                continue
            values = None if selected == list(experiment.values) else selected
        log(
            f"{experiment.name}: {experiment.variable} over "
            f"{list(values or experiment.values)}, {runs} runs "
            f"({warmups} warm-ups)"
        )
        if not metadata.reportable:
            log("  NOT REPORTABLE — recorded in run_meta.json:")
            for reason in metadata.not_reportable_because:
                log(f"    - {reason}")

        result = runner.run_experiment(
            experiment, metadata, config=config, scheme=SCHEME_NAME, values=values
        )
        # Variant goes in the directory name: four schedulers writing to one
        # exp7 folder would silently overwrite each other and leave a single
        # curve labelled as an ablation.
        folders = PSA_FOLDERS if psa else FOLDERS
        out_dir = sweep.shard_dir(output_root / folders[number], args.points)
        variant_dir_numbers = (1, 4, 6, 7, 8) if psa else (6, 7, 8)
        if variant and number in variant_dir_numbers:
            out_dir = out_dir.parent / f"{out_dir.name}__{variant}"
        written = runner.write_outputs(result, out_dir)
        for point in result.points:
            log(
                f"  {experiment.variable}={point.variable_value}: "
                f"{point.primary.mean:.4f} ± {point.primary.ci95:.4f} "
                f"{experiment.primary.unit} (n={point.retained}"
                + (f", {point.failed} failed" if point.failed else "")
                + ")"
            )
        for warning in result.warnings:
            log(f"  WARNING {warning}")
        log(f"  wrote {', '.join(sorted(p.name for p in written.values()))}")

    return exit_code


def _force_utf8_stdout() -> None:
    """Make console output encoding-independent.

    The runners print check marks, box-drawing characters and Greek letters.
    On Linux stdout is UTF-8 and these are fine; a Windows console defaults to
    cp1252 and the first such character raises UnicodeEncodeError mid-run —
    which killed a guo run after Exp. 1 had already completed and written its
    results. Reconfiguring leaves Linux output byte-identical.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass  # already-wrapped or non-reconfigurable stream


if __name__ == "__main__":
    _force_utf8_stdout()
    raise SystemExit(run())
