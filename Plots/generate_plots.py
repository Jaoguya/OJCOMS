"""Generate the manuscript's eight figures from the schemes' ``results.csv``.

    python3 Plots/generate_plots.py --input Schemes --output Plots/output

Walks ``Schemes/*/exp<N>_*/results.csv`` and emits one figure per experiment
(SystemConfiguration.md). Schemes with no ``results.csv`` for an experiment are skipped,
so a partial campaign still plots — that is deliberate: the campaign runs
per-scheme on separate instances and finishes at different times.

ONE FIGURE FAMILY
-----------------
Section VI's eight figures, drawn from ``Schemes/<scheme>/exp<N>_*/``.

There used to be two, because the proposed scheme carried two constructions
writing to ``exp<N>_*/`` and ``psa_exp<N>_*/``. It implements the manuscript's
policy-state-aware form and nothing else as of 2026-09-12, the prefix is gone,
and all five schemes now write the same directory names. ``--construction`` is
kept and accepts only ``psa``.

FIGURE CONVENTIONS (SystemConfiguration.md, followed exactly)
-------------------------------------------------
* Vector PDF, single-column width.
* 8 pt minimum type size anywhere on the figure.
* 95% CI error bars on **every** point, taken from the ``*_ci95`` columns —
  never recomputed here, because this script does not see the raw runs.
* Log x-axis for Exp. 2, 5 and 6 (their sweeps span decades).
* Schemes distinguished by **both** marker and line style, so the figures
  survive grayscale printing.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO
-----------------------------------------
It does not aggregate, derive, interpolate or smooth. Every plotted value is
read verbatim from a ``results.csv`` cell, so that SystemConfiguration.md's "every numeric
claim in §VI traces to a results.csv cell" stays literally true. A missing or
malformed row is reported and skipped, never filled in.

It also does not read ``run_meta.json``'s ``reportable`` flag to *exclude*
anything — a development run is still worth plotting while building the
pipeline. Instead, ``--require-reportable`` opts into that check, and without
it the script prints a warning naming every non-reportable series it drew, so
a development figure cannot be mistaken for a submission one.
"""

from __future__ import annotations

import argparse
import math
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import matplotlib

matplotlib.use("Agg")  # headless: the experiment host has no display
import matplotlib.pyplot as plt  # noqa: E402


# ---------------------------------------------------------------------------
# Figure specifications — global.yaml (metrics) and §10 (filenames, log axes)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PanelSpec:
    """One sub-plot of a multi-panel figure.

    ``metric`` indexes the results.csv columns: 0 is ``primary_mean``, 1 is the
    first secondary, 2 the second. ``tag`` is the (a)/(b)/(c) label.
    """
    metric: int
    ylabel: str
    tag: str
    #: The metric name this panel MUST be drawing, checked against
    #: run_meta.json's `secondary_metrics` before the panel is rendered. Set it
    #: on any secondary panel whose label names a specific quantity; a mismatch
    #: raises rather than drawing the wrong column under the right label. Left
    #: unset (None) the panel is drawn unverified, which is what a pre-2026-09-07
    #: run_meta without the field forces anyway.
    metric_name: Optional[str] = None
    #: Opt a SECONDARY panel into a log y-axis. Off by default because a
    #: secondary is a different quantity from the primary and may be zero or
    #: narrow-ranging; set it only where the panel's own values are positive
    #: and span enough to flatten on a linear axis.
    log_y: bool = False
    #: Draw this panel from a DIFFERENT experiment folder. Exp. 4 needs it: the
    #: figure's two panels answer "what does verification cost" and "what does
    #: it buy", and the second is measured by a separate sweep over tampered
    #: records. When any panel sets this, the panels no longer share an x-axis
    #: -- they are different variables (`r` against `t`) and overlaying them
    #: would be a category error.
    folder: Optional[str] = None
    #: Per-panel x label and x scale, used only when `folder` is set.
    xlabel: Optional[str] = None
    log_x: bool = False
    #: Divide the metric by the x value before plotting. Section VI reports
    #: Exp. 4's latency as T_avg = T_verify / r, the per-returned-ciphertext
    #: cost, so the panel must show that rather than the total the CSV holds.
    #: Derived here rather than in the runner so `results.csv` keeps the raw
    #: measurement and the figure states the transform in one place.
    per_x: bool = False
    #: Draw a SECOND metric of the same series as a companion curve, with
    #: `companion_label` naming it. For a panel whose claim is a contrast
    #: between two columns of ONE run rather than between two schemes: Exp. 3
    #: records both `tokens_issued` and `option_d_tokens_issued` on every run,
    #: and the D9 claim is precisely the gap between them. Drawn dashed and
    #: grey so it reads as the reference line it is, never as a fifth scheme.
    companion_metric: Optional[int] = None
    companion_label: str = ""
    #: Draw a WITHIN-SCHEME parameter sweep assembled from several arm folders.
    #:
    #: Exp. 1 needs it. The proposed scheme measures four authorization scopes
    #: in `exp1_trapdoor_generation__pu<N>/`, each a full sweep over `q`. Those
    #: four curves used to be drawn on the cross-scheme figure alongside four
    #: baselines -- eight series in an IEEE single column, and worse than
    #: cluttered: four of them were one scheme at four scopes and four were
    #: four different schemes, so the reader could not tell which spread meant
    #: "scheme A vs scheme B" and which meant "the same scheme paying more".
    #: A within-scheme parameter does not belong on a between-scheme axis.
    #:
    #: This panel takes ONE point from each arm -- the value at `scope_at_x` --
    #: and plots it against the arm's own parameter, giving `|P_U|` its own
    #: axis. `scope_arms` maps folder suffix to that numeric parameter.
    scope_arms: Tuple[Tuple[str, float], ...] = ()
    scope_at_x: Optional[float] = None
    scope_scheme: str = ""


@dataclass(frozen=True)
class ExperimentSpec:
    number: int
    folder: str
    filename: str
    xlabel: str
    ylabel: str
    log_x: bool = False
    log_y: bool = False
    #: Empty for a normal one-metric figure. When set, the figure is drawn as
    #: one stacked panel per entry, sharing the x-axis and one legend.
    #:
    #: Exp. 8 needs this: "load balance" is not one number. `least_loaded`
    #: minimises queue length, so it wins on utilization spread by
    #: construction, while AASS trades some spread for authorization locality
    #: and wins on peak node load and on cross-node traffic. Plotting only the
    #: spread shows the one metric the proposed scheduler loses; plotting only
    #: a metric it wins would be choosing the metric after seeing the result.
    #: All three are recorded on every run, so all three are shown.
    panels: Tuple["PanelSpec", ...] = ()
    #: Directory-name prefix. Empty for the implemented scheme, whose folders
    #: are `exp<N>_*` for every scheme. Retained because `proposed_prefix`
    #: uses the mechanism; with one construction it is empty everywhere.
    #: A prefix rather than a `__psa` suffix for the reason main.py gives: the
    #: two constructions time DIFFERENT functions at the same experiment
    #: number, so they must never fall into one glob and be averaged or
    #: overlaid as if they were arms of one measurement.
    prefix: str = ""
    #: Override the ablation vocabulary. Empty means `variants_for()` decides.
    variants: Tuple[Tuple[str, str], ...] = ()
    #: Directory prefix used for the PROPOSED scheme only, leaving the baselines
    #: on their own `exp<N>_*` folders. §VI Exp. 1 compares the proposed
    #: construction against four baselines AND sweeps |P_U|, so its figure needs
    #: the proposed curves from `exp1_trapdoor_generation__pu<N>/` and the baselines from
    #: `<scheme>/exp1_*/` in ONE plot. A single `prefix` cannot express that: it
    #: applies to every scheme, and the baselines have no `psa_` directories.
    proposed_prefix: str = ""
    #: Arms of the PROPOSED scheme drawn as separate curves alongside the
    #: baselines. Set together with `proposed_prefix`; empty means the proposed
    #: scheme contributes one curve like everyone else.
    proposed_variants: Tuple[Tuple[str, str], ...] = ()
    #: Draw only these sweep values, whatever a results.csv holds. §VI Exp. 1
    #: states q in {1,5,10,15,20} -- five points -- but the four baselines were
    #: measured at every integer 1..20, so the figure would otherwise show four
    #: 20-point curves against a 5-point proposed one. The extra points are a
    #: superset, not different data: 1, 5, 10, 15 and 20 are all present in
    #: every baseline's results.csv, and nothing measured is discarded from the
    #: file -- only from the plot.
    restrict_x: Tuple[float, ...] = ()


#: §VI Exp. 1's second sweep dimension: "|P_U| is varied as {1,2,4,8}".
#: Defined here rather than in the PSA block below because the MANUSCRIPT
#: Exp. 1 figure needs it -- the proposed curves are one per scope.
#: ONE CURVE ON THE FIGURE, 2026-09-13. Exp. 1 still MEASURES all four
#: authorization scopes -- `global.yaml` still declares
#: `policy_scopes: [1,2,4,8]`, the runner still writes
#: `exp1_trapdoor_generation__pu<N>/` for each, and nothing measured is
#: discarded. Only the PLOT changed: four proposed curves against each
#: baseline's one made Fig. 1 an eight-series figure that read as eight
#: unrelated schemes, so it now draws the |P_U| = 1 scope alone, labelled
#: "Proposed" like every other single-curve figure.
#:
#: This is a presentation choice and nothing else. The other three scopes stay
#: in their directories and in `results.csv`; to draw them again, restore the
#: three commented entries below. `plotgen.md` records the same rule for
#: `restrict_x`: nothing measured is discarded from any file, only from the
#: plot.
PSA_EXP1_VARIANTS: Tuple[Tuple[str, str], ...] = (
    ("pu1", "Proposed"),
    # ("pu2", "$|P_U| = 2$"),
    # ("pu4", "$|P_U| = 4$"),
    # ("pu8", "$|P_U| = 8$"),
)


EXPERIMENTS: Tuple[ExperimentSpec, ...] = (
    # TWO PANELS, because SVI Exp. 1 asks two questions that do not share an
    # axis. (a) is the between-scheme comparison over q; (b) is the
    # within-scheme cost of widening the authorization scope.
    #
    # Both used to be panel (a): the proposed scheme drew one curve per |P_U|
    # from exp1_trapdoor_generation__pu<N>/ alongside four baselines. Eight
    # series in an IEEE single column, and worse than cluttered -- four of them
    # were ONE scheme at four scopes and four were four different schemes, so a
    # reader could not tell which spread meant "scheme A vs scheme B" and which
    # meant "the same scheme paying more". A within-scheme parameter does not
    # belong on a between-scheme axis.
    #
    # Panel (b) is also where the |P_U| half of tab:cost's O(|T_Q|)T_H row
    # becomes checkable: |T_Q| = q|P_U|, so at fixed q the latency must be
    # linear in |P_U|. Measured 0.0349 / 0.0655 / 0.1288 / 0.2606 ms at q=5 --
    # ratios 1.88, 1.97, 2.02. Nothing is re-run; all four arms were already
    # measured and stay on disk.
    ExperimentSpec(1, "exp1_trapdoor_generation", "fig_exp1_trapdoor.pdf",
                   "Queried keywords $q$", "Token generation latency (ms)",
                   log_y=True,   # 4.82 decades — see LOG_Y_DECADES
                   proposed_prefix="",
                   proposed_variants=PSA_EXP1_VARIANTS,
                   restrict_x=(1, 5, 10, 15, 20),
                   panels=(
                       PanelSpec(0, "Token generation\nlatency (ms)", "a"),
                       PanelSpec(0, "Token generation\nlatency (ms)", "b",
                                 scope_arms=(("pu1", 1), ("pu2", 2),
                                             ("pu4", 4), ("pu8", 8)),
                                 scope_at_x=5,
                                 scope_scheme="Proposed",
                                 xlabel=r"Authorized policies $|\mathcal{P}_U|$ (at $q=5$)",
                                 # LOG-LOG. |P_U| is sampled geometrically
                                 # (1,2,4,8) and |T_Q| = q|P_U| is LINEAR in
                                 # it, so only log-log renders that as a
                                 # straight line -- on a linear x the four
                                 # points bunch at the left and the linear law
                                 # reads as a curve, which is the opposite of
                                 # what the panel exists to show.
                                 log_x=True,
                                 log_y=True),
                   )),
    ExperimentSpec(2, "exp2_search_latency", "fig_exp2_search.pdf",
                   "Index size $N$ (records)", "Search latency (ms)",
                   log_x=True, log_y=True,
                   proposed_prefix=""),
    ExperimentSpec(3, "exp3_crossdomain_scalability", "fig_exp3_crossdomain.pdf",
                   "Domains $d$", "Cross-domain search latency (ms)",
                   log_y=True,
                   proposed_prefix=""),
    # TWO PANELS. Exp. 4 asks what verification COSTS and what it BUYS, and the
    # second question is a different sweep: `r` returned ciphertexts against `t`
    # tampered ones. Merged 2026-09-05 -- panel (b) was a standalone Exp. 9
    # figure, but Section VI makes one claim out of the pair (a moderate
    # per-result cost bought with per-result localization), so splitting them
    # across two figures asked the reader to join them up.
    #
    # Panel (a) is T_avg = T_verify / r, which is what Section VI reports.
    # Panel (b) is records discarded, NOT records retained: retention is 0 for
    # both baselines and a log axis cannot draw a zero, so the complement is
    # what stays plottable. It carries the same fact -- discarding exactly `t`
    # is localizing exactly `t` and retaining the rest.
    ExperimentSpec(4, "exp4_verification_overhead", "fig_exp4_verify.pdf",
                   "Returned results $r$", "Verification latency (ms)",
                   log_y=True,   # 2.66 decades — see LOG_Y_DECADES
                   panels=(
                       PanelSpec(0, "Verification latency\nper result (ms)", "a",
                                 per_x=True, log_x=True),
                       # FOLDED 2026-09-12. Panel (b) reads Exp. 4's own
                       # `granularity` ARM, not a separate experiment: SVI has
                       # no Experiment 9, it has one Exp. 4 whose figure has two
                       # panels over two variables.
                       PanelSpec(0, "Records discarded", "b",
                                 folder="exp4_verification_overhead__granularity",
                                 xlabel="Tampered records $t$",
                                 log_x=True, log_y=True),
                   ),
                   proposed_prefix=""),
    ExperimentSpec(5, "exp5_keyword_update", "fig_exp5_update.pdf",
                   "Updated (keyword, document) pairs $k$", "Update latency (ms)",
                   log_x=True, log_y=True,
                   proposed_prefix=""),
    # TWO PANELS, because Exp. 6's ablation makes two DIFFERENT claims and
    # only one of them is visible in latency.
    #
    # `full_rebuild` is 1.40-1.44x `ias` at every delta, so panel (a) carries
    # the INCREMENTAL half. `broadcast` is NOT distinguishable from `ias` in
    # latency -- the campaign measured +2%, a local rerun measured -4.5%, i.e.
    # noise in both directions -- because every FSN is an object in ONE
    # interpreter, so delivering to four of them costs essentially nothing and
    # the per-update cost is all sender-side (authorization evolution, index
    # evolution, Merkle path update, message build). A latency-only figure
    # would leave the SELECTIVE half of the claim with no evidence at all,
    # which is exactly what SystemConfiguration.md's "selective propagation"
    # asks the experiment to show.
    #
    # Panel (b) is the DELIVERED PAYLOAD: bytes leaving the AIM per update,
    # `delivered_kb` (secondary_3), MEASURED by the runner. It was derived here
    # as secondary_1 x secondary_2 until 2026-09-04, which was wrong for
    # `full_rebuild`: only 1 of its 37 deliveries is a DIAS message and the
    # other 36 are AuthorizationMeta republishes at ~a third the size, so the
    # product charged it ~3x the bytes it sends. Bytes rather than a node count
    # because the quantity the selective claim is about is network load, and
    # "4 nodes" only becomes a cost once multiplied by what each node is sent.
    #
    # Section VI must state that the selective saving is in DELIVERY VOLUME, not
    # in sender-side latency, and why: an in-process harness models no network.
    ExperimentSpec(6, "exp6_authorization_sync", "fig_exp6_sync.pdf",
                   "Authorization updates $\\delta$", "Synchronization latency (ms)",
                   log_x=True, log_y=True,
                   panels=(
                       PanelSpec(0, "Synchronization latency (ms)", "a"),
                       # Log, or the 4x that IS the selective claim (0.204 vs
                       # 0.816 KB) is squashed against the axis by
                       # full_rebuild's larger payload. On log the three sit
                       # evenly apart and both gaps read at a glance.
                       PanelSpec(3, "DIAS payload delivered (KB)", "b",
                                 log_y=True),
                   ),
                   proposed_prefix=""),
    ExperimentSpec(7, "exp7_search_throughput", "fig_exp7_throughput.pdf",
                   "Concurrent queries", "Throughput (queries/s)",
                   proposed_prefix=""),
    # Exp. 9 is the Exp. 4 companion: Exp. 4 asks what verification COSTS,
    # Exp. 9 what it BUYS. Log-log because the gap is the story -- ours tracks
    # t exactly while the accumulator schemes sit flat at the full result-set
    # size, so at t=1 the two are ~4 orders apart and at t=1000 ~1.
    # `metric_name` on every secondary panel: the index says WHERE to read and
    # the name says WHAT must be there, checked against run_meta.json before the
    # panel is drawn. Panel (c) is why -- it carried this label for runs whose
    # secondary_2 was peak queue depth, because `cross_node_forwards` had been
    # dropped from the metric list and nothing tied the label to the column.
    ExperimentSpec(8, "exp8_load_balance", "fig_exp8_balance.pdf",
                   "Concurrent queries", "FSN utilization std. dev.",
                   panels=(
                       PanelSpec(0, "Utilization std. dev.", "a"),
                       PanelSpec(1, "Max node utilization", "b",
                                 metric_name="max_node_utilization"),
                       PanelSpec(2, "Cross-node forwards", "c",
                                 metric_name="cross_node_forwards"),
                   ),
                   proposed_prefix=""),
)

#: Exp. 6's arms, in the manuscript's own words.
PSA_EXP6_VARIANTS: Tuple[Tuple[str, str], ...] = (
    ("incremental_all", "Incremental-All"),
    ("full_state", "Full-State Synchronization"),
    ("dias", "DIAS (proposed)"),
)



#: One family: the manuscript's eight figures. The key is kept so
#: `--construction psa` stays accepted.
CONSTRUCTIONS: Dict[str, Tuple[ExperimentSpec, ...]] = {
    "psa": EXPERIMENTS,
}


# LOG-Y CRITERION, applied uniformly: an experiment gets a log y-axis when its
# measured values span >= LOG_Y_DECADES orders of magnitude. On a linear axis a
# wider span collapses every curve but the slowest onto the x-axis, which hides
# real differences rather than showing them.
#
# Stated as a threshold, not chosen per figure, so it cannot be an axis picked
# after seeing which scheme it flatters (AGENT_RULES "Bias Detection"). Measured
# spans at the time of writing:
#
#   exp1 4.82   exp2 8.10   exp3 6.29   exp4 2.66
#   exp5 4.41   exp6 3.01   exp7 0.99   exp8 0.93 / 0.11 / inf(zeros)
#
# so 1-6 are log and 7-8 are linear. exp1 and exp4 were LINEAR until
# 2026-09-01: exp1 put four of five schemes flat on the axis (the proposed
# scheme's 0.01-0.10 ms was indistinguishable from Guo's and Perera's), and
# exp4 hid the 0.06-28 ms spread the same way. `_check_log_y_criterion` warns
# if new data ever pushes a linear figure past the threshold, so the rule stays
# enforced rather than becoming a comment about what was once true.
#: Reportable repetition count, read from the campaign config rather than
#: hardcoded here. `Experiment Configuration/global.yaml` is the single source of
#: truth; a literal in this file is how the n_runs warning kept
#: citing 30 after the campaign moved to 10. yaml is not imported at module
#: scope because this script must run in a bare matplotlib environment, so the
#: value is parsed with a regex and falls back to the documented default.
def _required_repetitions(default: int = 10) -> int:
    config = (Path(__file__).resolve().parents[1]
              / "Experiment Configuration" / "global.yaml")
    try:
        match = re.search(r"^\s*repetitions:\s*(\d+)",
                          config.read_text(encoding="utf-8"), re.MULTILINE)
    except OSError:
        return default
    return int(match.group(1)) if match else default


LOG_Y_DECADES = 2.0


# Display names. Anything not listed falls back to the directory name, so a
# newly added scheme still plots (with an uglier label) rather than vanishing.
# Baselines are labelled by REFERENCE NUMBER, not author name, so a figure and
# section V's prose name the same thing without the reader translating between
# them. Numbers are the bibitem keys in Overleaf/MA-LB-PQ-VDSE.tex.
#
# yue_ge was labelled "Ge et al. [55]" and that was WRONG. ref55 is Cao et al.,
# "Enabling Puncturable Encrypted Search Over Lattice" (IEEE TMC 2026) -- a
# different paper. Ge et al. is ref30, cited 17 times in the manuscript against
# ref55's 2. Every figure has been pointing readers at the wrong citation.
#: The scheme whose folders `proposed_prefix`/`proposed_variants` redirect.
PROPOSED_SCHEME = "ma_lb_pq_vdse"

SCHEME_LABELS: Dict[str, str] = {
    "ma_lb_pq_vdse": "Proposed",
    "yue_ge": "Scheme [30]",
    "guo_vdsse": "Scheme [35]",
    "thingom_pq_abse": "Scheme [41]",
    "perera_lv_pqabse": "Scheme [54]",
}

# Marker AND linestyle both vary, so the figures survive grayscale.
# The proposed scheme is pinned to index 0 so it is visually consistent across
# all eight figures rather than shifting when a baseline is absent.
STYLE_ORDER: Tuple[str, ...] = (
    "ma_lb_pq_vdse", "guo_vdsse", "thingom_pq_abse",
    "perera_lv_pqabse", "yue_ge",
)
MARKERS = ("o", "s", "^", "D", "v", "P", "X")
LINESTYLES = ("-", "--", "-.", ":", (0, (3, 1, 1, 1)), (0, (5, 2)), (0, (1, 1)))
COLORS = ("#0072B2", "#D55E00", "#009E73", "#CC79A7", "#56B4E9", "#E69F00", "#000000")

# Legend/draw order only -- kept separate from STYLE_ORDER so reordering the
# legend can never reassign a scheme's marker/color/linestyle (that mapping is
# pinned by STYLE_ORDER's index and must stay fixed across every figure).
# Proposed first, then baselines by citation number: [30], [35], [41], [54].
LEGEND_ORDER: Tuple[str, ...] = (
    "ma_lb_pq_vdse",
    # Exp. 1's proposed curves are |P_U| arms rather than the scheme key, so
    # without these four they all sorted to the fallback slot and trailed the
    # baselines. Listed ascending so the family reads in scope order.
    "$|P_U| = 1$", "$|P_U| = 2$", "$|P_U| = 4$", "$|P_U| = 8$",
    "yue_ge", "guo_vdsse", "thingom_pq_abse",
    "perera_lv_pqabse",
)


#: Exp. 7-8 are an ABLATION of one scheme, so their four series are variant
#: LABELS rather than scheme keys and would all miss STYLE_ORDER -- every curve
#: drawn in the same colour and marker. Pin each to its own slot, and give
#: `aass` slot 0, the one the proposed scheme holds on the other six figures, so
#: the proposed line is the same blue circle everywhere.
ABLATION_STYLE_SLOT: Dict[str, int] = {
    "AASS (proposed)": 0,
    "Round robin": 1,
    "Least loaded": 2,
    "No load balancing": 3,
    # Exp. 6 uses its own vocabulary (EXP6_VARIANTS) -- none of these matched
    # the slots above, so all three fell through to the same fallback index
    # and drew identically (same color/marker). "DIAS (proposed)" gets slot 0,
    # the same blue circle the proposed scheme holds everywhere else.
    "DIAS (proposed)": 0,
    "Incremental-All": 1,
    "Full-State Synchronization": 2,
    # PSA Exp. 1's arms are |P_U| values, so they are ORDERED and the styles
    # should read that way: slot 0 (the proposed scheme's blue circle) is
    # |P_U| = 1, the baseline scope, and the rest step up from there. Without
    # these four entries all four curves drew in one colour and the figure
    # could not be read at all in grayscale, which SystemConfiguration.md requires.
    # "Proposed" is the sole Exp. 1 arm drawn since 2026-09-13; slot 0 is the
    # proposed scheme's blue circle, the same style it carries in every other
    # figure. The three |P_U| labels stay for the commented-out arms.
    "Proposed": 0,
    "$|P_U| = 1$": 0,
    "$|P_U| = 2$": 1,
    "$|P_U| = 4$": 2,
    "$|P_U| = 8$": 3,
}


#: Exp. 1's proposed curves are ONE scheme at four authorization scopes, not
#: four schemes. Given four different colours they read as eight unrelated
#: curves against four baselines; sharing the proposed colour and varying only
#: marker and linestyle makes them read as a family, which is what they are.
PROPOSED_FAMILY: Tuple[str, ...] = (
    "$|P_U| = 1$", "$|P_U| = 2$", "$|P_U| = 4$", "$|P_U| = 8$",
)


def style_for(scheme: str) -> Dict[str, object]:
    if scheme in ABLATION_STYLE_SLOT:
        idx = ABLATION_STYLE_SLOT[scheme]
    else:
        idx = STYLE_ORDER.index(scheme) if scheme in STYLE_ORDER else len(STYLE_ORDER)
    color = COLORS[idx % len(COLORS)]
    if scheme in PROPOSED_FAMILY:
        # Marker and linestyle still step with the slot, so the family stays
        # separable in grayscale.
        color = COLORS[0]
    return {
        "marker": MARKERS[idx % len(MARKERS)],
        "linestyle": LINESTYLES[idx % len(LINESTYLES)],
        "color": color,
    }


# ---------------------------------------------------------------------------
# Reading results
# ---------------------------------------------------------------------------
@dataclass
class Series:
    scheme: str
    x: List[float] = field(default_factory=list)
    y: List[float] = field(default_factory=list)
    yerr: List[float] = field(default_factory=list)
    #: metric index (1-based) -> (values, ci95s), for multi-panel figures.
    extra: Dict[int, Tuple[List[float], List[float]]] = field(default_factory=dict)
    n_runs: List[int] = field(default_factory=list)
    #: Per point, the results.csv `measurement_type` column when present:
    #: "measured" or "projected". Scheme 41's experiment_2_projected() writes it,
    #: is the ONLY reliable signal that a point was computed -- it writes
    #: n_runs=1, not 0, so the n_runs==0 rule below never fired for it and
    #: extrapolated points were drawn solid, indistinguishable from measured
    #: ones, while §VI's caption claimed they were hollow.
    measurement: List[str] = field(default_factory=list)
    reportable: Optional[bool] = None
    #: run_meta.json's `secondary_metrics`: the names behind `secondary_1_mean`,
    #: `secondary_2_mean`, ... in file order. Empty for a run written before the
    #: field existed, in which case a panel cannot be verified and says so.
    secondary_metrics: List[str] = field(default_factory=list)
    #: run_meta.json's `config_hashes["global.yaml"]`. global.yaml fixes
    #: `keywords_per_query`, `repetitions`, `warmup_runs` and the sweep values
    #: -- the parameters that make two schemes on one axis comparable, and its
    #: own header says changing it "invalidates every existing results.csv".
    #: Each run records its hash faithfully; nothing checked they AGREE across
    #: the schemes drawn in one figure, so the drift was invisible at plot time.
    #: fig:exp2's six runs turned out to carry four different values.
    config_hash: Optional[str] = None
    #: run_meta.json's `corpus_type`. A figure whose series were measured on
    #: DIFFERENT corpora is not one comparison: fig:exp1 drew four proposed
    #: curves from `psa_in_process` fixtures against four baseline curves from
    #: the frozen `synthea` corpus, on one axis, while SVI's own setup paragraph
    #: says the fixture measurements are "not directly comparable with the
    #: cross-scheme results".
    corpus_type: Optional[str] = None
    #: run_meta.json's `construction` — `option_d` (T = H(w)) or `psa`
    #: (T = H(w || PID || PV || Dom)). The two time DIFFERENT functions at the
    #: same experiment number, so mixing them in one figure publishes two
    #: schemes as one.
    construction: Optional[str] = None
    #: Secondaries keyed by their COLUMN NAME, alongside the positional `extra`.
    #: A panel that declares `metric_name` is resolved through this, so the
    #: number it draws is the metric the label names rather than whatever sits
    #: at that index. Empty only for a legacy file whose columns are numbered.
    extra_by_name: Dict[str, Tuple[List[float], List[float]]] = field(
        default_factory=dict
    )
    #: run_meta.json's `measurement_fingerprint` — a digest of the construction,
    #: metric names, sweep values and the SOURCE of `prepare`/`measure`. Two
    #: series with different fingerprints came from different measurements, even
    #: when their columns agree. Empty for a run written before the field.
    fingerprint: Optional[str] = None
    problems: List[str] = field(default_factory=list)


def _to_float(value: str) -> Optional[float]:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _secondary_columns(row: Dict[str, str]) -> List[Tuple[str, str]]:
    """The (`*_mean`, `*_ci95`) column pairs after primary, in file order."""
    pairs: List[Tuple[str, str]] = []
    for name in row:
        if not name.endswith("_mean") or name == "primary_mean":
            continue
        stem = name[: -len("_mean")]
        ci = f"{stem}_ci95"
        pairs.append((name, ci if ci in row else ""))
    return pairs


def read_results(path: Path, scheme: str) -> Optional[Series]:
    """Parse one ``results.csv``. Returns None if it has no usable rows.

    Rows with a missing/unparseable variable_value or primary_mean are skipped
    and recorded in ``problems`` — never silently dropped, and never guessed at.
    """
    series = Series(scheme=scheme)
    try:
        with path.open(newline="", encoding="utf-8") as handle:
            for lineno, row in enumerate(csv.DictReader(handle), start=2):
                x = _to_float(row.get("variable_value", ""))
                y = _to_float(row.get("primary_mean", ""))
                if x is None or y is None:
                    series.problems.append(
                        f"{path}:{lineno}: unusable variable_value/primary_mean, skipped"
                    )
                    continue
                ci = _to_float(row.get("primary_ci95", ""))
                n = _to_float(row.get("n_runs", "")) or 0
                # A single-run point has NO confidence interval -- there is no
                # variance to compute one from. Drawing ci=0 would put a
                # zero-length bar with caps on the point, which reads as "we
                # measured this very precisely": the exact opposite of the
                # truth. NaN makes matplotlib omit the bar entirely, so an
                # n=1 point is visibly bare next to the n=10 points beside it.
                # Points measured once are legitimate for a baseline whose
                # 10-run cost is prohibitive (thingom_pq_abse's Exp. 2 is
                # ~10.9 h for ONE run at N=10^6); claiming a CI for them is
                # not. See AGENT_RULES.md "Statistical Integrity".
                if n < 2 or ci is None:
                    ci = float("nan")
                series.x.append(x)
                series.y.append(y)
                series.yerr.append(ci)
                series.n_runs.append(int(n))
                series.measurement.append(row.get("measurement_type", "") or "")
                # Secondaries, positionally. The NAMES differ per scheme
                # (ma_lb writes `secondary_1_mean`, the baselines write the
                # metric's real name), so the i-th `*_mean` after primary is
                # the i-th secondary. Only multi-panel figures read these, and
                # those are single-scheme ablations, so the positional read
                # cannot cross schemes that disagree on ordering.
                for i, (mcol, ccol) in enumerate(_secondary_columns(row), start=1):
                    sy = _to_float(row.get(mcol, ""))
                    if sy is None:
                        continue
                    sci = _to_float(row.get(ccol, "")) if ccol else None
                    if n < 2 or sci is None:
                        sci = float("nan")
                    vals, errs = series.extra.setdefault(i, ([], []))
                    vals.append(sy)
                    errs.append(sci)
                    # Also by NAME. `secondary_1_mean` yields the stem
                    # "secondary_1", which matches no panel's `metric_name`, so
                    # a legacy numbered file simply contributes nothing here and
                    # the positional path still serves it.
                    stem = mcol[: -len("_mean")]
                    nvals, nerrs = series.extra_by_name.setdefault(
                        stem, ([], [])
                    )
                    nvals.append(sy)
                    nerrs.append(sci)
    except FileNotFoundError:
        return None
    except OSError as exc:
        print(f"  WARNING cannot read {path}: {exc}", file=sys.stderr)
        return None

    if not series.x:
        return None

    # Sort by x so a results.csv written out of order still plots as a curve
    # rather than a zigzag.
    order = sorted(range(len(series.x)), key=lambda i: series.x[i])
    series.x = [series.x[i] for i in order]
    series.y = [series.y[i] for i in order]
    series.yerr = [series.yerr[i] for i in order]
    series.n_runs = [series.n_runs[i] for i in order]

    meta = path.with_name("run_meta.json")
    if meta.is_file():
        try:
            parsed = json.loads(meta.read_text(encoding="utf-8"))
            series.reportable = bool(parsed.get("reportable", False))
            # Top level for most schemes; nested under `dataset` for the
            # schema thingom_pq_abse writes. Both are in use, exactly as they
            # are for `config_hashes` below.
            def _nested(key: str) -> Optional[str]:
                # isinstance rather than `or {}`: a malformed run_meta whose
                # `dataset` is a string would raise AttributeError, and that
                # exception is caught by the SHARED handler below — silently
                # costing this series its `secondary_metrics` and
                # `config_hash` too. A type check keeps one bad field from
                # taking the others with it.
                block = parsed.get(key)
                if isinstance(block, dict):
                    value = block.get("corpus_type")
                    if value:
                        return str(value)
                return None

            corpus = (
                parsed.get("corpus_type")
                or _nested("dataset")
                or _nested("environment")
            )
            if corpus:
                series.corpus_type = str(corpus)
            construction = parsed.get("construction")
            if construction:
                series.construction = str(construction)
            fingerprint = parsed.get("measurement_fingerprint")
            if fingerprint and fingerprint != "unavailable":
                series.fingerprint = str(fingerprint)
            # The secondary column ORDER the run actually wrote. This scheme's
            # results.csv columns are positional (`secondary_N_mean`), so a
            # panel's `metric` index is a claim about which metric sits there
            # and nothing used to check it. Fig. 8(c) was labelled "Cross-node
            # forwards" while that column held peak queue depth.
            names = parsed.get("secondary_metrics")
            if isinstance(names, list):
                series.secondary_metrics = [str(n) for n in names]
            # Top level for this scheme's harness, nested under `environment`
            # for the baselines that write the other schema. Both are in use.
            hashes = parsed.get("config_hashes") or {}
            if not hashes:
                hashes = (parsed.get("environment") or {}).get(
                    "config_hashes"
                ) or {}
            digest = hashes.get("global.yaml")
            if digest:
                series.config_hash = str(digest)
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            series.problems.append(f"{meta}: unreadable ({type(exc).__name__})")
    else:
        series.problems.append(f"{meta}: missing (global.yaml requires it)")
    return series


#: Order is fixed so the legend reads weakest-to-proposed on every regeneration.
ABLATION_VARIANTS: Tuple[Tuple[str, str], ...] = (
    ("no_lb", "No load balancing"),
    ("round_robin", "Round robin"),
    ("least_loaded", "Least loaded"),
    ("aass", "AASS (proposed)"),
)

#: Exp. 6 ablates DIAS PROPAGATION, not the scheduler, so it has its own
#: vocabulary. Added 2026-09-03 -- before that Exp. 6 plotted one series with no
#: comparison, so global.yaml's "selective propagation is the claim" had nothing to
#: read it against.
#:
#: LEFT is the on-disk slug, RIGHT is the legend text. They differ on purpose:
#: the manuscript's Exp. 6 names the arms DIAS / Incremental-All / Full-State
#: Synchronization, while the slug is frozen by the directory every banked run
#: was written into (`exp6_authorization_sync__<slug>/`). This tuple is the one
#: place the two vocabularies meet, so the figure can carry the paper's names
#: without any measured data being moved or relabelled.
#:
#:   `incremental_all` -> Incremental-All: updates only affected state, but
#:                        delivers the delta to every FSN. Ablates SELECTIVE.
#:   `full_state`      -> Full-State: every authority recomputes its commitment
#:                        and the AIM republishes it. Ablates INCREMENTAL.
#:   `dias`            -> DIAS: the published rule, both halves together.
#:
#: RESLUGGED 2026-09-12. These were `broadcast`/`full_rebuild`/`ias`, frozen by
#: the directories the pre-revision runs were written into. But `--construction`
#: accepts only `psa`, so the arm names that reach disk are
#: `psa_experiments.PSA_EXP6_VARIANTS` -- `dias`/`incremental_all`/`full_state`
#: -- and the old slugs matched no directory. Fig. 6 would have drawn three
#: "missing variant" notes and no data. The pre-revision Exp. 6 dirs are
#: superseded (they carry the stipulated policy topology), so nothing measured
#: is stranded by the rename. Pinned by
#: test_exp6_figure_uses_the_runners_variant_slugs.
EXP6_VARIANTS: Tuple[Tuple[str, str], ...] = (
    ("incremental_all", "Incremental-All"),
    ("full_state", "Full-State Synchronization"),
    ("dias", "DIAS (proposed)"),
)


def variants_for(spec: "ExperimentSpec") -> Tuple[Tuple[str, str], ...]:
    """Which variant vocabulary an experiment's ablation figure uses."""
    if spec.variants:
        return spec.variants
    return EXP6_VARIANTS if spec.number == 6 else ABLATION_VARIANTS


def _variant_of(exp_dir: Path) -> Optional[str]:
    """Which scheduler produced this directory.

    ``run_meta.json``'s ``scheduler_variant`` note is authoritative; the
    directory suffix is only the fallback. A result identified solely by its
    folder name loses its identity the moment anything is renamed or merged,
    which is why the note is written in the first place.
    """
    meta = exp_dir / "run_meta.json"
    try:
        notes = json.loads(meta.read_text(encoding="utf-8")).get("notes") or []
        for note in notes:
            if str(note).startswith("scheduler_variant="):
                return str(note).split("=", 1)[1].strip()
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return exp_dir.name.rsplit("__", 1)[-1] if "__" in exp_dir.name else None


def _has_variant_dirs(input_root: Path, spec: ExperimentSpec) -> bool:
    """Whether any directory names a variant this experiment's ablation knows."""
    if not input_root.is_dir():
        return False
    known = dict(variants_for(spec))
    for scheme_dir in sorted(p for p in input_root.iterdir() if p.is_dir()):
        for exp_dir in scheme_dir.glob(f"{spec.prefix}exp{spec.number}_*__*"):
            if exp_dir.is_dir() and _variant_of(exp_dir) in known:
                return True
    return False


def collect_ablation(input_root: Path, spec: ExperimentSpec) -> List[Series]:
    """One series per scheduler variant, for Exp. 7-8.

    ``collect`` takes the FIRST matching ``exp<N>_*`` directory per scheme and
    stops, which is right for a cross-scheme figure and wrong here: it would
    draw a single curve labelled with the scheme name where section V claims a
    four-way comparison, silently choosing whichever variant sorted first.
    """
    found: List[Series] = []
    if not input_root.is_dir():
        return found
    by_variant: Dict[str, Path] = {}
    for scheme_dir in sorted(p for p in input_root.iterdir() if p.is_dir()):
        for exp_dir in sorted(scheme_dir.glob(f"{spec.prefix}exp{spec.number}_*__*")):
            if not exp_dir.is_dir():
                continue
            variant = _variant_of(exp_dir)
            # Skips the `__points-<N>` sweep shards, which are not variants.
            if variant in dict(variants_for(spec)):
                by_variant.setdefault(variant, exp_dir)
    for variant, label in variants_for(spec):
        exp_dir = by_variant.get(variant)
        if exp_dir is None:
            print(f"  NOTE exp{spec.number}: no directory for variant "
                  f"{variant!r}; the ablation figure will be incomplete")
            continue
        series = read_results(exp_dir / "results.csv", label)
        if series is None:
            print(f"  NOTE exp{spec.number}: {exp_dir.name} has no usable "
                  f"results.csv; {variant!r} omitted")
            continue
        found.append(series)
    return found


def collect_scope_sweep(
    input_root: Path, base_folder: str, panel: "PanelSpec"
) -> List[Series]:
    """One series: the metric at a fixed x, across several arm folders.

    Reads `<scheme>/<base_folder>__<suffix>/results.csv` for every
    `(suffix, parameter)` in `panel.scope_arms`, takes the row whose
    `variable_value` equals `panel.scope_at_x`, and plots it against
    `parameter`. That turns a within-scheme knob into its own axis instead of
    N extra curves on a between-scheme figure.

    A missing arm or a missing x is dropped rather than raised on: the panel
    then draws the arms that exist, and the caller warns. Silently plotting a
    short curve as if it were complete is the failure this avoids.
    """
    out = Series(scheme=panel.scope_scheme or "Proposed")
    scheme_dir = input_root / PROPOSED_SCHEME
    for suffix, parameter in panel.scope_arms:
        results = scheme_dir / f"{base_folder}__{suffix}" / "results.csv"
        if not results.is_file():
            continue
        try:
            rows = list(csv.DictReader(results.open(newline="", encoding="utf-8")))
        except OSError:
            continue
        for row in rows:
            try:
                if float(row["variable_value"]) != float(panel.scope_at_x):
                    continue
                out.x.append(float(parameter))
                out.y.append(float(row["primary_mean"]))
                out.yerr.append(float(row.get("primary_ci95") or 0.0))
                out.n_runs.append(int(float(row.get("n_runs") or 0)))
            except (KeyError, TypeError, ValueError):
                continue
            break
    meta = scheme_dir / f"{base_folder}__{panel.scope_arms[0][0]}" / "run_meta.json"
    if meta.is_file():
        try:
            out.reportable = json.loads(meta.read_text()).get("reportable")
        except (OSError, json.JSONDecodeError):
            pass
    return [out] if out.x else []


def collect_folder(input_root: Path, folder: str) -> List[Series]:
    """Every scheme's results for one experiment FOLDER, by name.

    ``collect`` dispatches on the experiment number (ablations, variant probes);
    this is the plain read a cross-folder panel needs. Kept separate so adding a
    panel from another experiment cannot accidentally re-route Exp. 6/7/8's
    ablation handling.
    """
    found: List[Series] = []
    if not input_root.is_dir():
        return found
    number = folder.split("_", 1)[0].replace("exp", "")
    for scheme_dir in sorted(p for p in input_root.iterdir() if p.is_dir()):
        for exp_dir in sorted(scheme_dir.glob(f"exp{number}_*")):
            if not exp_dir.is_dir() or "__" in exp_dir.name:
                continue
            series = read_results(exp_dir / "results.csv", scheme_dir.name)
            if series is not None:
                found.append(series)
                break
    return found


def _restrict_to(series: Series, allowed: Sequence[float]) -> Series:
    """Keep only the sweep points a figure is meant to show.

    Filters the parallel arrays TOGETHER -- x, y, yerr, n_runs, measurement and
    every secondary in `extra` -- because they are indexed positionally and
    dropping a point from one alone would silently shift a curve against its own
    error bars.
    """
    if not allowed:
        return series
    keep = [i for i, x in enumerate(series.x) if x in set(allowed)]
    if len(keep) == len(series.x):
        return series
    series.x = [series.x[i] for i in keep]
    series.y = [series.y[i] for i in keep]
    series.yerr = [series.yerr[i] for i in keep]
    series.n_runs = [series.n_runs[i] for i in keep]
    if series.measurement:
        series.measurement = [series.measurement[i] for i in keep]
    for metric, (vals, errs) in list(series.extra.items()):
        series.extra[metric] = (
            [vals[i] for i in keep if i < len(vals)],
            [errs[i] for i in keep if i < len(errs)],
        )
    # The name-keyed copy is the SAME data under a different key, so it must be
    # filtered with the same `keep`. Leaving it unfiltered would misalign a
    # named series against a restricted x -- the exact class of silent shift
    # this function's docstring exists to prevent.
    for name, (vals, errs) in list(series.extra_by_name.items()):
        series.extra_by_name[name] = (
            [vals[i] for i in keep if i < len(vals)],
            [errs[i] for i in keep if i < len(errs)],
        )
    return series


def collect_mixed(input_root: Path, spec: ExperimentSpec) -> List[Series]:
    """Baselines from `exp<N>_*`, the proposed scheme from its own arm folders.

    §VI Exp. 1 makes one figure do two jobs: compare the proposed construction
    against four baselines over `q`, AND show how the curve moves with
    `|P_U|`. The proposed arms live in `exp1_trapdoor_generation__pu<N>/`
    while every baseline has a single `exp1_trapdoor_generation/`, so neither
    `collect` (one directory per scheme) nor `collect_ablation` (arms only, no
    baselines) can assemble it -- `collect_ablation` in particular would have
    silently produced a four-curve figure with no baseline on it at all.

    Baseline series keep the scheme KEY as their label so `style_for` and
    `SCHEME_LABELS` resolve as they do on every other figure; the proposed arms
    carry their `|P_U|` label, which `ABLATION_STYLE_SLOT` already styles.
    """
    found: List[Series] = []
    if not input_root.is_dir():
        return found

    for scheme_dir in sorted(p for p in input_root.iterdir() if p.is_dir()):
        if scheme_dir.name == PROPOSED_SCHEME:
            continue
        for exp_dir in sorted(scheme_dir.glob(f"{spec.prefix}exp{spec.number}_*")):
            if not exp_dir.is_dir() or "__" in exp_dir.name:
                continue          # skip point shards and variant folders
            series = read_results(exp_dir / "results.csv", scheme_dir.name)
            if series is not None:
                found.append(_restrict_to(series, spec.restrict_x))
                break

    proposed_dir = input_root / PROPOSED_SCHEME
    if proposed_dir.is_dir():
        by_variant: Dict[str, Path] = {}
        pattern = f"{spec.proposed_prefix}exp{spec.number}_*__*"
        for exp_dir in sorted(proposed_dir.glob(pattern)):
            if not exp_dir.is_dir():
                continue
            variant = _variant_of(exp_dir)
            if variant in dict(spec.proposed_variants):
                by_variant.setdefault(variant, exp_dir)
        for variant, label in spec.proposed_variants:
            exp_dir = by_variant.get(variant)
            if exp_dir is None:
                print(f"  NOTE exp{spec.number}: no directory for proposed arm "
                      f"{variant!r}; the figure will be missing a curve")
                continue
            series = read_results(exp_dir / "results.csv", label)
            if series is None:
                print(f"  NOTE exp{spec.number}: {exp_dir.name} has no usable "
                      f"results.csv; arm {variant!r} omitted")
                continue
            found.append(_restrict_to(series, spec.restrict_x))
    return found


def _folders_claimed_by_other_specs(spec: "ExperimentSpec") -> set:
    """Folder names that belong to a DIFFERENT experiment spec.

    `collect`'s glob keys on the experiment NUMBER so a scheme whose directory
    is named slightly differently still contributes. Two specs can share a
    number across constructions, though — `exp3_crossdomain_scalability` (§VI's
    Fig. 3) once had a token-count companion that also matched `exp3_*`
    — and then the fallback silently drew the wrong experiment
    under the right axis label. Excluding folders another spec has claimed keeps
    the fallback for its purpose (a naming variant) and out of the one case it
    gets wrong (a different measurement).
    """
    claimed = set()
    # Was `tuple(EXPERIMENTS) + tuple(PSA_EXPERIMENTS)`. PSA_EXPERIMENTS went
    # with the second figure family on 2026-09-12, so this raised NameError on
    # every run -- the plotter has not executed since. One family now.
    for other in tuple(EXPERIMENTS):
        if other.folder and other.folder != spec.folder:
            claimed.add(other.folder)
        for panel in other.panels:
            if panel.folder and panel.folder != spec.folder:
                claimed.add(panel.folder)
    return claimed


def collect(input_root: Path, spec: ExperimentSpec) -> List[Series]:
    """Find every scheme's results for one experiment.

    Matches ``exp<N>_*`` rather than the exact folder name so a scheme that
    names its directory slightly differently is still picked up instead of
    silently contributing nothing.
    """
    found: List[Series] = []
    # Checked FIRST: a mixed spec also sets `variants` in some cases, and the
    # ablation branch below would then drop every baseline.
    if spec.proposed_variants:
        return collect_mixed(input_root, spec)
    if spec.number in (7, 8):
        return collect_ablation(input_root, spec)
    if spec.variants:
        # A spec that names its own arms IS an ablation, whatever its number.
        # PSA Exp. 1 is one: |P_U| is the arm, so the four `__pu<N>` directories
        # are four curves. Without this it fell through to the cross-scheme
        # branch, which takes the FIRST matching directory per scheme and stops
        # -- one arm drawn, three silently dropped, labelled with the scheme.
        return collect_ablation(input_root, spec)
    if spec.number == 6 and _has_variant_dirs(input_root, spec):
        # Exp. 6 gained an ablation on 2026-09-03 (ias / broadcast /
        # full_rebuild). Probed rather than assumed so results predating it still
        # plot as a single series instead of emitting three "missing variant"
        # notes for directories that were never supposed to exist.
        return collect_ablation(input_root, spec)
    if not input_root.is_dir():
        return found
    for scheme_dir in sorted(p for p in input_root.iterdir() if p.is_dir()):
        # THE SPEC'S OWN FOLDER FIRST, then the number glob.
        #
        # The glob exists so a scheme that names its directory slightly
        # differently still contributes instead of silently plotting nothing.
        # But it matches on the NUMBER, and two different experiments can share
        # one number across a construction: `exp3_crossdomain_scalability`
        # (SVI's Fig. 3) once shared a prefix with a token-count companion,
        # so a glob could match both. The latency spec therefore drew the token
        # experiment's data under a "Cross-domain search latency (ms)" axis --
        # the same defect as Fig. 8(c), reached by a different route.
        #
        # Preferring the declared folder makes the spec's own `folder` field
        # load-bearing rather than decorative; the glob stays as the fallback
        # it was written to be.
        # THE PROPOSED SCHEME FOLLOWS `proposed_prefix`; the baselines do not.
        #
        # `proposed_prefix` only took effect through `collect_mixed`, which runs
        # only when `proposed_variants` is also set. So a spec that named a
        # `psa_` folder and set `proposed_prefix` — every manuscript spec after
        # the 2026-09-10 repoint — still globbed `exp<N>_*` for the proposed
        # scheme and silently drew OPTION D. Verified: Fig. 3's proposed series
        # came back as 0.076/0.106/0.123 ms, the pre-fix Option D numbers, under
        # a spec whose folder said `exp3_crossdomain_scalability`.
        #
        # The baselines have no `psa_` directories, so the prefix must apply to
        # the proposed scheme alone.
        prefix = spec.prefix
        if spec.proposed_prefix and scheme_dir.name == PROPOSED_SCHEME:
            prefix = spec.proposed_prefix
        declared = scheme_dir / spec.folder
        matches = [declared] if declared.is_dir() else []
        # The fallback must never reach ANOTHER spec's declared folder. Merely
        # preferring `declared` was not enough: when it is absent the glob still
        # matched the sibling and drew it, so an experiment with no data yet
        # borrowed a different experiment's numbers instead of reporting that it
        # had none.
        claimed = _folders_claimed_by_other_specs(spec)
        matches += [
            m for m in sorted(scheme_dir.glob(f"{prefix}exp{spec.number}_*"))
            if m != declared and m.name not in claimed
        ]
        used: Optional[Path] = None
        for exp_dir in matches:
            if not exp_dir.is_dir():
                continue
            series = read_results(exp_dir / "results.csv", scheme_dir.name)
            if series is not None:
                found.append(series)
                used = exp_dir  # the one that actually contributed data
                break
        if len(matches) > 1:
            # Name the directory that supplied the data, not matches[0]: the
            # loop skips directories whose results.csv is missing or unusable,
            # so the two can differ and reporting the wrong one misleads.
            print(f"  NOTE {scheme_dir.name}: multiple exp{spec.number}_* dirs "
                  f"{[m.name for m in matches]}; used "
                  f"{used.name if used else 'none (no usable results.csv)'}")
    return found


# ---------------------------------------------------------------------------
# Plotting
# ---------------------------------------------------------------------------
# IEEE single-column is 3.5 in. 8 pt is SystemConfiguration.md's stated minimum, so every
# text element is set at or above it.
plt.rcParams.update({
    "font.size": 8,
    "axes.labelsize": 8,
    "axes.titlesize": 8,
    "xtick.labelsize": 8,
    "ytick.labelsize": 8,
    "legend.fontsize": 8,
    "figure.figsize": (3.5, 2.6),
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.02,
    "pdf.fonttype": 42,  # embed TrueType rather than Type 3: IEEE requires it
    "ps.fonttype": 42,
})


def _axis_number(v: float) -> str:
    """Compact scientific form for a legend note: 200000 -> 2x10^5."""
    if v <= 0:
        return str(v)
    exp = int(math.floor(math.log10(v)))
    mant = v / (10 ** exp)
    if exp < 3:
        return f"{v:g}"
    # mathtext, so the exponent renders as a superscript in both the PDF and
    # the PNG rather than as a literal "10^5".
    return (rf"$10^{{{exp}}}$" if abs(mant - 1) < 1e-9
            else rf"${mant:g}\times10^{{{exp}}}$")


def _check_log_y_criterion(spec: ExperimentSpec, series_list: Sequence[Series],
                           warnings: List[str]) -> None:
    """Warn when a linear-y figure has grown past the log threshold.

    Without this the criterion above decays into a comment: new data widens a
    range, the axis stays linear, and curves quietly flatten onto the x-axis.
    """
    if spec.log_y or spec.panels:
        return
    vals = [v for s in series_list for v in s.y if v > 0]
    if len(vals) < 2:
        return
    span = math.log10(max(vals) / min(vals))
    if span >= LOG_Y_DECADES:
        warnings.append(
            f"exp{spec.number}: y-values now span {span:.2f} decades "
            f"(>= {LOG_Y_DECADES}); LOG_Y_DECADES says this figure should set "
            f"log_y=True or curves will flatten onto the axis"
        )


def _panel_values(
    series: Series, metric: int, per_x: bool = False,
    metric_name: Optional[str] = None,
) -> Tuple[List[float], List[float]]:
    """(values, ci95s) for one metric, BY NAME where the file provides one.

    ``metric`` is a position and ``metric_name`` is what the panel claims sits
    there. When the file names its columns the name wins, so the number drawn is
    the metric the label names; the positional read remains for legacy files
    whose columns are ``secondary_1_mean``, ``secondary_2_mean``, ….

    That positional binding is what let Fig. 8(c) caption `max_queue_depth` as
    "Cross-node forwards": the metric list gained an entry, every later column
    shifted, and the panel kept its index.

    ``per_x`` divides both the value and its interval by the x value, turning a
    total into a per-unit rate. The interval scales with the value because it is
    a half-width in the same units, so T_avg's interval is the total's over r.
    """
    if metric == 0:
        values, errs = series.y, series.yerr
    elif metric_name and metric_name in series.extra_by_name:
        values, errs = series.extra_by_name[metric_name]
    else:
        values, errs = series.extra.get(metric, ([], []))
    if not per_x:
        return values, errs
    scaled_v, scaled_e = [], []
    for i, v in enumerate(values):
        x = series.x[i] if i < len(series.x) else 0
        if not x:
            # A zero x cannot yield a per-unit rate; drop the point rather than
            # divide by zero and plot an inf.
            continue
        scaled_v.append(v / x)
        scaled_e.append((errs[i] / x) if i < len(errs) else 0.0)
    return scaled_v, scaled_e


def _panel_metric_agrees(
    spec, panel, panel_series, warnings, strict: bool = False
) -> bool:
    """Refuse to draw a panel whose label does not name the column it reads.

    ``PanelSpec.metric`` is a POSITION in results.csv and ``ylabel`` is a claim
    about what sits there. Nothing connected the two until run_meta.json began
    recording ``secondary_metrics``, and the gap produced a real defect: Exp. 8's
    panel (c) was labelled "Cross-node forwards" after ``cross_node_forwards``
    had been dropped from the metric list, so the column at index 2 was peak
    queue depth and the figure said otherwise.

    Skipping the panel is the right failure. A figure with a missing panel is
    obviously incomplete; a figure with a mislabelled one is not.
    """
    if panel.metric == 0 or not panel.metric_name:
        return True                       # primary, or an unverified panel
    ok = True
    for series in panel_series:
        names = series.secondary_metrics
        if not names:
            # A run written before the field existed.
            #
            # WITHOUT `strict`: say so once, and draw. Refusing outright would
            # blank every panel of every banked result, and a figure nobody can
            # produce is not a safer figure.
            #
            # WITH `strict` (--require-reportable): SKIP. This is the submission
            # path, and an unverifiable label is exactly how Fig. 8(c) shipped
            # captioned "Cross-node forwards" over a column holding peak queue
            # depth. A missing panel is obviously incomplete; a mislabelled one
            # is not, so the strict path must not draw it.
            warnings.append(
                f"exp{spec.number}: panel ({panel.tag}) cannot be verified — "
                f"{series.scheme}'s run_meta.json records no "
                f"secondary_metrics; the label {panel.ylabel!r} is unchecked"
                + ("; panel skipped (--require-reportable)" if strict else "")
            )
            if strict:
                ok = False
            continue
        index = panel.metric - 1          # metric 1 is the first secondary
        actual = names[index] if 0 <= index < len(names) else None
        if actual != panel.metric_name:
            warnings.append(
                f"exp{spec.number}: panel ({panel.tag}) claims "
                f"{panel.metric_name!r} at secondary_{panel.metric} but "
                f"{series.scheme} recorded {actual!r} there; panel skipped "
                f"rather than mislabelled"
            )
            ok = False
    return ok


def _check_provenance_homogeneity(
    spec: "ExperimentSpec", series_list, warnings: List[str]
) -> None:
    """Refuse to present series measured on different corpora or constructions.

    A figure is a COMPARISON, and a comparison only means something if the
    things compared were measured against the same corpus by the same scheme.
    Neither was checked, and both had already gone wrong:

    * **Corpus.** fig:exp1's four proposed curves come from
      ``exp1_trapdoor_generation__pu<N>/``, whose ``corpus_type`` is
      ``psa_in_process`` and whose ``reportable`` is ``false`` ("no corpus
      SHA-256"), drawn on one axis against four baselines measured on the frozen
      ``synthea`` corpus. SVI's own setup paragraph says the fixture
      measurements are "not directly comparable with the cross-scheme results",
      and then the figure compares them.

    * **Construction.** ``option_d`` computes ``T = H(w)`` and ``psa`` computes
      ``T = H(w || PID || PV || Dom)``. They time DIFFERENT functions at the same
      experiment number, so a figure mixing them publishes two schemes as one.

    A warning rather than a hard skip, for the reason the label check gives:
    every banked run predates ``construction``, so refusing outright would blank
    every figure in the repo. Under ``--require-reportable`` the non-reportable
    series is dropped before this runs, which is the enforcing path.
    """
    for attribute, label, plural in (
        ("corpus_type", "corpus", "corpora"),
        ("construction", "construction", "constructions"),
    ):
        # A MISSING value is its own bucket, not a skip. Every one of the 114
        # banked runs predates `construction`, so skipping None would leave the
        # check blind during exactly the migration it exists to police: a figure
        # drawing one freshly re-run `psa` series against four banked series
        # that never recorded a construction is the most likely way the two get
        # mixed, and it would pass silently. Bucketing None keeps a
        # wholly-unrecorded figure quiet (one bucket) while flagging a partly
        # re-run one (two buckets), which is the case that matters.
        seen = {}
        for series in series_list:
            value = getattr(series, attribute, None)
            seen.setdefault(value or "<not recorded>", []).append(series.scheme)
        if len(seen) > 1:
            detail = "; ".join(
                f"{value}: {', '.join(sorted(schemes))}"
                for value, schemes in sorted(seen.items())
            )
            warnings.append(
                f"exp{spec.number}: this figure mixes more than one {label} "
                f"on one axis — {detail}. Series measured on different "
                f"{plural} are not one comparison."
            )


def render(spec: ExperimentSpec, series_list: Sequence[Series],
           out_path: Path, dpi: Optional[int] = None,
           input_root: Optional[Path] = None,
           strict: bool = False) -> Tuple[bool, List[str]]:
    """Draw one figure. Returns (written, warnings).

    ``input_root`` is needed only when a panel names its own ``folder``; without
    it such a panel is skipped with a warning rather than drawn empty.
    """
    warnings: List[str] = []
    if not series_list:
        return False, [f"exp{spec.number}: no results.csv found for any scheme"]
    _check_log_y_criterion(spec, series_list, warnings)
    _check_provenance_homogeneity(spec, series_list, warnings)

    if spec.panels:
        # A panel drawn from another experiment sweeps a DIFFERENT variable, so
        # the panels cannot share an x-axis: Exp. 4's (a) is `r` returned
        # ciphertexts and (b) is `t` tampered ones. Sharing would silently
        # relabel one of them.
        # A scope panel's x is |P_U|, not the figure's q, so it cannot
        # share an x-axis either.
        cross = any(panel.folder or panel.scope_arms for panel in spec.panels)
        # Stacked, not side by side: three panels across an IEEE single column
        # would be 1.16in each, too narrow for an axis label. Height is per
        # panel; width is whatever the column (and --scale) already set.
        w, h = plt.rcParams["figure.figsize"]
        fig, axes = plt.subplots(
            len(spec.panels), 1, sharex=not cross,
            figsize=(w, h * 0.78 * len(spec.panels)),
        )
        for i, (ax, panel) in enumerate(zip(axes, spec.panels)):
            panel_series = series_list
            if panel.scope_arms:
                if input_root is None:
                    warnings.append(
                        f"exp{spec.number}: panel ({panel.tag}) is a scope "
                        f"sweep but no input root was given; skipped"
                    )
                    continue
                panel_series = collect_scope_sweep(input_root, spec.folder, panel)
                if not panel_series:
                    warnings.append(
                        f"exp{spec.number}: panel ({panel.tag}) found none of "
                        f"the arms {[a for a, _ in panel.scope_arms]} under "
                        f"{spec.folder}__*; the figure is incomplete"
                    )
                    continue
                drawn = len(panel_series[0].x)
                if drawn != len(panel.scope_arms):
                    warnings.append(
                        f"exp{spec.number}: panel ({panel.tag}) drew {drawn} of "
                        f"{len(panel.scope_arms)} arms at x={panel.scope_at_x}; "
                        f"a short curve would read as a complete one"
                    )
            elif panel.folder:
                if input_root is None:
                    warnings.append(
                        f"exp{spec.number}: panel ({panel.tag}) reads "
                        f"{panel.folder!r} but no input root was given; skipped"
                    )
                    continue
                panel_series = collect_folder(input_root, panel.folder)
                if not panel_series:
                    warnings.append(
                        f"exp{spec.number}: panel ({panel.tag}) found no "
                        f"results under {panel.folder!r}; the figure is "
                        f"incomplete"
                    )
                    continue
            if not _panel_metric_agrees(
                spec, panel, panel_series, warnings, strict=strict
            ):
                continue
            _draw_panel(
                ax, spec, panel_series, warnings,
                metric=panel.metric, ylabel=panel.ylabel,
                # The panel's own claim about which metric it draws. When the
                # results.csv names its columns this selects by name, so the
                # label and the number cannot disagree.
                metric_name=panel.metric_name,
                # Legend once, on the top panel -- EXCEPT for a panel that
                # draws curves the top one does not. A cross-folder panel has
                # its own scheme set (Exp. 4 panel (b) omits Scheme [54], which
                # has no granularity arm, so borrowing panel (a)'s four-entry
                # legend would claim a curve that is not drawn), and a
                # companion panel adds a reference curve of its own -- Exp. 3
                # panel (b)'s flat line at 1 is Option D's trapdoor count, and
                # unlabelled it is just an unexplained rule across the figure.
                # A scope panel draws ONE series in the proposed
                # scheme's own style, already named in panel (a); a
                # one-entry legend would just repeat it.
                add_legend=(i == 0 or bool(panel.folder)
                            or panel.companion_metric is not None),
                # Warnings once, or each series would report itself per panel.
                collect_warnings=(i == 0),
                # Every panel labels its own x when they are different
                # variables; otherwise only the bottom one does.
                add_xlabel=(cross or i == len(spec.panels) - 1),
                # log_y is declared for the PRIMARY metric; a secondary is a
                # different quantity and may not be positive or wide-ranging, so
                # it opts in per panel.
                allow_log_y=(panel.metric == 0) or panel.log_y,
                per_x=panel.per_x,
                xlabel=panel.xlabel,
                force_log_x=panel.log_x,
                companion_metric=panel.companion_metric,
                companion_label=panel.companion_label,
            )
            ax.set_title(f"({panel.tag})", loc="left", fontsize=8, pad=2)
        fig.align_ylabels(axes)
        fig.tight_layout(pad=0.3, h_pad=0.6)
    else:
        fig, ax = plt.subplots()
        _draw_panel(ax, spec, series_list, warnings,
                    metric=0, ylabel=spec.ylabel, add_legend=True,
                    collect_warnings=True, add_xlabel=True, allow_log_y=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, **({'dpi': dpi} if dpi else {}))
    plt.close(fig)
    return True, warnings


def _draw_panel(ax, spec: ExperimentSpec, series_list: Sequence[Series],
                warnings: List[str], *, metric: int, ylabel: str,
                add_legend: bool, collect_warnings: bool,
                add_xlabel: bool, allow_log_y: bool,
                per_x: bool = False, xlabel: Optional[str] = None,
                force_log_x: bool = False,
                companion_metric: Optional[int] = None,
                companion_label: str = "",
                metric_name: Optional[str] = None) -> None:
    """Draw every series' `metric` onto one axes, resolved BY NAME where given."""
    # The furthest point any scheme reached, so a shorter series can be marked.
    _all_x = [v for s in series_list for v in s.x]
    max_x = max(_all_x) if _all_x else None
    for series in sorted(series_list,
                         key=lambda s: LEGEND_ORDER.index(s.scheme)
                         if s.scheme in LEGEND_ORDER else 99):
        label = SCHEME_LABELS.get(series.scheme, series.scheme)
        # A series that stops short of the sweep is a DISCLOSED CAP, not missing
        # data -- guo_vdsse's Exp. 2 ends at N=2e5 because its forward index is
        # 51.7 GB at 10^6 on a 16 GiB host, and thingom_pq_abse ends at 10^4 for
        # the same class of reason. Unlabelled, a line that simply stops reads
        # as a failed run; the first question anyone asks of the figure is why
        # it vanishes. Say so on the curve itself.
        if series.x and max_x is not None and max(series.x) < max_x:
            label += f" (to {_axis_number(max(series.x))})"
        # n_runs=0 marks a point COMPUTED from a measured anchor rather than
        # run. Those markers are drawn HOLLOW so
        # the figure still separates measured from computed at a glance.
        #
        # A legend suffix saying so was removed on request 2026-08-31. The
        # marker is now the ONLY in-figure signal, which means the FIGURE
        # CAPTION in section V has to state which points are extrapolated --
        # a hollow marker shows a reader that something differs, not what.
        computed = [
            i for i, n in enumerate(series.n_runs)
            if n == 0
            or (i < len(series.measurement)
                and series.measurement[i].strip().lower() == "projected")
        ]
        yvals, yerrs = _panel_values(series, metric, per_x, metric_name)
        if not yvals:
            # A scheme that records no such secondary simply has no curve on
            # this panel; the others still draw.
            continue
        ax.errorbar(
            series.x, yvals, yerr=yerrs,
            label=label, capsize=2, markersize=3.5, linewidth=1.1,
            elinewidth=0.8, **style_for(series.scheme),
        )
        if computed:
            st = style_for(series.scheme)
            ax.plot([series.x[i] for i in computed],
                    [yvals[i] for i in computed],
                    linestyle="none", marker=st["marker"], markersize=3.5,
                    markerfacecolor="white", markeredgecolor=st["color"],
                    markeredgewidth=0.9, zorder=3)
        if not collect_warnings:
            continue
        if series.reportable is False:
            warnings.append(
                f"exp{spec.number}: {series.scheme} is NOT reportable "
                f"(run_meta.json) — development data, not for submission"
            )
        elif series.reportable is None:
            warnings.append(
                f"exp{spec.number}: {series.scheme} has no readable run_meta.json"
            )
        # Read from the config, not a literal. This said `< 30` and cited
        # "global.yaml requires 30" until 2026-09-04 -- eight months after the
        # campaign moved to 10 -- so it fired on EVERY series of EVERY figure
        # at the correct count. A warning that is always wrong is worse than
        # none: it trains the reader to scroll past the ones that are right.
        required = _required_repetitions()
        short = [n for n in series.n_runs if n < required]
        if short:
            warnings.append(
                f"exp{spec.number}: {series.scheme} has points with "
                f"n_runs<{required} (min {min(short)}) — global.yaml requires "
                f"{required} for reportable data"
            )
        warnings.extend(series.problems)

    # ONE FIGURE, ONE `global.yaml`. Every series above is drawn on a shared
    # axis, and that only means anything if the schemes were run under the same
    # `keywords_per_query`, `repetitions`, `warmup_runs` and sweep values --
    # all of which live in global.yaml. Its header calls any change to it
    # results-affecting; each run records the hash it used, but until this
    # check nothing compared them, and fig:exp2's six runs carried FOUR
    # different values (2026-09-07). Named here rather than silently drawn.
    if collect_warnings:
        seen: Dict[str, List[str]] = {}
        for series in series_list:
            if series.config_hash:
                seen.setdefault(series.config_hash, []).append(series.scheme)
        if len(seen) > 1:
            detail = "; ".join(
                f"{digest[:12]}...: {', '.join(sorted(schemes))}"
                for digest, schemes in sorted(seen.items())
            )
            warnings.append(
                f"exp{spec.number}: schemes in one figure were run under "
                f"{len(seen)} DIFFERENT global.yaml revisions — {detail}. "
                f"global.yaml fixes q, repetitions and the sweep, so the "
                f"curves may not be comparable"
            )
        missing = sorted(
            s.scheme for s in series_list if not s.config_hash
        )
        if missing and seen:
            warnings.append(
                f"exp{spec.number}: no global.yaml hash in run_meta.json for "
                f"{', '.join(missing)} — cannot confirm they match the rest"
            )

        # SAME CHECK, ONE LEVEL DEEPER. global.yaml pins the parameters; the
        # fingerprint pins the MEASUREMENT — construction, metric names, sweep
        # values and the source of prepare/measure. Two series can share a
        # config revision and still have been produced by different code, which
        # is exactly the `exp5_keyword_update` case: `entries_rewritten` reads
        # 0.0 in banked data and 6-per-record today, with identical column names
        # either side. Named columns cannot see that; this can.
        prints: Dict[str, List[str]] = {}
        for series in series_list:
            if series.fingerprint:
                prints.setdefault(series.fingerprint, []).append(series.scheme)
        if len(prints) > 1:
            detail = "; ".join(
                f"{digest[:12]}...: {', '.join(sorted(schemes))}"
                for digest, schemes in sorted(prints.items())
            )
            warnings.append(
                f"exp{spec.number}: series in one figure carry "
                f"{len(prints)} DIFFERENT measurement fingerprints — {detail}. "
                f"They were produced by different measurement code; a "
                f"difference between their curves is not necessarily a "
                f"difference between the schemes."
            )

    # A COMPANION CURVE is a second column of the same run, not another
    # scheme, so it is drawn once (from the first series that has it) in a
    # neutral dashed grey. Exp. 3 is the case: the claim is the gap
    # between `tokens_issued` and `option_d_tokens_issued`, and both are
    # measured columns of one results.csv.
    if companion_metric is not None:
        for series in series_list:
            cvals, cerrs = _panel_values(series, companion_metric, per_x)
            if not cvals:
                continue
            ax.errorbar(
                series.x, cvals, yerr=cerrs,
                label=companion_label or f"secondary {companion_metric}",
                color="#666666", linestyle="--", marker="None",
                linewidth=1.0, elinewidth=0.8, capsize=2, zorder=1,
            )
            break

    if add_xlabel:
        # A cross-folder panel sweeps its own variable, so it labels its own
        # axis; everything else inherits the figure's.
        ax.set_xlabel(xlabel or spec.xlabel)
    ax.set_ylabel(ylabel)
    if spec.log_x or force_log_x:
        # Same guard as log_y below, for the same reason: a log axis silently
        # drops non-positive values, so a variable_value of 0 would vanish from
        # the figure without any indication it had been read.
        all_x = [v for s in series_list for v in s.x]
        if all_x and min(all_x) > 0:
            ax.set_xscale("log")
        else:
            warnings.append(
                f"exp{spec.number}: log x-axis requested but data contains "
                f"non-positive values; drew linear instead so nothing is hidden"
            )
    if spec.log_y and allow_log_y:
        # Only if every plotted value is strictly positive — a zero or negative
        # would be silently dropped by a log axis, which would hide data.
        all_y = [v for s in series_list
                 for v in _panel_values(s, metric, metric_name=metric_name)[0]]
        if all_y and min(all_y) > 0:
            ax.set_yscale("log")
        else:
            warnings.append(
                f"exp{spec.number}: log y-axis requested but data contains "
                f"non-positive values; drew linear instead so nothing is hidden"
            )
    # X-AXIS BREATHING ROOM. Matplotlib fits the axis tightly to the data, so
    # the first and last swept points sit exactly on the frame edge -- every
    # figure's leftmost/rightmost marker reads as clipped by the border. Same
    # fix as the Y-axis headroom below: extend the LIMITS symmetrically, never
    # crop a point or hide data.
    if ax.get_xscale() == "log":
        lo, hi = ax.get_xlim()
        if lo > 0 and hi > lo:
            pad = 0.06 * (math.log10(hi) - math.log10(lo))
            ax.set_xlim(10 ** (math.log10(lo) - pad), 10 ** (math.log10(hi) + pad))
    else:
        lo, hi = ax.get_xlim()
        if hi > lo:
            pad = 0.05 * (hi - lo)
            ax.set_xlim(lo - pad, hi + pad)

    ax.grid(True, which="major", linewidth=0.3, alpha=0.5)
    if spec.log_x or spec.log_y:
        ax.grid(True, which="minor", linewidth=0.2, alpha=0.3)

    # HEADROOM. These curves span up to six decades (0.08 ms for the proposed
    # scheme against 150,000 ms for Ref[41] in Exp. 3), and matplotlib fits the
    # axis tightly to the data. The legend then sits ON the topmost series and
    # everything reads as squeezed into the lower half. Add room above the data
    # for the legend, and a little below so the lowest series is not on the
    # frame. Done by extending the LIMITS, never by clipping: no point moves and
    # nothing is hidden.
    #
    # The legend now sits INSIDE the axes (see below), so it needs more room
    # than the 0.25-decade margin an outside legend wanted -- otherwise `best`
    # is choosing between corners that are all occupied.
    # The added band is a FRACTION of the data's own span, not a fixed number of
    # decades: a five-row legend costs roughly a third of the axes height, and a
    # fixed +0.75 decades that clears the curves in Exp. 5 (two decades) is
    # invisible in Exp. 2 (seven). Capped so a very wide span does not push the
    # data into a strip at the bottom.
    if ax.get_yscale() == "log":
        lo, hi = ax.get_ylim()
        if lo > 0 and hi > lo:
            span = math.log10(hi) - math.log10(lo)
            grow = min(0.72 * span, 4.6) if add_legend else 0.10 * span
            ax.set_ylim(10 ** (math.log10(lo) - 0.25),
                        10 ** (math.log10(hi) + grow))
    else:
        # One-sided, and never below zero. `ax.margins(y=...)` expands BOTH
        # directions, which on Exp. 1 put the floor at -100 ms -- a negative
        # latency, which is not a quantity. Every metric plotted here is a
        # duration, a count or a ratio, so zero is a real floor: hold it when
        # the data does, and spend the whole margin above, where the legend is.
        #
        # The 0.65 band exists to hold the LEGEND. A panel without one needs
        # only breathing room, and on a stacked figure the difference is
        # stark: Exp. 8's max-utilization panel spent two thirds of its height
        # empty because it inherited a margin sized for a legend it does not
        # carry.
        lo, hi = ax.get_ylim()
        if hi > lo:
            grow = 0.65 if add_legend else 0.10
            ax.set_ylim(0.0 if lo >= 0 else lo - 0.05 * (hi - lo),
                        hi + grow * (hi - lo))

    # Legend INSIDE the axes, one entry per row.
    #
    # It used to sit above the axes in two expanded columns. That collided: with
    # `mode="expand"` the two columns split the width evenly regardless of label
    # length, and "Proposed (MA-LB-PQ-VDSE)" is far longer than half the figure,
    # so it overprinted "Perera & Fugkeaw [54]" in the second column and both
    # became unreadable. Widening the columns is not available -- the figure is
    # fixed at IEEE single-column width.
    #
    # ncol=1 removes the collision by construction: no entry can ever run into
    # another, at any figure width or label length. `loc="best"` then picks the
    # emptiest corner per figure, which differs by experiment -- upper-left is
    # free in Exp. 1 (one rising series), lower-right in Exp. 2 (all series
    # rise). The extra headroom above keeps a corner genuinely free rather than
    # letting `best` settle on top of a curve.
    #
    # A frame is required here, unlike outside the axes: the legend now overlays
    # gridlines, and unframed text on a grid is what makes a figure look sloppy
    # in print. Opaque white, thin grey edge -- and the headroom means it covers
    # empty space, not data.
    if add_legend:
        ax.legend(frameon=True, ncol=1, loc="best",
                  framealpha=1.0, facecolor="white", edgecolor="0.7",
                  borderpad=0.3, labelspacing=0.22, handlelength=1.5)
        ax.get_legend().get_frame().set_linewidth(0.4)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: Optional[Sequence[str]] = None) -> int:
    # THE WARNINGS CONTAIN NON-ASCII (`§`, `—`) AND THE CONSOLE MAY NOT.
    #
    # On a console whose encoding is not UTF-8, `print()` of a warning raised
    # UnicodeEncodeError and killed the script MID-RUN -- after exp1's warnings
    # and before any figure past it was rendered, so the run looked like it had
    # simply produced fewer figures. `errors="replace"` degrades one character
    # instead of losing seven figures; the file writes are unaffected because
    # matplotlib writes bytes, not through this stream.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass          # a redirected or already-wrapped stream

    parser = argparse.ArgumentParser(
        prog="python3 Plots/generate_plots.py",
        description="Generate the manuscript's eight figures.",
    )
    parser.add_argument("--input", default="Schemes",
                        help="root containing <scheme>/exp<N>_*/results.csv")
    parser.add_argument("--output", default="Plots/output",
                        help="directory to write the PDFs into")
    # Default was "option_d", a key CONSTRUCTIONS no longer has: the second
    # figure family was removed on 2026-09-12 and only "psa" remains. So the
    # plotter crashed with KeyError: 'option_d' on every invocation that did
    # not pass --construction explicitly -- i.e. the command plotgen.md
    # documents. Kept as a compatibility alias per plotgen.md, now defaulting
    # to the one family that exists.
    parser.add_argument("--construction", default="psa",
                        choices=sorted(CONSTRUCTIONS),
                        help="kept for compatibility; there is one figure "
                             "family, the manuscript's eight figures drawn "
                             "from exp<N>_*/, so this only names what is "
                             "already drawn.")
    parser.add_argument("--experiment", default="all",
                        help="all, or a comma-separated subset e.g. 1,2,6")
    parser.add_argument("--require-reportable", action="store_true",
                        help="refuse to plot any series whose run_meta.json "
                             "is not reportable:true")
    parser.add_argument("--format", default="pdf",
                        help="output format(s), comma-separated, e.g. 'pdf' or "
                             "'pdf,png'. SystemConfiguration.md wants vector for the paper, "
                             "so pdf stays the default. With MORE THAN ONE "
                             "format each goes in its own subdirectory "
                             "(<output>/pdf/, <output>/png/) so a raster copy "
                             "can never be picked up where the vector one "
                             "belongs; a single format writes to <output>/ "
                             "directly, unchanged.")
    parser.add_argument("--scale", type=float, default=1.0,
                        help="multiply the figure size. The default 3.5x2.6in "
                             "is IEEE single-column and is what the paper needs; "
                             "use e.g. --scale 1.8 for a copy that is readable "
                             "on screen without changing the paper figures.")
    parser.add_argument("--png-dpi", type=int, default=200,
                        help="raster resolution; 200 is legible on a slide and "
                             "in a review PDF without being enormous")
    args = parser.parse_args(argv)

    if args.scale != 1.0:
        w, h = plt.rcParams["figure.figsize"]
        plt.rcParams["figure.figsize"] = (w * args.scale, h * args.scale)

    specs = CONSTRUCTIONS[args.construction]
    available = {spec.number for spec in specs}
    if args.experiment.strip().lower() == "all":
        wanted = available
    else:
        try:
            wanted = {int(tok) for tok in args.experiment.split(",") if tok.strip()}
        except ValueError:
            raise SystemExit(f"--experiment {args.experiment!r} is not a number list")
        unknown = wanted - available
        if unknown:
            # Names the construction, because 2, 4, 7 and 8 exist for option_d
            # and have no psa form at all -- D1-D9 are construction and
            # experiment-design divergences, not a fork of the whole harness.
            raise SystemExit(
                f"no such experiment(s) for --construction "
                f"{args.construction}: {sorted(unknown)} "
                f"(have {sorted(available)})"
            )

    input_root = Path(args.input)
    output_root = Path(args.output)
    if not input_root.is_dir():
        raise SystemExit(f"--input {input_root} is not a directory")

    written: List[str] = []
    skipped: List[str] = []
    all_warnings: List[str] = []

    for spec in specs:
        if spec.number not in wanted:
            continue
        found = collect(input_root, spec)
        if args.require_reportable:
            kept = [s for s in found if s.reportable is True]
            for s in found:
                if s.reportable is not True:
                    all_warnings.append(
                        f"exp{spec.number}: dropped {s.scheme} "
                        f"(--require-reportable, reportable={s.reportable})"
                    )
            found = kept
        fmts = [f.strip().lstrip(".").lower()
                for f in args.format.split(",") if f.strip()] or ["pdf"]
        multi = len(fmts) > 1
        rendered_any = False
        for fmt in fmts:
            name = spec.filename if fmt == "pdf" else re.sub(
                r"\.pdf$", f".{fmt}", spec.filename)
            target_dir = output_root / fmt if multi else output_root
            target_dir.mkdir(parents=True, exist_ok=True)
            ok, warns = render(spec, found, target_dir / name,
                               dpi=args.png_dpi if fmt == "png" else None,
                               input_root=input_root,
                               strict=args.require_reportable)
            # Warnings describe the DATA, not the format, so collect them once
            # rather than repeating every reportability warning per format.
            if not rendered_any:
                all_warnings.extend(warns)
            if ok:
                rendered_any = True
                rel = f"{fmt}/{name}" if multi else name
                written.append(f"{rel}  ({len(found)} scheme(s): "
                               f"{', '.join(sorted(s.scheme for s in found))})")
        if not rendered_any:
            skipped.append(f"exp{spec.number}")

    for warning in all_warnings:
        print(f"  WARNING {warning}")
    print()
    for line in written:
        print(f"  wrote {output_root / line.split('  ')[0]}"
              f"{line[len(line.split('  ')[0]):]}")
    if skipped:
        print(f"\n  no data, not written: {', '.join(skipped)}")
    # `written` counts FILES; with --format pdf,png that is two per experiment,
    # so reporting it against the experiment count printed "16/8".
    n_figs = len({w.split("  ")[0].split("/")[-1].rsplit(".", 1)[0] for w in written})
    extra = f" ({len(written)} files)" if len(written) != n_figs else ""
    print(f"\n{n_figs}/{len(wanted)} figure(s) written to {output_root}{extra}")

    # A missing figure is not an error — partial campaigns are expected while
    # schemes finish on different instances. Exit non-zero only if nothing at
    # all was produced, which usually means --input points somewhere wrong.
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
