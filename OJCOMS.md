# OJCOMS — the proposed scheme

**Reference:** MA-LB-PQ-VDSE — *Multi-Authority Load-Balanced Post-Quantum
Verifiable Dynamic Searchable Encryption for Multi-Authority IoMT Data
Sharing*. This repository's own manuscript, `Overleaf/MA-LB-PQ-VDSE.tex`.

**Code:** `Schemes/ma_lb_pq_vdse/` · **Key:** `ma_lb_pq_vdse` ·
**Label:** `Proposed`

> Which experiments it runs, what they measure, how to invoke it and where
> output lands: **[skill.md](skill.md)**. This file covers what is specific to
> the proposed scheme.

> **Implementation ported 2026-09-12 from `OJCOMS_expByexp`.** A from-scratch
> rebuild on 2026-09-11 proved too thin to publish from — it measured
> simplified operations rather than the full protocol paths. The branch's
> implementation replaced it: 78 modules, 34k lines, 21 test modules. The
> proposed scheme runs the manuscript's policy-state-aware construction.
>
> **Experiment 9 was folded into Experiment 4** in the same pass. Section VI
> defines no Experiment 9; it defines one Experiment 4 whose figure has two
> panels over two variables, and those are now two arms of Exp. 4.

---

## Role in this project

The scheme under evaluation, and the only one that runs all nine experiments.
It is the sole participant in Experiments 6, 7 and 8 because it is the only
scheme with a multi-authority synchronization protocol and a scheduler to
ablate.

---

## How the scheme works

One module per phase. Nothing is spread across two.

```
src/authority/   Phases I-II   init, AA state, C_i^auth, keygen, revocation
src/user/        Phase III     registration, ML-KEM delivery, VAP, tokens
src/psa/         Phase IV      policy-state tokens, state, commitments, verify
src/index/       Phase IV      DSI, token derivation, extraction, commitments
src/chain/       Phase V       ledger (in-process / Fabric v2.5), IPFS, outsourcing
src/aim/         Phase VI.1-2  authorization verification, token derivation
src/scheduler/   Phase VI.3    AASS (Algorithm 1) + three ablation arms
src/fsn/         Phase VI.4-5  Fog Search Nodes, forked pool, search
src/shard/       Phase VII     shard propagation
src/sync/        Phase VII     DIAS
src/verify/      Phase VIII    Merkle proofs, ledger checks
src/harness/     experiments.py, psa_experiments.py, runner, provenance, stats
src/tests/       21 test modules
```

### The token is the construction

```
V_{P_i} = {(ID_k, v_k) : AA_k in AA(PID_i)}      the policy-relevant version VECTOR
PV_i    = H(Encode(V_{P_i}))                     its digest
T_{i,j} = H(w_{i,j} || PID_i || PV_i || Dom_i)   eq:policy-bound-token
I_{i,j} = (T_{i,j}, CID_i, PID_i, PV_i)          eq:index-entry
```

Policy, policy-state digest and domain are **inside** the hash. Two consequences
carry the whole design, and both are pinned by tests:

- A token names a domain, so a trapdoor is **not** domain-independent. One
  keyword under `|P_U|` authorized policies yields `|P_U|` distinct tokens —
  which is why Experiment 1 sweeps `q` *and* `|P_U|` and reports
  `|T_Q| = q·|P_U|`.
- An authority update **inside** `AA(PID_i)` changes `PV_i`, hence every token
  of every record under that policy. An update **outside** it leaves
  `V_{P_i}` untouched, so `PV_i`, every token and every index entry are
  bit-identical and no delta exists to send. That is Policy-State
  Non-Interference, and `retokenize()` returns 0 recomputed nodes for it.

`derive_token()` is **one function** serving both Phase IV Step 2 and Phase VI
Step 2. The Token Consistency theorem is the claim that those two computations
coincide; two implementations would leave it resting on the bodies staying in
step, which is the exact failure the previous construction died of.

### AASS

For each required shard, nodes that do not hold it are skipped; among the rest
the minimum predicted cost wins:

```
SC_j = L1·C_index + L2·C_verify + L3·C_sync + L4·C_queue
C_index = |Cand|   C_verify = |R|·log N_j   C_sync = lagging authority count   C_queue = queue delay
```

**The eligibility guard sits inside the per-shard loop**, which is why AASS's
cross-node forward count is 0 by construction and the three oblivious arms'
is not. Implementing it anywhere else makes Experiment 8 panel (c) invariant
across all four arms.

### DIAS

The three arms differ in exactly two independent decisions, and keeping them
independent is what makes the ablation say anything:

| arm | recomputes | tells |
|---|---|---|
| `full_state` | all policies | all FSNs |
| `incremental_all` | affected policies | all FSNs |
| `dias` | affected policies | affected FSNs only |

`full_state → incremental_all` isolates dependency-localized state maintenance;
`incremental_all → dias` isolates selective propagation.

---

## Configuration

### From `global.yaml`

| Key | Value | Provenance |
|---|---|---|
| `topology.fog_search_nodes` | 4 | published |
| `topology.cloud_servers` | 1 | published |
| `topology.independent_processes` | true | published |
| `authorities.count` | 4 | benchmark — one AA per domain |
| `authorities.attributes_per_authority` | 10 | benchmark, not in the paper |
| `authorities.disjoint_attribute_universes` | true | published (Phase II Step 2) |
| `exp7/8.ramp_seconds` | 30 | published, excluded from every timing |

### From `scheduler.yaml`

`status: fixed`, from the documented hold-out sweep. Fixed across Experiments 7
and 8 — per-experiment retuning would let the scheduler be tuned to the metric
it is scored on.

| Weight | Term | Value |
|---|---|---|
| `lambda_1_index` | index traversal | 0.5 |
| `lambda_2_verify` | proof generation | 0.125 |
| `lambda_3_sync` | policy-state synchronization | 0.25 |
| `lambda_4_queue` | queue delay | 0.125 |

### From `crypto.yaml`

Type-3 pairing on BN254 (`charm_type3`, fallback MNT224), SHA-256,
AES-256-GCM, HKDF-SHA256, ML-KEM-768, 256-bit hash output, length-prefixed
concatenation.

### Benchmark decisions the paper does not make

| Decision | Value | Why it is needed |
|---|---|---|
| `governing_set_size` | 2 of 4 | see below |
| `POLICIES_PER_DOMAIN` | 4 | the corpus carries no access policy |
| `replication` | 2 | at 1 every scheduler is forced to the same node |

**`AA(PID)` had to be modelled.** Policy ids are `<domain>/polN` and global.yaml
maps one authority to one domain, so read literally the governing set is a
**singleton** — and then both halves of the manuscript's central claim are
vacuous: non-interference has nothing outside the set, token consistency has no
vector. `governance.py` therefore makes the set size a benchmark parameter
defaulting to 2-of-4, so every policy has at least one authority inside and one
outside. `test_governing_set_is_a_proper_nonempty_subset` enforces exactly that.

### Ledger backend

`ABCD_LEDGER=memory` (default) or `fabric`. `fabric` **refuses to fall back** —
a silent fallback would produce Exp. 4 numbers measured against a dict under a
`run_meta.json` claiming Fabric.

---

## Scheme-specific flags

| Flag | Effect |
|---|---|
| `--variant` | Exp. 1: `pu1/pu2/pu4/pu8` (authorization scope). Exp. 6: `dias/incremental_all/full_state`. Exp. 7–8: `no_lb/round_robin/least_loaded/aass`. Or `all`. |
| `--smoke` | one sweep point, few runs |
| `--require-reportable` | refuse to start unless every condition holds |
| `--quiet` | |

Output flag is `--output`; warm-ups `--warmups`. `--dataset` accepted. The three
variant vocabularies are **not interchangeable** — a scheduler decides which FSN
serves a query and plays no part in how an authorization change propagates.

---

## Measurement notes

- **Every point is measured**, and every timed region is bracketed with
  `gc_quiesced()` so a cyclic collection cannot land inside a measurement.
- **Fixture preparation is outside the stopwatch.** Experiments 4 and 9 build
  and tamper their bundles before timing — forging a response is the
  adversary's work, not the verifier's.
- **MAABE and ML-KEM are on no measured path**, recorded in `run_meta.json` as
  `maabe_on_measured_path: false` and `mlkem_on_measured_path: false`. Phase V
  runs in untimed setup and key delivery happens once per enrolment, not per
  query — the same boundary Scheme 54 declares for its lattice CP-ABE. No
  reported number contains a pairing.
- **Exp. 2 refuses a point the corpus cannot support.** `records[:10**6]` on a
  10⁴-record corpus silently returns 10⁴ records; reporting that at `x = 10⁶`
  would put a flat curve in the figure and call it a 100× sweep. The point is
  recorded `status=failed` instead.
- **Exp. 3 sweeps the authority and node count with `d`.** `authority_to_domain`
  is one-to-one, so pinning authorities at 4 while `d` runs to 10 leaves the
  upper domains with no authority and no satisfiable policy — which showed up
  as token counts saturating at `d = 4`.
- **Exp. 7–8 model arrival then service.** Draining each request before
  scheduling the next leaves every queue at depth ≤ 1, makes `max_queue_depth`
  identically 0, and hands `least_loaded` an always-empty queue to minimise.

---

## Where it still departs from the paper

D1–D9 are closed by the rebuild. These are open, and the code is the source of
truth for all of them — none was resolved by tuning the implementation toward
the prose.

- **Exp. 7 and Exp. 8: AASS does not top either panel. CLOSED on the campaign
  host, 2026-09-13**, n = 10, `synthea`, `m6i.xlarge`, `sharding.replication: 2`.

  Both previous entries here were measured under conditions that could not
  test the claim, and both are now superseded:

  * The ablation was measuring **one scheduler four times**.
    `psa_experiments.build()` dropped its `variant` argument for Exp. 7-8, so
    every arm was built at the default `aass` and the four results were written
    to four differently-named directories. That produced four throughput curves
    agreeing inside their CIs and `cross_node_forwards = 0` everywhere — AASS's
    signature, reported as though the oblivious arms shared it.
  * `sharding.replication` was **1**, so every shard had exactly one eligible
    node and Algorithm 1's `S ∉ S_j` guard pinned AASS to it. AASS had no
    scheduling freedom at all, while the oblivious arms "won" by scattering
    work onto nodes that could not serve the shard.

  With the variant carried through and replication at 2:

  | concurrency | no_lb | round_robin | least_loaded | **aass** |
  |---|---|---|---|---|
  | 100 | 460.3 ± 10.6 | 665.3 ± 17.5 | **746.7 ± 56.9** | 619.7 ± 46.4 |
  | 1000 | 542.6 ± 17.9 | 929.2 ± 11.0 | **1029.7 ± 14.8** | 982.1 ± 26.0 |

  throughput (q/s); and utilization std. dev. with cross-node forwards:

  | concurrency | no_lb | round_robin | least_loaded | **aass** |
  |---|---|---|---|---|
  | 1000 | 0.3924 ± 0.0017 | 0.1215 ± 0.0053 | **0.0193 ± 0.0054** | 0.0903 ± 0.0177 |
  | forwards @1000 | 2,114 | 2,173 | 2,149 | **0** |

  **§VI's Exp. 8 claim is confirmed and this document's was wrong.** The text
  says least-loaded "attains the lowest variation of the four" — it does. The
  previous entry above asserted "Measured here, AASS does"; that was the
  replication-1 artefact.

  **§VI's Exp. 7 claim is not supported as written.** AASS does not sustain the
  highest throughput; least-loaded does, at every concurrency. AASS is second,
  and the gap is outside the CIs.

  What AASS *does* hold uniquely is **zero cross-node forwards at every point**,
  where the other three pay ~2,100 at concurrency 1000. It also improves with
  load (0.190 → 0.090 utilization spread) and beats no-load-balancing 4.3x. The
  defensible claim is therefore a **trade — perfect authorization locality at
  some cost in balance — and not dominance on either axis.** §VI currently
  claims dominance on both; that wording needs the user's decision.
- **Exp. 2's selectivity is not constant.** §VI claims "query selectivity is
  kept constant… matching records increase proportionally". The query draws `q`
  keywords from one record, so selectivity tracks that record's keyword
  frequencies rather than a fixed band.
- **Exp. 3's sizing is the opposite of the baselines'.** §VI fixes the
  per-domain index; all four baselines fix the *total*, so their per-domain
  shrinks as `d` grows. Each curve's shape is partly an artefact of its own
  sizing rule.
- **Search returns a union, not an intersection.** `eq:search-results` defines
  `R` as every entry whose token is in `T_Q` — a disjunction at the index layer.
  §VI elsewhere calls the workload a "conjunctive query". The implementation
  follows the equation.
- **Manuscript-side, unchanged by the rebuild:** the Conclusion still lists
  "PDSIs" and "IAS" while the body defines DIAS throughout; `ref34`, `ref44`,
  `ref49`, `ref56` are never cited; `tab:notation` defines `VID_i`, which the
  protocol no longer uses, `Score_j`, where the scheduler's symbol is `SC_j`,
  and `ST` and `Root_t`, which appear nowhere in the body — it uses `T_Q` and
  `Root_i`/`Root_i'`/`Root_U`, the last of which `tab:notation` never defines.

  **Corrected 2026-09-13.** This bullet used to claim `tab:cost` uses
  `T_Mul`, `T_Sig`, `T_BF`, `N` and `n_cand`, "none defined in
  `tab:cost-notation`". Verified against the manuscript: all four of
  `T_Mul`, `T_Sig`, `T_BF` and `N` *are* defined there, and `n_cand` does not
  occur anywhere in the `.tex` at all. A stale finding in a findings list is
  worse than no finding, because it costs the reader trust in the rest.

---

## Implementation notes

- **The Merkle tree is per record**, over that record's `t` keyword entries.
  `MerkleTree` applies `hash_leaf` itself, so `RecordIndex.leaf()` returns
  `Encode(I_{i,j})` and `L_{i,j} = H(Encode(I_{i,j}))` is computed once. Phase
  VIII verifies with `verify_leaf(entry.encode(), …)`; the two must agree on
  where `H` is applied or every proof fails.
- **`retokenize()` updates leaves in place**, not by rebuilding — that is
  `MerkleUpdate(Root_i, ΔL_i)` and the difference between `O(k log t)` and
  `O(k·t)`. Measured: ~4.07 nodes recomputed per entry rewritten, ≈ log₂ t.
- **The AIM never holds a secret key.** `build_vap` takes attributes, versions
  and commitments — never key material — and a test asserts no `MSK` appears in
  the profile.
- **`run_meta.json` carries the arm on every run**, including defaults, so a
  directory stays identifiable after a rename or merge.
- **Experiment 4 has two arms.** The default sweeps `r` and measures what
  verification costs; `granularity` pins `returned_results: 20000` and sweeps
  the tamper count `t`, measuring what it buys. `--experiment 4` runs both,
  because a figure missing a panel is not the figure Section VI describes. The
  arm asserts soundness AND completeness per point — rejecting fewer records
  than were tampered means a tamper went undetected, rejecting more means an
  intact record was discarded, and either raises rather than being averaged in.
- Test coverage is the branch's 21 modules, including `test_psa_construction`,
  `test_psa_verify`, `test_phase1_2` through `test_phase8`, and
  `test_cost_table_agreement`.
