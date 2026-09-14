# MA-LB-PQ-VDSE — working rules

Short on purpose; this loads every turn. **[skill.md](skill.md) is the manual.**
When a question is about how to run, configure, shard, validate or plot
something, the answer is there, not here. This file holds only what must be
true regardless of what you are doing.

| Need | Read |
|---|---|
| run, configure, validate, collect | `skill.md` |
| figures | `plotgen.md` |
| the proposed scheme | `OJCOMS.md` |
| a baseline | `30.md` `35.md` `41.md` `54.md` |
| a baseline's published algorithm | `References/Ref[NN]/` |

Those eight documents are the whole documentation set. There is no
`needfix.md`, no `checkexp.md`, no `SCHEME.md`, no `SystemConfiguration.md`.
**Do not recreate them.** A finding goes in the scheme's own document, in its
*departs from the paper* section, with its evidence attached; anything that
needs a decision comes to me in the reply instead of accumulating in a ledger.

## Every reply

Lead with the answer. Scannable bullets, numbers in tables, no preamble, no
restatement of the question. Spend extra lines only on a number that moved, a
thing that failed, or a decision that is mine.

Alongside any number, always state **reportable or not** (corpus type, host,
`n_runs`), **what one point on the axis is**, and **measured or derived**.

## The rerun boundary — set 2026-09-12

The manuscript was rewritten and the proposed scheme rebuilt against it. **That
campaign completed on 2026-09-13.** What it covered, per experiment:

| Exp. | Baselines | Proposed | Why |
|---|---|---|---|
| **2, 3** | **FROZEN — do not re-run, do not touch the code that produces them** | 3 done; **2 killed** | the baseline numbers are good and are the expensive ones |
| 1, 4, 5 | done | done | the construction and Exp. 1's second sweep dimension changed; Exp. 4 now has two arms |
| 6, 7, 8 | n/a | done | proposed-scheme ablations |
| ~~9~~ | — | — | **folded into Exp. 4 on 2026-09-12**; Section VI has no Experiment 9 |

**Exp. 2 and Exp. 3 baseline results are frozen.** Their `results.csv` files
stay as they are. A change that could move those numbers — in a baseline's
search path, index construction, workload selection or aggregation — needs my
say-so first. Renaming a flag or fixing a comment cannot move a number and does
not need asking.

**The proposed scheme now has reportable results for every experiment it
runs** — measured on the campaign host at `sharding.replication: 2`, n = 10,
`corpus_type: synthea`: Exps. 1, 3, 4(a), 5, 6, 7, 8. Two carve-outs:

- **Exp. 2 was killed** as a rerun on 2026-09-13, then **restored from the
  archive** the same day (`beef6b8`). The proposed scheme's series is
  reportable but **pre-rebuild**: commit `79a5739e`, 2026-09-03, at
  `sharding.replication: 1` — a different `index.yaml` from the campaign's.
  Quote it with that provenance attached; do not describe it as campaign data.
- **Exp. 4's `granularity` arm is not reportable**, deliberately — it ran on
  the in-process ledger because `tab:cost` prices verification at `O(r)T_BC`
  and at `r = 20,000` that is ~3M chain reads (~36 h) against a real peer.
  Panel (a), where latency IS the measurement, ran against Hyperledger Fabric
  v2.5 and is reportable.

`skill.md`'s inventory is the per-directory detail.

## Fixing things

**Do not ask which option to take.** Pick the one you would recommend and
execute it. Report what changed, not what you considered. The exceptions are
above and below: the frozen results, and the refusal rules.

Destructive or irreversible actions are confirmed first — terminating
instances, discarding measured data, force-pushing, deleting results.

## Hard rules

- **Five schemes, and only five.** The benchmark measures the proposed scheme
  against **Scheme 30, 35, 41 and 54**. XB-Muse (`ref36`) and Zhuang (`ref52`)
  were dropped 2026-09-12: **not measured, not implemented, never a baseline in
  any experiment.** Do not create a scheme directory or a config block for
  either. This is about MEASUREMENT only — both stay as literature citations in
  Related Work, and Zhuang keeps its row in `tab:comparison`, which is a survey
  of the field rather than a benchmark. Do not touch that table.
- **No branches.** Commit straight to `main`. `origin/OJCOMS_expByexp` exists
  and is 11 commits ahead; **leave it alone** — we work on `main`.
- **The corpus is frozen.** Only `corpus_type: synthea` is reportable. A
  development corpus is stamped `unverified_development` and the gate refuses
  it. Nothing measured on a dev host is quotable, ever.
- **One construction.** The proposed scheme implements the manuscript's
  policy-state-aware form only — `T = H(w‖PID‖PV‖Dom)`. No `--construction`
  switch, no second figure family.
- **Scheme 41's Exp. 2 is measured only at N=10⁴.** The larger points are
  fitted from three measured anchors and drawn hollow. Never launch a run above
  10⁴ — one at 10⁶ costs ~11.3 h. See `41.md`.
- **Stop an idle instance** the moment its work ends:
  `aws ec2 stop-instances --instance-ids <id>`. Idling costs ~$0.19/hr per
  `m6i.xlarge`; restarting costs ~2 minutes. Only `Project=OJCOMS` instances.
- **`Overleaf/*.tex` may be edited.** Back it up first, change only the
  sentences the decision names, show the diff.
- **No fabricated data.** No baseline held to a weaker standard than the
  proposed scheme. Never gate on speed — slowness is a finding, not a bug.
- Say **Scheme 30/35/41/54**, never author names. Ours is "the proposed scheme".

## Tests

`python -m pytest -q` from the repository root. A green suite is the floor, not
the goal — every defect found so far was found with it green.
