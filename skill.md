# skill.md — the experiment framework

The operational guide to this benchmark. If you need to understand, configure,
run, validate or reproduce the experiments, start here.

Scheme-specific construction, parameters and quirks are **not** in this file.
They are in [OJCOMS.md](OJCOMS.md), [30.md](30.md), [35.md](35.md),
[41.md](41.md) and [54.md](54.md). Figures are [plotgen.md](plotgen.md).

---

## 1. The workflow

```
skill.md  ->  pick experiment  ->  pick scheme  ->  read that scheme's file
          ->  check configuration  ->  run  ->  validate run_meta.json
          ->  merge shards (if sharded)  ->  figures (plotgen.md)
```

Each step below is verified against the current code.

---

## 2. Where the numbers come from

One chain, each link derived from the one before:

```
tab:cost  ->  the experiments  ->  the results  ->  Section VI's prose
```

The **computation-cost table** (`Overleaf/MA-LB-PQ-VDSE.tex`, `tab:cost`) says
what should scale with what. **The experiments** are designed to sweep exactly
those variables — that is why Exp. 1 sweeps `q` and `|P_U|` (the row is
`O(|T_Q|)T_H` and `|T_Q| = q·|P_U|`), why Exp. 5 sweeps `k`, and why Exp. 6
sweeps the affected-policy ratio. **The results** are what those sweeps produce.
**Section VI's prose** describes the results, and is written after they exist.

Nothing restates the table. It was copied into test code once — two lists of
cells-as-strings — and the copy drifted out of step with the construction while
its assertions silently skipped. The one check that remains
(`test_cost_table_agreement.py`) asserts a structural invariant the table
implies, against the data: `O(|T_Q|)T_H` means `q=20,|P_U|=1` must cost what
`q=5,|P_U|=4` costs. That is checkable without naming a single cell.

---

## 3. Experiment structure

Every experiment is a **one-variable sweep**. One parameter is swept; every
other parameter is held at `Experiment Configuration/global.yaml`'s `defaults`.
Index construction is **setup, not measurement** — only the operation under
test is timed, though setup is where most of the wall-clock goes.

### Numbering and sweeps

Defined in `global.yaml` under `experiments:`. These are the values the
**figures** draw; a scheme may measure a superset and the extra points stay in
its `results.csv`.

| # | Folder stem | Variable | Sweep values |
|---|---|---|---|
| 1 | `exp1_trapdoor_generation` | `keywords_per_query` (q) | 1, 5, 10, 15, 20 — and policy scopes 1, 2, 4, 8 |
| 2 | `exp2_search_latency` | `index_size` (N, in **records**) | 10^4, 5·10^4, 10^5, 5·10^5, 10^6 |
| 3 | `exp3_crossdomain_scalability` | `domains` (d) | 2, 4, 6, 8, 10 |
| 4 | `exp4_verification_overhead` | `returned_results` (r) | 10, 50, 100, 500, 1000 |
| 5 | `exp5_keyword_update` | `keyword_document_pairs` (k) | 10^2, 10^3, 10^4, 10^5 |
| 6 | `exp6_authorization_sync` | `affected_policy_ratio` | 0.1, 0.25, 0.5, 0.75, 1.0 |
| 7 | `exp7_search_throughput` | `concurrency` | 100, 500, 1000, 2500, 5000, 10000 |
| 8 | `exp8_load_balance` | `concurrency` | same as Exp. 7 — **shares its runs** |

Exp. 8 sets `shares_runs_with: exp7_search_throughput`: the trace is run once
per variant and both metric sets are emitted.

**Exp. 4 has two arms**, because Section VI's Fig. 4 makes one claim in two
panels over two different variables. The default arm sweeps `r` (panel a, what
verification costs); the `granularity` arm pins `returned_results: 20000` and
sweeps the tamper count `t` (panel b, what it buys). `--experiment 4` runs both.
There is no Experiment 9 — it was folded in on 2026-09-12.

### Global defaults

`keywords_per_query: 5` · `domains: 4` · `fog_search_nodes: 4` ·
`index_size: 100000` · `returned_results: 100`

No scheme may override them. That is the bias-detection rule: identical
`global.yaml` defaults for every scheme, no per-scheme tuning.

### Measurement methodology

`repetitions: 10` retained runs, `warmup_runs: 5` discarded, 95% confidence
intervals, `drop_outliers: false` (a failed run is recorded `status=failed` and
**re-run**, never dropped to clean the sample), latency in ms on
`perf_counter_ns`, throughput on wall clock.

> Changing any value in `global.yaml` **invalidates every `results.csv`
> measured under the old one.** Its hash is recorded in every `run_meta.json`,
> and `generate_plots.py` warns when two schemes in one figure carry different
> hashes. Record any change in the file's own comments, beside the value.

---

## 4. Scheme selection

| Scheme key | Doc | Experiments implemented |
|---|---|---|
| `ma_lb_pq_vdse` | [OJCOMS.md](OJCOMS.md) | 1–9 |
| `yue_ge` | [30.md](30.md) | 1, 2, 3, 4, 5, 9 |
| `guo_vdsse` | [35.md](35.md) | 1, 2, 3, 4, 5, 9 |
| `thingom_pq_abse` | [41.md](41.md) | 1, 2, 3 |
| `perera_lv_pqabse` | [54.md](54.md) | 1, 2, 3, 4 |

**Five schemes, and only five.** XB-Muse (`ref36`) and Zhuang (`ref52`) were
dropped on 2026-09-12: not measured, not implemented, never a baseline in any
experiment. The manuscript's Experiment 1 briefly listed them as baselines; that
was corrected to Scheme 30/35/41/54. `Schemes/zhuang_lattice_mabse/` and its
`crypto.yaml` entry are gone. Both remain as literature citations in Related
Work and Zhuang keeps its `tab:comparison` row — that table surveys the field,
it is not the benchmark.

A scheme absent from an experiment is absent on purpose — the reason is in that
scheme's document and in `global.yaml`'s per-experiment `schemes:` list.
`generate_plots.py` skips schemes with no results, so partial runs still plot.

### Participation matrix

Who runs what. **This table is the single owner of that fact** — the scheme
documents do not repeat it, so adding an experiment is a one-file edit here.

| Exp. | Proposed | 30 | 35 | 41 | 54 |
|---|:--:|:--:|:--:|:--:|:--:|
| 1 token generation | yes | yes | yes | yes | yes |
| 2 search latency | yes | yes | yes | yes* | yes |
| 3 cross-domain | yes | yes | yes | yes | yes |
| 4 verification overhead | yes | yes | yes | — | yes |
| 5 dynamic update | yes | yes | yes | — | — |
| 6 authorization sync | yes | — | — | — | — |
| 7 search throughput | yes | — | — | — | — |
| 8 load balance | yes | — | — | — | — |
| 4 arm `granularity` | yes | yes | yes | — | — |

`*` Scheme 41's Exp. 2 points above `N = 10^4` are **derived, not measured** —
see *Measured vs derived* in §7 and [41.md](41.md).

Why each `—` is a `—`, in one line each; the reasoning is in the scheme file:

| Absence | Reason |
|---|---|
| 41 from 4, 5, 9 | no result-verification and no dynamic-update primitive |
| 54 from 5 | no incremental-update primitive; indexes are built at Phase 3 |
| 54 from 9 | verifies per record already, so the all-or-nothing question does not describe it |
| 30, 35, 41, 54 from 6 | no fine-grained authorization-synchronization primitive |
| 30, 35, 41, 54 from 7, 8 | proposed-scheme scheduler ablations |

### What each experiment measures, per scheme

Primary metric first, then that scheme's secondaries as they appear in its
`results.csv`. Also owned here, for the same reason.

| Exp. | Primary | Proposed | 30 | 35 | 54 | 41 |
|---|---|---|---|---|---|---|
| 1 | latency (ms) | `trapdoor_size`, `tokens` | `token_size_bytes`, `tokens_issued` | `trapdoor_size_bytes` | `trapdoor_size_bytes`, `prf_evaluations` | `trapdoor_size_bytes`, `trapdoors_issued` |
| 2 | latency (ms)† | `n_eff`, `entries_traversed` | `n_eff`, `entries_traversed`, `prune_ratio` | `n_eff`, `entries_traversed`, `prune_ratio` | `n_eff`, `tree_descents`, `prune_ratio` | `wall_clock_ms`, `pairings_computed` |
| 3 | latency (ms) | `trapdoors_issued`, `nodes_searched` | `trapdoors_issued`, `cross_node_msgs`, `results_returned` | `trapdoors_issued`, `cross_node_messages` | `trapdoors_issued`, `cross_node_msgs`, `results_returned` | `trapdoors_issued`, `cross_node_messages` |
| 4 | latency (ms) | `proof_size` (KB), `path_length` | `proof_size_kb`, `entries_combined`, `accepted` | `proof_size_kb`, `proof_elements` | `proof_size_kb`, `path_length` | — |
| 5 | latency (ms) | `entries_retokenized`, `merkle_nodes_recomputed`, `commitments_rebuilt`, `replica_writes` | `entries_rewritten`, `index_growth_bytes`, `delete_ms` | `entries_rewritten`, `index_entries_before` | — | — |
| 6 | latency (ms) | `records_evolved`, `entries_retokenized`, `delivered_kb` (KB), `fsns_touched` | — | — | — | — |
| 7 | throughput (q/s) | `latency_p95`, `rejected` | — | — | — | — |
| 8 | utilization std. dev. | `max_node_utilization`, `cross_node_forwards`, `max_queue_depth` | — | — | — | — |

† Scheme 41's Exp. 2 primary is **aggregate CPU time**
(`search_cpu_time_ms`), not wall-clock latency, because its points are
projected and CPU time is invariant in the worker count.

**Exp. 8's secondary order is the panel order.** `generate_plots.py` indexes
panels positionally; reordering that list silently relabels a panel.

Two Exp. 5/6 metrics changed on 2026-09-13 and mean specific things:

- **`replica_writes` is PHYSICAL, `entries_retokenized` is LOGICAL.** `T'` is
  recomputed once per affected entry — the token does not depend on which node
  stores it — but the entry is written to every node holding that shard, or the
  replicas diverge and a query AASS routes to the stale one returns stale
  results. At `replication: 2` they differ by exactly 2x (204 vs 102 at
  `k = 100`), and the physical count is the one comparable with Scheme 30/35's
  `entries_rewritten`, which comes from `dias.synchronize()` and hits every
  holder.
- **`records_evolved` was called `policies_evolved` and counted the wrong
  noun.** It increments once per RECORD. While Exp. 6 synthesised one record
  per policy the two were indistinguishable — both read 40 — so the mislabel
  was invisible; on real corpus records they differ by the records-per-policy
  factor (811 vs 4 at ratio 0.1) and a reader would have taken 811 as a policy
  count.

### One construction

The proposed scheme implements the manuscript's policy-state-aware form —
`T = H(w || PID || PV || Dom)`, a policy-relevant version vector, commitments
bound to the governing authorities' state — **and nothing else**.

It used to carry two, selected with `--construction`, writing to `exp<N>_*/` and
`psa_exp<N>_*/`. The second construction is gone, and with it the flag, the
`psa_` prefix and the second figure family. **All five schemes now write the
same directory names** — `exp<N>_*/`, with arms as `exp<N>_*__<variant>/` — so
nothing about the proposed scheme's output looks unlike a baseline's.

Alongside any number you report, still state: reportable or not (corpus type,
host, `n_runs`), what one point on the axis is, and measured vs extrapolated.

---

## 5. Configuration

Five YAML files in `Experiment Configuration/`. All five are SHA-256 hashed
into every `run_meta.json`.

| File | Holds |
|---|---|
| `global.yaml` | sweeps, defaults, measurement methodology, topology, authorities, environment, fleet, paths, corpus policy |
| `crypto.yaml` | per-scheme cryptographic parameters — curves, lattice dimensions, attribute counts, Bloom settings |
| `dataset.yaml` | corpus pin, including the frozen SHA-256 |
| `index.yaml` | index structure parameters |
| `scheduler.yaml` | AASS weights and the hold-out sweep that fixed them |

Nothing numeric is hardcoded in a scheme. A parameter a paper never published
is recorded in `crypto.yaml` with a date and a reason.

`Common/crypto/config.py` snapshots the configuration before measurement and
re-checks it at every sweep-point boundary; a mid-run change aborts rather than
producing points measured under two configurations.

---

## 6. Dataset

The corpus is **frozen**. `Dataset/derived/corpus.jsonl` is git-ignored (it is
hundreds of MB) but pinned by SHA-256 in `dataset.yaml` and verified on load.

Current pin: `corpus_type: synthea`, `e56ca2d1...`, **1,143,792 records**,
2,023 distinct keywords, 36.3M keyword–document pairs, mean 31.7 keywords per
record, 10 domains balanced to within one record of equal.

Record schema, one JSON object per line:

```json
{"rid": 0, "pid": "a3f8...", "vid": 1, "dom": 2,
 "ts": "2180-07-23T14:31:00", "kw": ["dx:I10", "rx:aspirin"]}
```

Build it once:

```bash
python3 Dataset/prepare_dataset.py --input <synthea>/output_full/csv \
    --output Dataset/derived --synthea-version 7e08387
```

- **Regenerating the corpus invalidates every result** — all five schemes must
  be re-run, not just the one being worked on.
- `Dataset/dataset_manifest.json` is committed provenance. Do not overwrite it.
  `synthetic_generator.py` refuses to touch it without `--force`; if it appears
  modified in `git status` unintentionally, `git checkout --` it.
- `synthetic_generator.py` produces a same-format development corpus. It is
  **never reportable** — `corpus.reportable_types` admits `synthea` only.
  Identical format is the point: a scheme must not be able to tell which corpus
  it is reading.

---

## 7. Execution

The flags are **not uniform**. Verified against each `src/main.py`:

Every scheme takes the same flags. They diverged for years because the runners
were written at different times; they were unified on 2026-09-12, and the old
spellings (`--output-dir`, `--warmup`) stay accepted as aliases so banked
command lines and fleet scripts keep working.

| Flag | Meaning |
|---|---|
| `--experiment` | comma-separated numbers, or `all` |
| `--runs` / `--warmups` | retained runs / discarded warm-ups |
| `--output` | output root |
| `--points` | run only these sweep values |
| `--require-reportable` | refuse unless every condition holds |
| `--seed` | reproducible keyword selection |

Scheme-specific additions:

| Scheme | Adds |
|---|---|
| `ma_lb_pq_vdse` | `--variant`, `--smoke`, `--dataset`, `--quiet` |
| `yue_ge` | `--variant` (`peony_plus`/`peony`), `--bloom-hashes` (5/13) |
| `thingom_pq_abse` | `--dev`, `--max-seconds-per-run`, `--dataset`, `--config` |
| `guo_vdsse`, `perera_lv_pqabse` | none |

A scheme without `--dataset` reads the corpus from its configured location.

### The common contract

Every scheme obeys all of this. **A scheme document states only where it
deviates** — if its file is silent on a row below, the row holds.

| | |
|---|---|
| Invocation | `python3 -m Schemes.<key>.src.main --experiment <list> --runs 10` |
| Input | the verified Synthea corpus via `Dataset.corpus.load_verified_corpus` |
| Output | `raw_runs.csv`, `results.csv`, `run_meta.json` in `Schemes/<key>/exp<N>_*/` |
| Timing boundary | index construction is setup and is **not** timed |
| Replication | 10 retained runs, 5 discarded warm-ups, 95% CI |
| Provenance | all five config hashes, git commit, host, corpus SHA in `run_meta.json` |
| Sharding | `--points` writes to `..._points-<vals>/`; reassemble with `infra/merge_points.py` |
| Unmeasured points | none — every point is measured, **except Scheme 41 Exp. 2** |

Known deviations, and nothing else:

| Scheme | Deviates by |
|---|---|
| 41 | Exp. 2 above `N = 10^4` is projected; emits a `measurement_type` column; refuses to start without a verified corpus unless `--dev`; Linux-only |
| 30 | `--variant peony` has no `Verify`, so Exp. 4 is refused under it |
| 54 | ML-KEM session establishment measured and printed separately, outside the Exp. 1 curve |
| 35, 54 | Exp. 2 grows one index across nested prefixes, so `--points` buys nothing |

```bash
# proposed scheme, all nine experiments, implemented construction
python3 -m Schemes.ma_lb_pq_vdse.src.main --experiment all --runs 10

# proposed scheme, manuscript construction, Exp. 1 across every policy scope
python3 -m Schemes.ma_lb_pq_vdse.src.main \
    --construction psa --experiment 1 --variant all

# scheduler ablation — all four arms of Exp. 7 and 8
python3 -m Schemes.ma_lb_pq_vdse.src.main --experiment 7,8 --variant all

# DIAS propagation ablation — Exp. 6's three arms
python3 -m Schemes.ma_lb_pq_vdse.src.main --experiment 6 --variant all
```

### Variant vocabularies do not interchange

| Experiment | Variants | Meaning |
|---|---|---|
| 7, 8 | `no_lb`, `round_robin`, `least_loaded`, `aass` (default) | **scheduler** — which FSN serves a query |
| 6 | `dias` (default), `incremental_all`, `full_state` | **DIAS propagation** — how an authorization change spreads |
| 1 | `pu1` (default), `pu2`, `pu4`, `pu8` | the authorization scope `\|P_U\|` |

The Exp. 6 arms carry the manuscript's own names. Passing a scheduler variant to
Exp. 6 is refused — a scheduler
decides which node serves a *query* and plays no part in propagating an
authorization change.

### Splitting work across machines

`--points` runs only some sweep values, so one experiment can be split:

```bash
python3 -m Schemes.thingom_pq_abse.src.main --experiment 3 --points 2-5
python3 -m Schemes.thingom_pq_abse.src.main --experiment 3 --points 6-10
python3 infra/merge_points.py Schemes/thingom_pq_abse/exp3_crossdomain_scalability
```

Each shard writes to its own `..._points-2_3_4_5` directory, so instances
cannot overwrite each other. A `--points` value the experiment does not sweep is
an **error**, not a silent no-op.

`merge_points.py` re-aggregates from `raw_runs.csv`, never by averaging shard
means — that is only valid at equal run counts, and a confidence interval
cannot be recovered from other confidence intervals. It **refuses** to merge
shards that disagree on git commit, corpus hash or config hashes, or that
contain the same sweep value twice.

**Not everything splits.** Schemes 35 and 54 grow *one* index across the Exp. 2
sweep as nested prefixes (`records[:n]`), so they are inherently sequential;
sharding them is allowed but buys nothing.

### Fleet

Use `infra/fleet.sh`, not manual ssh — the manual procedure has already
destroyed completed results.

```bash
./infra/fleet.sh status        # what is running, what is busy, burn rate
./infra/fleet.sh start         # start all, authorise your IP, wait for sshd
./infra/fleet.sh deploy        # archive results, update code, restore results
./infra/fleet.sh run <ip> <tag> <cmd...>   # dispatch ONE job, pin BLAS, self-stop
./infra/fleet.sh reap <ip>...  # queue a self-stop onto an already-running job
./infra/fleet.sh harvest ./out # pull results, selected by provenance
./infra/fleet.sh stop          # STOP, not terminate
```

**Use `run` rather than hand-rolled ssh.** It sets the five BLAS variables
`global.yaml` requires — a miss makes latency depend on core count, silently —
and halts the node the moment the job exits, which is the "stop an idle
instance" rule enforced instead of remembered. `--keep` suppresses the
self-stop when you intend to chain more work onto the same node. `reap` does
the same for a job someone already started another way.

A plain `python3` in the command resolves to the campaign venv: `run` puts
`~/.venv-malbpq/bin` first on PATH. Without that it hits system python and dies
on `ModuleNotFoundError: mmh3` before measuring anything — and since the
command exits, the node self-stops in seconds and a failed run looks exactly
like a fast one.

There is also a CloudWatch alarm per instance (`OJCOMS-autostop-<id>`) that
stops a node whose CPU stays under 1% for **2 hours**. It is a backstop for a
wedged or killed process, not the primary mechanism — it was 30 minutes until
2026-09-13, which is short enough to reap a node mid-`docker pull`, and it did.

`OJCOMS_BRANCH` picks the branch (default `main`); `OJCOMS_KEY` and `OJCOMS_SG`
override key and security group.

- **`deploy` is not `git pull`.** Result files are git-tracked, so
  `git reset --hard` silently restores committed results over fresh ones — that
  is how a completed track was lost once. `deploy` archives `Schemes/` first,
  resets, then restores every result whose `run_meta.json` says
  `corpus_type: synthea`.
- **`harvest` ignores timestamps.** A reset rewrites mtimes, so a stale file can
  look newer than a real one. Selection is by provenance, never by mtime.
- **If every host times out at once**, it is almost certainly your egress IP
  rotating, not dead nodes. `start` and `status` re-authorise automatically.
- **Stop an idle node the moment its work ends.** Restarting costs ~2 minutes;
  idling bills continuously.

### Infrastructure for the proposed scheme

```bash
docker compose -f infra/fabric/docker-compose.yaml up -d
ipfs daemon &
export ABCD_LEDGER=fabric     # default is 'memory'
```

`ABCD_LEDGER=fabric` **refuses to fall back**: if the network is down the run
stops, because a silent fallback would produce Exp. 4 numbers measured against
an in-memory dict under a `run_meta.json` claiming Fabric.

---

## 8. Output and validation

Every run writes three files into `Schemes/<scheme>/exp<N>_*/`:

| File | Contents |
|---|---|
| `raw_runs.csv` | one row per individual run, including `status` |
| `results.csv` | per sweep point: mean, ci95, secondaries, `n_runs`, and `measurement_type` where the scheme emits it |
| `run_meta.json` | provenance: git commit, config hashes, environment, dataset, parameters, `reportable`, `not_reportable_because` |

A `PARTIAL` marker file may appear mid-sweep. **`run_meta.json` is written only
on completion** — its absence means the directory is incomplete and not
reportable.

### Measured vs derived, across the benchmark

**This table is the single owner of that distinction.** A scheme document
restates it only where the scheme deviates.

| Scheme | Status | Evidence in `results.csv` |
|---|---|---|
| Proposed | every point measured | no `measurement_type` column |
| 30 | every point measured | no `measurement_type` column |
| 35 | every point measured | no `measurement_type` column |
| 54 | every point measured | no `measurement_type` column |
| **41** | Exp. 1 and 3 measured; **Exp. 2 measured only at `N = 10^4`**, the four larger points **derived** | `measurement_type` column: `measured` / `projected`; projected rows carry `n_runs = 1` and `ci95 = nan` |

Never describe a projected Scheme 41 point as a measurement, and never quote one
without saying it is derived. The equation, its anchors and its residual are in
[41.md](41.md).

### The reportability gate

`Schemes/ma_lb_pq_vdse/src/harness/provenance.py::reportability()` is the
single place these conditions live. A run is `reportable: false` if any holds:

- host check unsatisfied (pin configured and not matched — absence of a pin
  satisfies it vacuously and is recorded)
- `corpus_type` not in `corpus.reportable_types` (i.e. not `synthea`)
- no corpus SHA-256, or a SHA that does not match `dataset.yaml`'s frozen pin
- the bilinear group is not a faithful Type-III backend
- **Exp. 4 only:** the ledger is the in-process hash chain rather than Fabric
- index tokens use an unkeyed `H`, or no token scheme resolved
- **Exp. 7–8 only:** AASS weights are not `fixed`, or the FSNs ran in one
  interpreter instead of independent processes
- `measurement.repetitions` is not 10
- BLAS thread pinning not verified

Read `reportable` before quoting any number. A `false` run is still a valid
engineering artefact — it is simply not paper material.

### What has to be re-run, and what does not

The manuscript was rewritten and the proposed scheme rebuilt against it
(2026-09-12). **That campaign ran to completion on 2026-09-13** — the table
below is what it covered, kept as the record of what was and was not re-run.

| Exp. | Baselines | Proposed | Status |
|---|---|---|---|
| **2** | **frozen — keep** | **killed** | user's instruction 2026-09-13; the proposed scheme has no Exp. 2 |
| **3** | **frozen — keep** | done | baseline numbers are good, and are the expensive ones |
| 1 | done | done | Exp. 1 gained its second sweep dimension |
| 4 | done | done (a) reportable, (b) memory ledger | construction changed |
| 5 | done | done | construction changed |
| 6 | n/a | done | variable changed to the affected-policy ratio |
| 7, 8 | n/a | done | proposed-scheme ablations |
| ~~9~~ | — | — | folded into Exp. 4; Section VI never defined it |

**`sharding.replication` went 1 → 2 on 2026-09-13, and it is
results-affecting.** At 1 the eligible set for any shard is a singleton, so
Algorithm 1's `S ∉ S_j` guard pinned AASS to the sole holder and it could not
balance load at all — Exp. 8 then ranked the arms by how evenly they spread
work while ignoring whether the chosen node could serve the shard. Every
proposed-scheme result was re-measured at 2. Anything measured at replication
1 is not comparable with anything measured at 2; `index.yaml`'s hash is in
every `run_meta.json`.

**Exp. 2 and Exp. 3 baseline results are frozen.** Their `results.csv` files
stay exactly as they are, and a change that could move them — a baseline's
search path, index construction, workload selection or aggregation — is not
made without asking. Renaming a flag or fixing a comment cannot move a number.

**They carry an older `global.yaml`, and that is accepted.** The frozen
baselines were measured under revision `032de0d9…`; everything measured after
the 2026-09-12 rewrite carries `2bb41bb1…` — the revision that took
`repetitions` from 30 to 10, among others. So `generate_plots.py` warns that
Fig. 2 and Fig. 3 mix two revisions, and it is right to: those curves were not
all measured under one configuration.

Re-running them is the only way to clear it, and the user declined on
2026-09-13 — the existing Exp. 2 and Exp. 3 results are satisfactory as they
stand. **Do not re-run them to silence the warning.** The warning is
disclosure; `plotgen.md`'s pre-publication checklist records it as the one
permitted exception, and Exps. 1, 4, 4b and 5 were re-run at the current
revision so that every other figure stays warning-free.

Current inventory — **38 directories, 37 reportable**, `n = 10` at every
measured point. Campaign completed 2026-09-13.

| Scheme | Directories | Reportable |
|---|---|---|
| `ma_lb_pq_vdse` | exp1, exp3, exp4(+granularity), exp5, exp6×3, exp7×4, exp8×4 | 18 of 19 |
| `yue_ge` | exp1, **exp2**, **exp3**, exp4(+granularity), exp5 | all 6 |
| `guo_vdsse` | exp1, **exp2**, **exp3**, exp4(+granularity), exp5 | all 6 |
| `perera_lv_pqabse` | exp1, **exp2**, **exp3**, exp4 | all 4 |
| `thingom_pq_abse` | exp1, **exp2**, **exp3** | all 3 |

Bold is frozen. Two entries need reading carefully:

- **The proposed scheme has no Exp. 2.** Killed on the user's instruction
  2026-09-13. Its own Exp. 2 also could not reach the top two sweep points:
  at `domains: 4` the corpus yields 457,518 records, so `N = 5·10⁵` and `10⁶`
  recorded `status=failed` — by design, see *Exp. 2 refuses a point the corpus
  cannot support* in [OJCOMS.md](OJCOMS.md).
- **`exp4_..._granularity` is the one non-reportable directory**, deliberately.
  Panel (a) ran against real Hyperledger Fabric v2.5 and is reportable; panel
  (b) pins `r = 20,000`, and `tab:cost` prices verification at `O(r)T_BC`, so
  against a real peer that is ~3M chain reads and about 36 h. It was measured
  on the in-process ledger instead. The plotted quantity is
  `records_discarded`, a COUNT identical under either backend; only its
  latency secondary is understated, and `run_meta.json` records exactly that
  as the reason. Batching the anchor fetch would not change this: `O(r)` reads
  is the published cost, not an implementation defect.

Scheme 54's Exp. 1 and Exp. 4 are **no longer superseded**. Its `trapdoor()`
did not bind to the user's attribute secret key and signed with the wrong key
(Ref[54] L563-566, L798, L816); fixed 2026-09-12, both re-run 2026-09-13, and
the `SUPERSEDED` markers removed. See [54.md](54.md).

**56 `__points-` shard directories were deleted on 2026-09-12.** They held
`n = 30` measurements — the old replication count — while every parent holds
`n = 10`, which is what `global.yaml` and Section VI both now state. A shard
measured at a replication count the paper does not claim cannot be reported,
and the parent is the data. The proposed scheme's 20 development-corpus
directories went at the same time; all were `reportable: false`.

The baselines' granularity results were renamed to
`exp4_verification_overhead__granularity/` when the fold landed, so all three
schemes that measure panel (b) now write the same directory name.

## 9. Figures

All of it — the command, the experiment-to-figure map, panel ordering, how
measured and projected points are drawn differently, and the pre-publication
checklist — is in **[plotgen.md](plotgen.md)**.

```bash
python3 Plots/generate_plots.py --input Schemes --output Plots/output
```

---

## 10. Reproducibility requirements

1. **Only the frozen Synthea corpus counts.** Nothing measured against the
   development corpus is reportable.
2. **The campaign host decides comparability.** `run_meta.json` records the
   instance type actually detected. `global.yaml`'s `environment.instance_type`
   pin is currently commented out because the campaign deliberately rents more
   than one size; what was run on is recorded rather than enforced.
3. **Pin BLAS threads.** `global.yaml` sets `blas_threads: 1` and lists the five
   environment variables. numpy otherwise claims every core, which makes latency
   depend on core count across differently-sized hosts.
4. **Commit straight to `main`**, repository and fleet nodes alike. No branches.
5. **Never fabricate data.** No baseline is held to a weaker standard than the
   proposed scheme, and slowness is a finding, not something to gate on.

---

## 11. Tests

```bash
python -m pytest -q          # from the repository root
```

880 tests collect across `Common`, `Schemes` and `Dataset`. `pytest.ini` sets
`--import-mode=importlib`, which is required: every scheme ships a package
literally named `src`, and the default prepend mode would let whichever is
imported first shadow the rest and silently skip their suites.

A green suite is the floor, not the goal — every defect found so far was found
with it green.

Development hosts: Python 3.11 in a venv (`~/.venv-malbpq` by convention, which
is what `infra/provision.sh` creates on the server). Scheme 41 cannot run on
macOS or Windows — `charm-crypto` is Linux-only and there is no `petrelic`
wheel for the fallback. Everything else runs anywhere.

---
