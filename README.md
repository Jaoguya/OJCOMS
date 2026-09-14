# MA-LB-PQ-VDSE

Reference implementation and benchmark harness for **MA-LB-PQ-VDSE** —
*Multi-Authority Load-Balanced Post-Quantum Verifiable Dynamic Searchable
Encryption for IoMT Data Sharing* — together with four published baseline
schemes it is evaluated against.

This repository is the experimental half of the paper in
[`Overleaf/MA-LB-PQ-VDSE.tex`](Overleaf/MA-LB-PQ-VDSE.tex). Every figure in
that paper's Evaluation section is produced by the code here.

**Status:** the benchmark campaign is complete. 39 result directories, 38 of
them reportable, measured at *n* = 10 repetitions with 5 warm-ups on a pinned
AWS `m6i.xlarge` fleet against the frozen Synthea corpus. The eight figures in
[`Plots/output/`](Plots/output/) are generated from exactly those directories.

---

## The problem

Internet of Medical Things (IoMT) deployments generate encrypted health records
that must be searched across several administrative domains — hospital,
laboratory, emergency care — each governed by its own attribute authority.
Existing searchable-encryption schemes assume a centralised search server and a
static authorization model. That combination breaks down in three places:

- **Authorization and index state evolve at different scopes.** An unrelated
  authority advancing its revocation state should not invalidate index entries
  it does not govern.
- **Resource-only load balancing misprices encrypted search.** The least-loaded
  node may not hold the required shard, may hold stale authority state, or may
  face expensive proof generation.
- **A returned content identifier proves nothing on its own.** It cannot show
  that its index entry belongs to the current authenticated index and policy
  state.

## What OJCOMS is

**OJCOMS** is this project's name for the proposed scheme — MA-LB-PQ-VDSE. In
the code it is the `ma_lb_pq_vdse` package; in the figures it is labelled
*Proposed*. It is a cloud–fog framework that unifies:

| Component | What it does |
|---|---|
| **VAP** — Version-Bound Authorization Profile | binds a user's attributes to the current states of the authorities that issued them |
| **Policy-state-aware index** | binds each token to only the authorities governing its policy, so unrelated updates do not invalidate it |
| **AASS** — Adaptive Authorization-Aware Search Scheduler | assigns each shard to an eligible Fog Search Node by predicted index, verification, synchronization and queue cost |
| **DIAS** — Dependency-Aware Incremental Authorization Synchronization | propagates an authority update only along its dependency closure |
| **Verifiable retrieval** | Merkle proofs plus blockchain-anchored commitments bind each returned entry to its ciphertext and policy state |
| **ML-KEM-768** | post-quantum protection for attribute-key delivery |

The construction rests on one policy-bound token, `T = H(w‖PID‖PV‖Dom)`, which
is what makes the token's validity scope a *policy*, not the whole index.

Full detail: **[OJCOMS.md](OJCOMS.md)**.

## The schemes

Five schemes are implemented. Baselines are referred to by their citation
number in the manuscript — never by author name — and each has one document:

| Doc | Scheme | Paper | Code |
|---|---|---|---|
| [OJCOMS.md](OJCOMS.md) | Proposed (MA-LB-PQ-VDSE) | this repository's manuscript | `Schemes/ma_lb_pq_vdse/` |
| [30.md](30.md) | Scheme 30 | Ge *et al.*, Peony / Peony++ | `Schemes/yue_ge/` |
| [35.md](35.md) | Scheme 35 | Guo *et al.*, forward-private VDSSE | `Schemes/guo_vdsse/` |
| [41.md](41.md) | Scheme 41 | Thingom *et al.*, PQ-ABSE | `Schemes/thingom_pq_abse/` |
| [54.md](54.md) | Scheme 54 | Perera and Fugkeaw, LV-PQ-ABSE | `Schemes/perera_lv_pqabse/` |

> **Scheme 41 is measured differently from the other four.** Its search is
> `O(N)` pairings with no early termination, so the published `10⁴–10⁶` sweep
> cannot be executed within the compute budget. Its large-`N` points are
> **derived from a fitted linear model**, not measured, and are drawn with
> hollow markers. See [41.md](41.md) — this distinction is load-bearing and
> must never be flattened.

## The experiments

Eight experiments. The code once carried a ninth number; it was folded into
Experiment 4 on 2026-09-12 and `parse_experiments("all")` now returns 1-8.

| # | Measures | Figure |
|---|---|---|
| 1 | Policy-state-aware token generation vs `q` | `fig_exp1_trapdoor` |
| 2 | Search latency vs index size `N` | `fig_exp2_search` |
| 3 | Cross-domain search latency vs domains `d` | `fig_exp3_crossdomain` |
| 4 | Verification overhead vs returned results `r` | `fig_exp4_verify` |
| 5 | Dynamic index update vs modified pairs `k` | `fig_exp5_update` |
| 6 | DIAS synchronization ablation | `fig_exp6_sync` |
| 7 | AASS search throughput vs concurrency | `fig_exp7_throughput` |
| 8 | Load-balancing effectiveness vs concurrency | `fig_exp8_balance` |

Experiment 4 has two arms: the default sweeps `r` (what verification costs),
the `granularity` arm sweeps the tamper count `t` (what it buys).
Experiments 6, 7 and 8 are proposed-scheme ablations with no baseline.

Which scheme runs which experiment, and what each one measures, is the
participation matrix in [skill.md](skill.md) §4.

---

## What the measurements show

All figures below: *n* = 10, 5 warm-ups, Synthea corpus, `m6i.xlarge`,
BLAS pinned to one thread. **Measured**, not derived, unless a row says
otherwise. Experiments 1 and 4-8 were re-run against the rebuilt construction at
`sharding.replication: 2`; Experiments 2 and 3 carry the frozen baseline numbers
and are noted where their provenance differs.

### Search scales sublinearly where the baselines do not (Exp. 2)

Mean search latency, milliseconds; one point is one index size `N`.

| `N` | Proposed | Scheme 30 | Scheme 35 | Scheme 41 | Scheme 54 |
|---:|---:|---:|---:|---:|---:|
| 10⁴ | **0.486** | 14.84 | 4,468 | 737,651 † | 0.290 |
| 10⁵ | **0.853** | 54.28 | 41,103 | 7.37 × 10⁶ † | 1.149 |
| 10⁶ | **6.040** | 441.9 | 402,413 | 7.37 × 10⁷ † | 13.17 |

† Scheme 41 is measured only at `N` = 10⁴; the rest are fitted. See
[41.md](41.md).

> **Provenance note.** The proposed scheme's Exp. 2 row is restored from the
> archive (2026-09-03, commit `79a5739e`) and was measured at
> `sharding.replication: 1`, before the construction was rebuilt. It is
> reportable and on the campaign host, but it is the one proposed-scheme series
> in this section not produced by the current campaign. The baseline Exp. 2 and
> Exp. 3 numbers are deliberately frozen — see [CLAUDE.md](CLAUDE.md).

Scheme 54 is genuinely *faster than the proposed scheme at small `N`* (0.290 ms
vs 0.486 ms at 10⁴) and loses only as the index grows — 2.2× behind at 10⁶.
That crossover is the honest shape of the result and is reported as such.

### Updates touch only the affected pairs (Exp. 5)

Mean update latency, milliseconds; one point is `k` modified keyword-record
pairs.

| `k` | Proposed | Scheme 30 | Scheme 35 |
|---:|---:|---:|---:|
| 10³ | **16.29** | 333.9 | 596.4 |
| 10⁴ | **157.4** | 2,997 | 5,968 |
| 10⁵ | **1,532** | 29,006 | 60,369 |

19× faster than Scheme 30 and 39× faster than Scheme 35 at `k` = 10⁵.

### Verification discards the tampered records, not the result set (Exp. 4)

With `r` = 20,000 returned records and `t` tampered, records discarded:

| `t` | Proposed | Scheme 30 | Scheme 35 |
|---:|---:|---:|---:|
| 200 | **200** | 20,000 | 19,975 |
| 500 | **500** | 20,000 | 19,975 |
| 1,000 | **1,000** | 20,000 | 19,975 |

Per-record proofs let a client keep every untampered record; the baselines'
whole-result-set digest forces the entire response to be discarded. The cost
side of that trade is real and is charged in the same figure: verification is
`O(r)` chain anchors, 1,623 ms at `r` = 1,000 against a live Hyperledger Fabric
v2.5 network.

> The `granularity` arm above is the one directory of 39 that is **not
> reportable**: it ran against the in-process hash chain rather than Fabric,
> because `O(r)·T_BC` at `r` = 20,000 costs roughly 36 hours on the real ledger.
> `run_meta.json` records this, and the figure's chain-consistency cost is
> understated as a result.

### DIAS buys bandwidth, not latency (Exp. 6)

One point is the fraction of policies an authority update affects. Corpus-backed,
8,000 records across 40 policies.

| Affected ratio | DIAS | Incremental-all | Full-state |
|---:|---:|---:|---:|
| 0.10 | 152.7 ms / 1,448 KB | 157.7 ms / 2,895 KB | 1,412 ms / 26,929 KB |
| 0.50 | 685.6 ms / 6,728 KB | 707.9 ms / 13,455 KB | 1,411 ms / 26,929 KB |
| 1.00 | 1,367 ms / 13,464 KB | 1,410 ms / 26,928 KB | 1,407 ms / 26,929 KB |

Against full-state resynchronization DIAS wins on both axes — 9.2× at a 10%
affected ratio. Against incremental-all its advantage is **bandwidth alone**:
exactly half the bytes at every ratio, at a latency that is within ~3%. The
dependency closure is what halves the payload; the time is dominated by
re-tokenization either way.

### AASS is the only arm that never forwards across nodes (Exp. 7, 8)

At concurrency 10,000, four scheduler arms:

| Arm | Throughput (q/s) | Imbalance | Cross-node forwards | Max queue |
|---|---:|---:|---:|---:|
| **AASS** | 1,030.8 ± 17.7 | 0.052 | **0** | 1,058 |
| Least-loaded | 1,024.4 ± 12.4 | **0.017** | 20,542 | 2,088 |
| Round-robin | 974.6 ± 22.3 | 0.134 | 21,875 | 2,726 |
| No load balancing | 546.1 ± 7.2 | 0.397 | 21,250 | 7,917 |

Read this as a **trade, not a sweep**. AASS and least-loaded are tied on
throughput — their 95% confidence intervals overlap — and least-loaded achieves
*better* raw imbalance (0.017 vs 0.052). What separates them is the column
resource-only scheduling cannot see: least-loaded reaches its balance by
forwarding 20,542 queries to nodes that did not hold the shard, while AASS
forwards none, because eligibility is part of its cost function rather than an
afterthought. Section VI reports this as a trade-off, not as dominance.

### Where the proposed scheme loses (Exp. 1)

Token generation, `q` = 20 keywords, `|P_U|` = 1:

| Scheme | Mean |
|---|---:|
| Scheme 35 | **0.0097 ms** |
| Scheme 30 | 0.0789 ms |
| Proposed | 0.129 ms |
| Scheme 54 | 1.175 ms |
| Scheme 41 | 411.0 ms |

Binding a token to policy and authority state costs roughly 1.6× Scheme 30 and
13× Scheme 35 at token generation. This is the price paid for the Exp. 2 and
Exp. 5 results above, and it is reported rather than buried: an online cost that
is linear in `q` buys sublinear search and per-record verification.

---

## Repository layout

```
Overleaf/              the manuscript (.tex) — the specification
Schemes/               one directory per scheme: src/ plus expN_*/ result folders
  ma_lb_pq_vdse/src/     aim/ authority/ chain/ fsn/ index/ psa/
                         scheduler/ shard/ sync/ user/ verify/ harness/
Common/                shared crypto primitives and timing
Dataset/               corpus preparation, loading, verification
Experiment Configuration/  global.yaml, crypto.yaml, dataset.yaml,
                           index.yaml, scheduler.yaml, workload/
Plots/                 generate_plots.py and the rendered figures
infra/                 provisioning, fleet control, sweep sharding, Fabric
References/            transcripts of the baseline papers
```

Results live beside the code that produced them: each run writes
`raw_runs.csv`, `results.csv` and `run_meta.json` into
`Schemes/<scheme>/exp<N>_*/`.

## Requirements

- Python 3.11
- `pip install -r requirements.txt`
- **Linux** for Scheme 41 — it needs `charm-crypto` for its published Type-I
  SS512 curve, which has no macOS or Windows wheel. The other four schemes run
  anywhere.
- `liboqs` or the `cryptography` package for ML-KEM-768.
- Hyperledger Fabric v2.5 for Experiment 4's chain anchors —
  `infra/fabric/network.sh` brings up the network. Without it the harness falls
  back to an in-process hash chain and marks the run **not reportable**.

## Running it

```bash
python3 -m Schemes.ma_lb_pq_vdse.src.main    --experiment all       --runs 10
python3 -m Schemes.yue_ge.src.main           --experiment 1,2,3,4,5 --runs 10
python3 -m Schemes.guo_vdsse.src.main        --experiment 1,2,3,4,5 --runs 10
python3 -m Schemes.thingom_pq_abse.src.main  --experiment 1,2,3     --runs 10
python3 -m Schemes.perera_lv_pqabse.src.main --experiment all       --runs 10

python3 Plots/generate_plots.py --input Schemes --output Plots/output
```

The flags are **not uniform across schemes** — they were written at different
times. **[skill.md](skill.md)** is the operational guide: experiment framework,
configuration, execution, output validation and result collection. Figures are
**[plotgen.md](plotgen.md)**. Read both before running a campaign.

Tests: `python3 -m pytest -q` from the repository root.

## Reportability

A run always completes and always writes results. Whether those results may be
**quoted in the paper** is a separate, machine-checked question recorded in
`run_meta.json` as `reportable` plus a `not_reportable_because` list. A run
qualifies only against the frozen Synthea corpus, on the campaign host, at the
configured replication count, with the repetition count the manuscript claims.
Never put a number in the manuscript without reading that field — see
[skill.md](skill.md).

```bash
python3 -c "import json,glob
for f in sorted(glob.glob('Schemes/*/exp*/run_meta.json')):
    m = json.load(open(f))
    print('OK ' if m['reportable'] else 'NO ', f)"
```

## Relationship to the paper

The manuscript is the specification; the code is the source of truth for what
was actually measured. Where they disagree, the scheme document records the
disagreement rather than resolving it silently — see *Where it still departs
from the paper* in [OJCOMS.md](OJCOMS.md), and the equivalent sections in the
baseline documents.

No number in this repository is fabricated, and no baseline is held to a weaker
standard than the proposed scheme. Where a baseline is slow, that is recorded as
a finding.
