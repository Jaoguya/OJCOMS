# MA-LB-PQ-VDSE

Reference implementation and benchmark harness for **MA-LB-PQ-VDSE** —
*Multi-Authority Load-Balanced Post-Quantum Verifiable Dynamic Searchable
Encryption for IoMT Data Sharing* — together with four published baseline
schemes it is evaluated against.

This repository is the experimental half of the paper in
[`Overleaf/MA-LB-PQ-VDSE.tex`](Overleaf/MA-LB-PQ-VDSE.tex). The manuscript is
the specification; the code here is what was actually measured.

---

## The problem

Internet of Medical Things (IoMT) deployments generate encrypted health records
that must be searched across several administrative domains — hospital,
laboratory, emergency care — each governed by its own attribute authority.
Existing searchable-encryption schemes generally assume a centralised search
server and a static authorization model. That combination runs into three
difficulties:

- **Authorization state and index state evolve at different scopes.** When an
  authority advances its revocation state, index entries it does not govern
  should not be invalidated along with the ones it does.
- **Resource-only load balancing misprices encrypted search.** The least-loaded
  node may not hold the required shard, may hold stale authority state, or may
  face expensive proof generation — none of which CPU and queue length reveal.
- **A returned content identifier carries no proof of its own.** On its own it
  cannot show that the index entry it came from belongs to the current
  authenticated index and the current policy state.

## What this project is

**OJCOMS** is this project's name for the proposed scheme. In the code it is the
`ma_lb_pq_vdse` package; in the figures it is labelled *Proposed*.

It is a cloud–fog framework in which encrypted records are sharded across Fog
Search Nodes, each search token is bound to the policy and authority state it
was issued under, and every returned entry carries a proof tying it back to an
authenticated index. Its parts:

| Component | What it does |
|---|---|
| **VAP** — Version-Bound Authorization Profile | binds a user's attributes to the current states of the authorities that issued them |
| **Policy-state-aware index** | binds each token to only the authorities governing its policy, so unrelated updates do not invalidate it |
| **AASS** — Adaptive Authorization-Aware Search Scheduler | assigns each shard to an eligible Fog Search Node by predicted index, verification, synchronization and queue cost |
| **DIAS** — Dependency-Aware Incremental Authorization Synchronization | propagates an authority update only along its dependency closure |
| **Verifiable retrieval** | Merkle proofs and blockchain-anchored commitments bind each returned entry to its ciphertext and policy state |
| **ML-KEM-768** | post-quantum protection for attribute-key delivery |

The construction rests on a single policy-bound token, `T = H(w‖PID‖PV‖Dom)`,
which makes a token's validity scope a *policy* rather than the whole index.

Full detail: **[OJCOMS.md](OJCOMS.md)**.

## The schemes

Five schemes are implemented — the proposed one and four baselines drawn from
the literature. Baselines are referred to by their citation number in the
manuscript, never by author name, and each has one document:

| Doc | Scheme | Paper | Code |
|---|---|---|---|
| [OJCOMS.md](OJCOMS.md) | Proposed (MA-LB-PQ-VDSE) | this repository's manuscript | `Schemes/ma_lb_pq_vdse/` |
| [30.md](30.md) | Scheme 30 | Ge *et al.*, Peony / Peony++ | `Schemes/yue_ge/` |
| [35.md](35.md) | Scheme 35 | Guo *et al.*, forward-private VDSSE | `Schemes/guo_vdsse/` |
| [41.md](41.md) | Scheme 41 | Thingom *et al.*, PQ-ABSE | `Schemes/thingom_pq_abse/` |
| [54.md](54.md) | Scheme 54 | Perera and Fugkeaw, LV-PQ-ABSE | `Schemes/perera_lv_pqabse/` |

Each baseline is implemented from its published algorithms, kept in
`References/`, rather than adapted from the proposed scheme's code.

## What is measured

Eight experiments, each isolating one dimension of the design:

| # | Measures |
|---|---|
| 1 | Policy-state-aware token generation against keyword count |
| 2 | Search latency against index size |
| 3 | Cross-domain search latency against number of domains |
| 4 | Verification overhead against returned results, and what that verification buys |
| 5 | Dynamic index update against number of modified pairs |
| 6 | Synchronization cost, comparing DIAS against full and naive-incremental resynchronization |
| 7 | Search throughput against concurrency, across scheduler policies |
| 8 | Load-balancing effectiveness against concurrency, across scheduler policies |

Experiments 1–5 compare the proposed scheme against the baselines.
Experiments 6–8 are ablations of the proposed scheme's own components, so they
have no baseline; each runs the same workload with one mechanism replaced by a
simpler alternative.

Measurements are taken against a frozen [Synthea](https://synthetichealth.github.io/synthea/)
synthetic-health-record corpus on a pinned AWS instance type, so that runs are
comparable to each other across time.

### Reportability

A run always completes and always writes results, but whether those results may
be **quoted in the paper** is a separate, machine-checked question. Each run
records in `run_meta.json` a `reportable` flag and, when false, the reasons why
— wrong corpus, wrong host, wrong repetition count, a substituted backend. This
keeps exploratory runs and publication runs in the same directory structure
without the two being confused for one another.

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
Plots/                 figure generation and the rendered figures
infra/                 provisioning, fleet control, sweep sharding, Fabric
References/            transcripts of the baseline papers
```

Results live beside the code that produced them: each run writes `raw_runs.csv`,
`results.csv` and `run_meta.json` into `Schemes/<scheme>/exp<N>_*/`.

## Documentation

| Read | For |
|---|---|
| [skill.md](skill.md) | the operational guide — configuration, execution, validation, collection |
| [plotgen.md](plotgen.md) | figures |
| [OJCOMS.md](OJCOMS.md) | the proposed scheme |
| [30.md](30.md) [35.md](35.md) [41.md](41.md) [54.md](54.md) | one per baseline |

Where the code and the manuscript disagree, the scheme's own document records
the disagreement rather than resolving it silently — see *Where it still departs
from the paper* in each.
