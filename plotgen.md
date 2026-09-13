# plotgen.md — figures

How the manuscript's figures are produced, what each one draws, and the rules
that keep a figure honest about where its numbers came from.

Running experiments is [skill.md](skill.md). Scheme internals are in
[OJCOMS.md](OJCOMS.md), [30.md](30.md), [35.md](35.md), [41.md](41.md) and
[54.md](54.md).

---

## 1. Command

```bash
python3 Plots/generate_plots.py --input Schemes --output Plots/output
```

| Flag | Default | Effect |
|---|---|---|
| `--input` | `Schemes` | root holding `<scheme>/exp<N>_*/results.csv` |
| `--output` | `Plots/output` | where figures land |
| `--experiment` | `all` | or a comma-separated subset, e.g. `1,2,6` |
| `--require-reportable` | off | refuse to plot any series whose `run_meta.json` is not `reportable: true` |
| `--format` | `pdf` | comma-separated; more than one writes `pdf/` and `png/` subdirectories |
| `--scale` | `1.0` | multiplies figure size; the default is IEEE single-column |
| `--png-dpi` | `200` | raster resolution |
| `--construction` | `psa` | kept for compatibility; there is one family |

**Use `--format pdf,png` for anything you intend to circulate.** With two
formats each goes in its own subdirectory, so a raster copy can never be picked
up where the vector one belongs. A single format writes to `--output` directly.

A scheme with no results is skipped rather than failing the run, so a partial
campaign still plots.

---

## 2. Experiment to figure

Eight figures, matching the manuscript's `\includegraphics` paths.

| Exp. | File | x | y |
|---|---|---|---|
| 1 | `fig_exp1_trapdoor.pdf` | queried keywords `q` | token generation latency (ms), log |
| 2 | `fig_exp2_search.pdf` | index size `N` (records), log | search latency (ms), log |
| 3 | `fig_exp3_crossdomain.pdf` | domains `d` | cross-domain latency (ms), log |
| 4 | `fig_exp4_verify.pdf` | returned results `r` / tampered `t` | two panels, log |
| 5 | `fig_exp5_update.pdf` | updated pairs `k`, log | update latency (ms), log |
| 6 | `fig_exp6_sync.pdf` | affected-policy ratio | two panels, log y |
| 7 | `fig_exp7_throughput.pdf` | concurrent queries | throughput (q/s) |
| 8 | `fig_exp8_balance.pdf` | concurrent queries | three panels |

**There is no Experiment 9.** Section VI defines one Experiment 4 that makes a
two-part claim, drawn as a two-panel figure. It was folded in on 2026-09-12 and
now runs as Exp. 4's second ARM:

| Arm | Directory | Sweeps | Panel |
|---|---|---|---|
| default | `exp4_verification_overhead/` | `r` = 10 … 1000 | (a) what verification costs |
| `granularity` | `exp4_verification_overhead__granularity/` | `t` = 1 … 1000, `r` pinned at 20,000 | (b) what it buys |

`--experiment 4` runs both, because a figure with one panel missing is not the
figure Section VI describes.

### Multi-panel figures, and their order

Panels are indexed **positionally**. Reordering a scheme's `secondaries` tuple
silently relabels a panel, so the order below is load-bearing.

| Figure | (a) | (b) | (c) |
|---|---|---|---|
| Exp. 4 | verification latency per result | records discarded (the `granularity` arm) | — |
| Exp. 6 | synchronization latency (ms) | DIAS payload delivered (KB) | — |
| Exp. 8 | utilization std. dev. | max node utilization | cross-node forwards |

Two panel choices are deliberate:

- **Exp. 4(a) is `T_verify / r`**, the per-result average §VI reports. The raw
  column stays total latency — a measured quantity — and the division happens
  here, so nothing derived is stored as if it were measured.
- **Exp. 4(b) counts RECORDS, not index entries**, the same unit as panel (a).
  One bundle per returned ciphertext. Both are panels of one figure, so a
  mismatch would put (a) in records and (b) in entries under one caption.
- **Exp. 6(b) is delivered bytes, not a node count.** An in-process harness
  models no network, so selective propagation barely moves sender-side latency;
  its cost is in payload. A latency-only figure would leave half the DIAS claim
  with no evidence. The value is measured directly by the runner, never derived
  as message size × recipients — the arms send different message mixes and a
  product would charge `full_state` for bytes it does not send.

---

## 3. How a figure states its provenance

This is the part that must not be quietly changed.

### Measured vs derived

A point whose `results.csv` `measurement_type` column reads `projected` is drawn
with a **hollow marker and no error bar**, visibly bare beside a measured point.
Only **Scheme 41** emits that column, for its Experiment 2 points above
`N = 10⁴`; every other scheme measures every point. The full equation, its
anchors and its residual are in [41.md](41.md).

The plotter reads the column rather than inferring from `n_runs`: a projected
row carries `n_runs = 1`, not 0, so an `n_runs == 0` rule would draw it solid
and indistinguishable from a measurement while the caption claimed otherwise.

### Reportability

`--require-reportable` drops any series whose `run_meta.json` says
`reportable: false`. Without the flag such series are drawn — useful during
development, never acceptable for a figure going into the paper. Check the
field before quoting anything; [skill.md](skill.md) §8 lists the conditions.

### Configuration agreement

`global.yaml` fixes the parameters that make two schemes comparable on one axis
— query size, replication count, warm-ups, the sweep values — and its own header
says changing it invalidates every existing `results.csv`. Each run records the
file's hash faithfully, and the plotter **warns when two schemes drawn in one
figure carry different hashes**. One figure has previously carried four
different values across six runs; the warning exists because nothing else
catches it.

### Panel labels

Every secondary panel names the metric it expects (`metric_name`) and the
plotter checks that against `run_meta.json`'s `secondary_metrics` before
drawing. A run written before that field existed cannot be verified and the
figure says so. This is what stops a panel labelled "cross-node forwards" from
drawing peak queue depth.

---

## 4. Series identity

Series are labelled by citation number, never by author name:

| Key | Label |
|---|---|
| `ma_lb_pq_vdse` | `Proposed` |
| `yue_ge` | `Scheme [30]` |
| `guo_vdsse` | `Scheme [35]` |
| `thingom_pq_abse` | `Scheme [41]` |
| `perera_lv_pqabse` | `Scheme [54]` |

- Marker, colour **and** linestyle all vary, so figures survive grayscale.
- The proposed scheme is pinned to style index 0, so it looks the same in every
  figure even when a baseline is absent from one.
- Legend order is kept separate from style assignment, so reordering the legend
  can never reassign a scheme's marker or colour.

**Experiment 1 is two panels, and that is deliberate.** §VI sweeps `q` *and*
the authorization scope, which are two different questions:

| Panel | x | Series |
|---|---|---|
| (a) | `q` | five schemes, one curve each — the between-scheme comparison |
| (b) | `\|P_U\|` at `q = 5` | the proposed scheme alone, four points, **log–log** |

Both used to be panel (a): the proposed scheme drew a curve per `|P_U|` from
`exp1_trapdoor_generation__pu<N>/` alongside four baselines. Eight series in an
IEEE single column, and worse than cluttered — four of them were *one* scheme
at four scopes and four were *four* schemes, so a reader could not tell which
spread meant "scheme A vs scheme B" and which meant "the same scheme paying
more". **A within-scheme parameter does not belong on a between-scheme axis.**

Panel (b) is drawn log–log because `|P_U|` is sampled geometrically (1,2,4,8)
and `|T_Q| = q·|P_U|` is *linear* in it, so only log–log renders that as the
straight line it is. It is also the only place `tab:cost`'s `O(|T_Q|)T_H` row
is checked against data now that `test_cost_table_agreement.py` has the arms it
needs but the figure no longer overlays them.

`PanelSpec.scope_arms` / `scope_at_x` build it: one point per arm folder at a
fixed x, plotted against the arm's own parameter. A missing arm draws short and
warns rather than passing an incomplete curve off as complete.

The figure also restricts x to `{1, 5, 10, 15, 20}`: the baselines were
measured at every integer 1–20, and §VI's figure shows five points. Nothing
measured is discarded from any file, only from the plot — all four `pu`
directories stay on disk with their `results.csv` intact.

---

## 5. One figure family

Every scheme — proposed and baselines alike — writes `exp<N>_*/`, and the
plotter draws one family from them.

There used to be two constructions writing `exp<N>_*/` and `psa_exp<N>_*/`, and
a second figure family to keep them apart. The proposed scheme implements the
manuscript's construction only; the prefix was dropped on 2026-09-12 and the
second family with it, since both would now glob the same directories and a
duplicate view of one dataset is how two figures of the same numbers end up in
one paper.

`--construction` survives as a compatibility alias accepting only `psa`.

## 6. Before a figure goes in the paper

1. `run_meta.json` says `reportable: true` for **every** series drawn.
2. The plotter emitted no config-hash warning, **except the one Figs. 2 and 3
   are known to carry** — see below.

   > **Accepted config-hash exception, Figs. 2 and 3.** Those figures' baseline
   > curves are the frozen ones, measured under `global.yaml` `032de0d9…`,
   > while everything measured after the 2026-09-12 rewrite carries
   > `2bb41bb1…` (the revision that took `repetitions` from 30 to 10, among
   > others). The plotter therefore warns that Fig. 2 and Fig. 3 mix
   > revisions, and it is right to.
   >
   > **Do not "fix" this by re-running the frozen baselines.** The user
   > reviewed it on 2026-09-13 and accepted the existing results as
   > satisfactory. The warning is disclosure, not a defect to clear, and the
   > cost of clearing it is re-running four schemes' Exp. 2 and Exp. 3 —
   > including Scheme 35's Exp. 2, which needs ~52 GB at `N = 10^6`.
   >
   > Every other figure must still be warning-free: Exps. 1, 4, 4b and 5 were
   > re-run at the current revision precisely so that they are.
3. Any hollow marker is explained in the caption, and the caption's description
   of how those points were derived matches what the code did.
4. Panel labels were verified against `secondary_metrics` — no warning.
5. The axis means the same thing for every scheme on it: `N` counts records,
   `d` counts participating domains, `r` counts returned entries.
