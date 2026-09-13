"""Phase VI Step 3 — Adaptive Authorization-Aware Search Scheduling.

Manuscript `Overleaf/MA-LB-PQ-VDSE.tex`:

    SC_j = L1*C_j^index + L2*C_j^verify + L3*C_j^sync + L4*C_j^queue

    C_j^index  = |Cand_Q^(j)|           estimated candidate set size
    C_j^verify = |R_Q^(j)| * log N_j    predicted matches x log entry count
    C_j^sync   = |{(ID_k,v_k) in V_Q : v_j,k < v_k}|
                                        query-relevant authorities the node lags
    C_j^queue  = T_j^queue              queue waiting time

    FOUR terms, per ``eq:search-cost``. A fifth, ``C_j^auth = |P_Q|``, belonged
    to the previous manuscript revision and was removed here on 2026-09-07; see
    :class:`CostVector`.

    M_Q[S] = arg min_{j : S in S_j} SC_j                       (Alg. 1)

ALGORITHM 1 IS PER SHARD, NOT PER REQUEST
-----------------------------------------
``alg:aass`` loops over the required shard set ``S_Q``, restricts each shard to
the FSNs that *maintain* it (``S notin S_j -> continue``), and returns a
shard-to-FSN map ``M_Q``. :meth:`Scheduler.assign` is that loop. A shard here is
a ``(domain, policy)`` pair -- the key ``DynamicSearchIndex`` already keeps its
authorization bitmaps under, so ``S_j`` is ``node.index.policy_pairs`` and
``S_Q`` is the authorized pair set the AIM returns from Phase VI Step 2.

:meth:`Scheduler.select` remains, and returns the node carrying the largest
share of ``M_Q``. It is a *reduction* of the assignment for callers that must
name one node (the Exp. 7-8 replay dispatches one request to one process); it is
not the published rule, and it never decides anything ``assign`` did not.

``C_j^sync`` IS A LAG COUNT
---------------------------
``|{(ID_k,v_k) in V_Q : v_{j,k} < v_k}|`` -- how many of the query-relevant
authorities this node is BEHIND on, read from the per-authority ``Meta_i`` the
node holds (``FogSearchNode.vid_for_authority``). It is not ``|VID_U - VID_j|``:
that scalar was the previous revision's form, it cannot distinguish "one
authority two versions stale" from "two authorities one version stale", and
Phase VII Step 5's staleness gate is stated over the per-authority vector.

A request that carries no ``V_Q`` cannot be costed on this term at all, so
:func:`sync_lag` returns 0 rather than inventing a scalar -- and
:meth:`Scheduler.assign` refuses such a request when the variant is ``aass``,
because silently scoring three of four published terms is how a scheduler comes
to be reported as AASS while implementing something else.

"Unlike conventional load balancing algorithms that rely solely on processor
utilization or memory consumption, AASS **predicts** the expected cryptographic
workload **before** executing encrypted search."

That word *predicts* sets the cost ceiling: every term is estimated from
statistics the node already keeps, never by evaluating the query. A scheduler
that searched in order to decide where to search would cost as much as the search
it was scheduling, and Exp. 7's throughput would measure the scheduler.
:func:`estimate_costs` therefore does O(q) dict lookups and one bitmap popcount.

**Normalization is ours, and it is load-bearing** (``scheduler.yaml →
normalization``). The four published estimators have incommensurable units and
magnitudes: at the §6 defaults ``|Cand_Q^(j)|`` is O(10^4), ``|R|*log N`` is
O(10^3), the ``V_Q`` lag count is O(1) (bounded by ``|V_Q|``, i.e. ``N_AA``),
and ``T_j^queue`` is a time in nanoseconds. Applying raw weights would let ``C_index`` dominate by orders of
magnitude regardless of the lambdas, making the weight vector — and with it the
AASS claim — vacuous. Each term is mapped to [0,1] by dividing by the maximum
across the candidate nodes, which is scale-free and needs no calibration
constants.

A term equal across all nodes is mapped to **0**, not 1: it cannot affect
``arg min``, so giving it a value would silently consume weight that belongs to
the terms that do differ.

**The weights are fixed, but they were never swept in this form.**
``scheduler.yaml`` carries ``weights.status: fixed``, so
``config.scheduler.require_fixed()`` returns them and the ``reportable=True``
gate does NOT raise -- this paragraph previously said it did, which stopped
being true when the status was set. What IS true is narrower and worth stating
plainly: the 2026-08-28 hold-out sweep chose a FIVE-weight vector
``(0.2, 0.4, 0.1, 0.2, 0.1)`` over a cost function that included ``C^auth``.
When ``C^auth`` was removed on 2026-09-07 the surviving four were RENORMALISED
to ``(0.5, 0.125, 0.25, 0.125)`` rather than re-swept, so no sweep has ever
produced the vector in use. ``determined_on`` and ``determined_by`` are null in
``scheduler.yaml`` for the same reason.

Renormalising preserves the ratio the sweep chose among the four surviving
terms, which is the most defensible thing to do without re-running it -- but it
is an inference from a sweep over a different objective, not a result of one.
Re-running the documented hold-out sweep over the four-term cost is what would
make ``status: fixed`` mean what it says.
"""

from __future__ import annotations

import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from .. import config as scheme_config  # noqa: E402
from ..fsn.fsn import FogSearchNode  # noqa: E402

#: The four ablation variants of ``skill.md``.
VARIANT_NO_LB = "no_lb"
VARIANT_ROUND_ROBIN = "round_robin"
VARIANT_LEAST_LOADED = "least_loaded"
VARIANT_AASS = "aass"

VARIANTS: Tuple[str, ...] = (
    VARIANT_NO_LB,
    VARIANT_ROUND_ROBIN,
    VARIANT_LEAST_LOADED,
    VARIANT_AASS,
)

#: Variants that read authorization state. Only ``aass`` does — which is why the
#: VID_j aggregation and the normalization rule affect that variant alone.
AUTHORIZATION_AWARE: Tuple[str, ...] = (VARIANT_AASS,)


class SchedulerError(RuntimeError):
    """Raised when a search request cannot be scheduled."""


@dataclass(frozen=True)
class SearchRequest:
    """What the scheduler needs to know about a query, before running it.

    ``authorized`` is ``S_Q``: the ``(domain, policy)`` shard set the AIM
    resolved in Phase VI Step 2 — scheduling happens after authorization, never
    before, so an unauthorized request is rejected without any node being costed.

    ``query_versions`` is ``V_Q``, the query-relevant authority state
    ``{(ID_k, v_k)}`` over the authorities governing the policies of ``S_Q``.
    It is what ``C_j^sync`` counts a node's lag against. Empty is allowed at the
    type level so the three authorization-oblivious variants — which never read
    the term — need not manufacture one; ``aass`` refuses it.

    ``vid_u`` is the scalar profile version, retained because the Option D
    profile and the Exp. 7-8 replay both carry it. It is NOT an input to any
    published cost term any more.
    """

    tokens: Tuple[bytes, ...]
    authorized: Tuple[Tuple[str, str], ...]
    vid_u: int = 0
    query_versions: Tuple[Tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        if not self.tokens:
            raise SchedulerError("a search request must carry at least one token")
        if not self.authorized:
            raise SchedulerError(
                "a search request must carry at least one authorized "
                "(domain, policy) pair; Phase VI Step 2 rejects an unauthorized "
                "request without traversing the index"
            )
        if self.vid_u < 0:
            raise ValueError(f"VID_U must be non-negative, got {self.vid_u}")
        ids = [authority for authority, _ in self.query_versions]
        if len(set(ids)) != len(ids):
            raise SchedulerError(
                f"duplicate authority in V_Q: {sorted(ids)}. V_Q is a state per "
                f"authority, so a repeated id would let one authority be counted "
                f"twice in C_j^sync"
            )
        for authority, version in self.query_versions:
            if version < 0:
                raise ValueError(
                    f"V_Q version for {authority!r} must be non-negative, "
                    f"got {version}"
                )

    @property
    def shards(self) -> Tuple[Tuple[str, str], ...]:
        """``S_Q`` — the required shard set Algorithm 1 iterates over."""
        return self.authorized

    @property
    def domains(self) -> Tuple[str, ...]:
        return tuple(sorted({domain for domain, _ in self.authorized}))


@dataclass(frozen=True)
class CostVector:
    """The four published terms for one node, before weighting.

    ``eq:search-cost`` of the manuscript reads

        SC_j = lambda_1 C_j^index + lambda_2 C_j^verify
             + lambda_3 C_j^sync  + lambda_4 C_j^queue

    -- FOUR terms. A fifth, ``C_j^auth = |P_Q|``, was carried here and in
    ``scheduler.yaml`` (labelled "published") from the previous manuscript
    revision, which did include it. The current revision dropped it, so the
    code carried a rule the paper does not state and the lambda indices did not
    line up: a reader reproducing from eq:search-cost would have built a
    different scheduler. Aligned to the paper on 2026-09-07.

    **This is a real reduction in what AASS considers.** ``C^auth`` was the
    authorization-awareness term; without it the rule weighs index size,
    verification depth, synchronization staleness and queue depth, and the
    authorization structure enters only through ``_candidates``, which still
    restricts scheduling to nodes serving an authorized domain.
    """

    index: float
    verify: float
    sync: float
    queue: float

    def as_tuple(self) -> Tuple[float, float, float, float]:
        return (self.index, self.verify, self.sync, self.queue)

    def score(self, weights: scheme_config.SchedulerWeights) -> float:
        """``SC_j`` for this (normalized) vector."""
        w = weights.as_tuple()
        return sum(term * weight for term, weight in zip(self.as_tuple(), w))


@dataclass(frozen=True)
class NodeCost:
    """One node's raw and normalized costs, and its resulting score."""

    node_id: str
    raw: CostVector
    normalized: CostVector
    score: float


@dataclass(frozen=True)
class Selection:
    """The scheduler's decision, with the evidence behind it.

    ``costs`` is retained so Exp. 7-8 can report *why* a node was chosen, not
    only which. A scheduler that cannot show its reasoning cannot be audited
    against the claim that it "predicts the expected cryptographic workload".

    **Empty for the three authorization-oblivious variants.** ``no_lb``,
    ``round_robin`` and ``least_loaded`` decide from a cursor or a queue depth
    and never consult ``SC_j``; costing them anyway would both charge a baseline
    for work its published construction does not do and imply evidence it never
    read. ``aass`` alone carries a vector per candidate.
    """

    node: FogSearchNode
    variant: str
    costs: Tuple[NodeCost, ...]
    #: The full ``M_Q`` this selection reduces. Empty only for a caller that
    #: built a ``Selection`` directly rather than through :meth:`Scheduler.select`.
    assignment: Optional["ShardAssignment"] = None

    @property
    def node_id(self) -> str:
        return self.node.node_id

    def cost_for(self, node_id: str) -> NodeCost:
        for cost in self.costs:
            if cost.node_id == node_id:
                return cost
        raise KeyError(node_id)


@dataclass(frozen=True)
class ShardAssignment:
    """``M_Q`` — what Algorithm 1 returns.

    ``mapping`` is ``S -> FSN_j`` for every required shard the variant could
    place. ``forwards`` counts the shards a variant assigned to a node that does
    **not** maintain them: §VI's Exp. 8(c) defines a cross-node forward as exactly
    that, "a scheduler assigns a required shard to an FSN that does not maintain
    it, requiring redirection to an eligible node". Under ``aass`` it is 0 by
    construction — the ``S notin S_j -> continue`` guard of Algorithm 1 makes it
    so — and under the three oblivious variants it is whatever their rule
    produces. A metric that is 0 for one arm and positive for the others is the
    locality claim; measuring it needs the per-shard loop, which is why it could
    not be measured while the scheduler returned one node per request.

    ``unplaceable`` names shards no node in the pool maintains. Those are a
    deployment fault, not a scheduling decision, and they are excluded from
    ``forwards`` so a gap in the shard map cannot be read as a scheduler defect.
    """

    mapping: Tuple[Tuple[Tuple[str, str], FogSearchNode], ...]
    variant: str
    costs: Tuple[NodeCost, ...]
    forwards: int
    unplaceable: Tuple[Tuple[str, str], ...]

    @property
    def shards(self) -> Tuple[Tuple[str, str], ...]:
        return tuple(shard for shard, _ in self.mapping)

    @property
    def nodes(self) -> Tuple[FogSearchNode, ...]:
        """The distinct FSNs this query touches, in first-assigned order."""
        seen: List[str] = []
        out: List[FogSearchNode] = []
        for _shard, node in self.mapping:
            if node.node_id not in seen:
                seen.append(node.node_id)
                out.append(node)
        return tuple(out)

    def shards_for(self, node_id: str) -> Tuple[Tuple[str, str], ...]:
        return tuple(s for s, n in self.mapping if n.node_id == node_id)

    def busiest(self) -> Optional[FogSearchNode]:
        """The node carrying the most shards; FIRST-ASSIGNED breaks ties.

        The reduction :meth:`Scheduler.select` performs. Deterministic, so a
        replayed trace makes the same dispatch.

        The tie-break is assignment order, not ``node_id``. Under ``round_robin``
        with ``|S_Q|`` a multiple of the pool size every node carries an equal
        share, and a ``node_id`` tie-break would then return the alphabetically
        first node for *every* request — silently collapsing round-robin into
        no-load-balancing and erasing the arm Exp. 7-8 compare against. Assignment
        order rotates with the cursor, which is the rotation itself.
        """
        if not self.mapping:
            return None
        counts: Dict[str, int] = {}
        for _shard, node in self.mapping:
            counts[node.node_id] = counts.get(node.node_id, 0) + 1
        best_node: Optional[FogSearchNode] = None
        best_count = -1
        for _shard, node in self.mapping:
            count = counts[node.node_id]
            if count > best_count:
                best_count = count
                best_node = node
        return best_node


def estimate_result_count(node: FogSearchNode, request: SearchRequest) -> int:
    """``|R_Q^(j)|`` — predicted matching ciphertexts, from statistics only.

    For a conjunctive query the match count cannot exceed the shortest posting
    list, so that length is the estimator: it is an upper bound, it costs one dict
    lookup per token, and it never evaluates the query.

    Using the true count would mean running the search to decide where to run it.
    """
    lengths = [
        len(node.index._postings.get(token, ())) for token in request.tokens
    ]
    return min(lengths) if lengths else 0


def estimate_candidate_count(
    node: FogSearchNode, request: SearchRequest
) -> int:
    """``|Cand_Q^(j)|`` — authorized candidates, from the shard's bitmaps.

    Exact rather than estimated, because the bitmap union already answers it in
    one popcount over the shard — cheaper than any approximation would be.

    PER NODE, NOT PER SHARD, and the distinction was got wrong once. All four
    estimators of ``eq:search-cost`` carry the superscript ``(j)`` and nothing
    else: they are functions of the node and ``T_Q``. Algorithm 1's
    ``EstimateSearchCost(FSN_j, T_Q, S)`` does pass ``S``, but ``S`` is what
    decides ELIGIBILITY (``S notin S_j -> continue``); it does not appear in any
    of the four term definitions. An earlier revision here scoped this one term
    to the shard and left the other three per node, which is neither reading.

    The union is over ``S_Q``, not over everything the node holds, so a node is
    already charged only for pairs the query is authorized for.
    """
    return node.index.authorized_bitmap(request.authorized).count(1)


def sync_lag(node: FogSearchNode, request: SearchRequest) -> int:
    """``C_j^sync = |{(ID_k, v_k) ∈ V_Q : v_{j,k} < v_k}|``.

    The count of query-relevant authorities this node has NOT caught up on. An
    authority in ``V_Q`` that the node holds no ``Meta_i`` for counts as lagging:
    an unsynchronised node cannot serve that authority's state at all, and
    treating "never synchronised" as "version 0, therefore only behind if
    ``v_k > 0``" would make a node that received nothing look fresher than one
    that received an older delta.

    Returns 0 for an empty ``V_Q``. That is not a fresh node — it is a request
    that did not state which authorities its query depends on, and
    :meth:`Scheduler.assign` rejects that case for ``aass`` rather than letting
    the term silently vanish.
    """
    lagging = 0
    for authority, version in request.query_versions:
        try:
            held = node.vid_for_authority(authority)
        except Exception:
            lagging += 1
            continue
        if held < version:
            lagging += 1
    return lagging


def estimate_costs(node: FogSearchNode, request: SearchRequest) -> CostVector:
    """The four raw terms of ``eq:search-cost`` for one node.

    Independent of which shard is being assigned — see
    :func:`estimate_candidate_count`. :meth:`Scheduler.assign` therefore
    evaluates this ONCE per node per request and reuses it across the shard
    loop, which is both what the equations say and what keeps the scheduler
    from costing the same node ``|S_Q|`` times inside a timed region.
    """
    entries = node.entry_count
    # log N_j: the base is a constant factor that normalization and lambda_2
    # absorb, so log2 is chosen for being the natural unit of a binary tree
    # (which is what the verification cost actually walks).
    log_entries = math.log2(entries) if entries > 1 else 0.0
    return CostVector(
        index=float(estimate_candidate_count(node, request)),
        verify=float(estimate_result_count(node, request)) * log_entries,
        sync=float(sync_lag(node, request)),
        queue=float(node.queue_wait_ns()),
    )


def normalize(
    vectors: Sequence[CostVector],
    *,
    degenerate_value: float = 0.0,
    epsilon: float = 1e-9,
) -> Tuple[CostVector, ...]:
    """Map each term to [0,1] by its maximum across the candidate nodes.

    A term that is identical on every node is mapped to ``degenerate_value``
    (0.0): it cannot change ``arg min``, so scoring it would consume weight
    without informing the decision.
    """
    if not vectors:
        return ()
    columns = list(zip(*(vector.as_tuple() for vector in vectors)))
    scales: List[Optional[float]] = []
    for column in columns:
        high, low = max(column), min(column)
        # Degenerate when every node agrees — including when all are zero.
        scales.append(None if abs(high - low) <= epsilon else high)
    normalized: List[CostVector] = []
    for vector in vectors:
        terms = []
        for value, scale in zip(vector.as_tuple(), scales):
            if scale is None:
                terms.append(degenerate_value)
            else:
                # No epsilon in the divisor. Every cost term is non-negative, so a
                # non-degenerate column has high > low >= 0 and therefore
                # high > 0: the guard is unnecessary, and adding it would put the
                # maximum at 0.999... instead of exactly 1.0. `epsilon` has one
                # job here — deciding degeneracy — not two.
                terms.append(value / scale)
        normalized.append(CostVector(*terms))
    return tuple(normalized)


class Scheduler:
    """The Phase VI Step 3 scheduler, in one of the four ablation variants.

    ``no_lb``, ``round_robin`` and ``least_loaded`` are authorization-oblivious
    and never read ``C_j^sync`` — which is why the ``VID_j`` aggregation and the
    normalization rule affect only ``aass``, and why Exp. 7-8 is an ablation of
    the scheduling rule rather than of the whole scheme.
    """

    def __init__(
        self,
        variant: str = VARIANT_AASS,
        *,
        config: Optional[scheme_config.Configuration] = None,
        reportable: bool = False,
    ) -> None:
        if variant not in VARIANTS:
            raise SchedulerError(
                f"unknown variant {variant!r}; expected one of {list(VARIANTS)}"
            )
        self.variant = variant
        self.config = config or scheme_config.load()
        self.reportable = reportable
        self._cursor = 0
        #: Requests seen, not shards placed. `round_robin` needs BOTH: the
        #: cursor rotates shards across the pool WITHIN one request, and this
        #: rotates where that rotation STARTS between requests. With only the
        #: cursor, `|S_Q|` a multiple of the pool size returns it to the same
        #: offset every request, so the first shard went to the same node every
        #: time -- and `select()`, which reduces M_Q by first-assigned, then
        #: returned that one node for every request. See `assign`.
        self._requests = 0

        if variant == VARIANT_AASS and reportable:
            # Live gate: raises while scheduler.yaml says pending_sweep, so
            # Exp. 7-8 cannot report the provisional uniform vector as AASS.
            self._weights = self.config.scheduler.require_fixed(
                context="Phase VI AASS scheduling (Exp. 7-8)"
            )
        else:
            self._weights = self.config.scheduler.weights

    @property
    def weights(self) -> scheme_config.SchedulerWeights:
        return self._weights

    @property
    def is_authorization_aware(self) -> bool:
        return self.variant in AUTHORIZATION_AWARE

    @staticmethod
    def eligible(
        nodes: Sequence[FogSearchNode], shard: Tuple[str, str]
    ) -> Tuple[FogSearchNode, ...]:
        """``{FSN_j : S ∈ S_j}`` — Algorithm 1's ``if S ∉ S_j: continue``.

        Membership is by the node's own bitmap keys, not by domain: a node can
        serve a domain and still hold no entries under a particular policy of
        it, and Algorithm 1 asks whether the node maintains the SHARD.
        """
        return tuple(node for node in nodes if shard in node.index.policy_pairs)

    def _reachable(
        self, nodes: Sequence[FogSearchNode], request: SearchRequest
    ) -> Tuple[FogSearchNode, ...]:
        """Nodes serving at least one domain the request is authorized for.

        The pool the three oblivious variants pick from. They do not consult
        ``S_j``, which is exactly why they can produce a cross-node forward;
        restricting them to a domain keeps them from being weakened below their
        published construction, which routes within the federation the query
        addresses.
        """
        wanted = set(request.domains)
        reachable = tuple(node for node in nodes if wanted & set(node.domains))
        if not reachable:
            raise SchedulerError(
                f"no Fog Search Node serves any of the authorized domains "
                f"{sorted(wanted)}"
            )
        return reachable

    def assign(
        self, nodes: Sequence[FogSearchNode], request: SearchRequest
    ) -> ShardAssignment:
        """Algorithm 1 — ``M_Q``, one FSN per required shard.

        ``aass`` follows the published loop exactly: eligible nodes only, then
        ``arg min SC_j`` among them. The three oblivious variants place each
        shard by their own rule over the reachable pool, without the eligibility
        guard — which is what makes their cross-node forwards non-zero and the
        Exp. 8(c) comparison meaningful.
        """
        if not nodes:
            raise SchedulerError("no Fog Search Nodes to schedule across")
        if self.variant == VARIANT_AASS and not request.query_versions:
            raise SchedulerError(
                "AASS requires V_Q (the query-relevant authority state) to "
                "evaluate C_j^sync; a request without it can only be scored on "
                "three of eq:search-cost's four terms, which is not AASS"
            )
        reachable = self._reachable(nodes, request)
        request_offset = self._requests
        self._requests += 1

        mapping: List[Tuple[Tuple[str, str], FogSearchNode]] = []
        unplaceable: List[Tuple[str, str]] = []
        forwards = 0
        all_costs: List[NodeCost] = []
        seen_costs: set = set()
        # Raw vectors are a function of (node, T_Q) alone, so one per node for
        # the whole request. Normalization still happens per shard, over that
        # shard's eligible set -- it is scale-free and depends on which nodes
        # are being compared, which is exactly what changes shard to shard.
        raw_cache: Dict[str, CostVector] = {}

        for shard in request.shards:
            holders = self.eligible(nodes, shard)
            if self.variant == VARIANT_AASS:
                if not holders:
                    # `S ∉ S_j` for every j: no eligible node exists. Algorithm 1
                    # writes `M_Q[S] <- ⊥` here; the entry is recorded in
                    # `unplaceable` instead of stored as a null, so callers
                    # iterating `mapping` cannot dispatch to ⊥ by accident.
                    unplaceable.append(shard)
                    continue
                costs = self.score(holders, request, cache=raw_cache)
                chosen = min(
                    zip(holders, costs),
                    key=lambda pair: (pair[1].score, pair[1].node_id),
                )[0]
                for cost in costs:
                    key = (shard, cost.node_id)
                    if key not in seen_costs:
                        seen_costs.add(key)
                        all_costs.append(cost)
            else:
                # The REQUEST-level pool, deliberately not filtered by S_j or
                # even by the shard's domain. §VI defines these arms that way:
                # "Least Loaded, which considers current workload but not shard
                # locality or synchronization state", and "Conventional
                # strategies may assign work to an FSN that does not maintain
                # the required shard, causing cross-node forwarding." Filtering
                # the pool per shard would hand them the locality AASS is
                # claimed to provide and flatten Exp. 8(c) to zero everywhere.
                pool = reachable
                if not holders:
                    unplaceable.append(shard)
                    continue
                if self.variant == VARIANT_NO_LB:
                    chosen = pool[0]
                elif self.variant == VARIANT_ROUND_ROBIN:
                    # OFFSET BY THE REQUEST, THEN BY THE SHARD.
                    #
                    # `self._cursor` alone rotates shards across the pool inside
                    # one request, which is what a per-shard Algorithm 1 wants.
                    # But it advances |S_Q| times per request, so whenever
                    # |S_Q| is a multiple of the pool size it lands back on the
                    # same offset -- the first shard goes to pool[0] on EVERY
                    # request. `busiest()` breaks ties by first-assigned, so
                    # `select()` then returned that same node forever and the
                    # round_robin arm became indistinguishable from `no_lb`,
                    # erasing the arm Exp. 7-8 compare against. `busiest()`'s
                    # own docstring warns about exactly this collapse.
                    #
                    # Before 5143ee7 `select()` advanced the cursor ONCE PER
                    # REQUEST and rotated correctly; this keeps that rotation
                    # while retaining the per-shard placement.
                    chosen = pool[(request_offset + self._cursor) % len(pool)]
                    self._cursor += 1
                else:  # VARIANT_LEAST_LOADED
                    chosen = min(
                        pool, key=lambda node: (node.queue_length, node.node_id)
                    )
            # §VI'S DEFINITION, APPLIED TO EVERY ARM.
            #
            # §VI Exp. 8: "A cross-node forward occurs when a scheduler assigns
            # a required shard to an FSN that does not maintain it, requiring
            # redirection to an eligible node." That is scheduler-agnostic —
            # any arm can score under it.
            #
            # This counter sat INSIDE the `else`, so the AASS branch never
            # touched it: AASS read 0 by construction and the three oblivious
            # arms read >0 by construction, and Fig. 8(c) measured the arm
            # DEFINITIONS rather than scheduler quality. §V then read that
            # figure as evidence that AASS "reduces unnecessary forwarding".
            #
            # Applied uniformly, AASS should still read 0 — but because its
            # eligibility guard genuinely never misplaces a shard, which is a
            # measurement, not a tautology.
            if shard not in chosen.index.policy_pairs:
                forwards += 1
            mapping.append((shard, chosen))

        return ShardAssignment(
            mapping=tuple(mapping),
            variant=self.variant,
            costs=tuple(all_costs),
            forwards=forwards,
            unplaceable=tuple(unplaceable),
        )

    def select(
        self, nodes: Sequence[FogSearchNode], request: SearchRequest
    ) -> Selection:
        """One node for the whole request — the reduction of ``M_Q``.

        Callers that must dispatch a request to a single process (the Exp. 7-8
        replay, and Exp. 2's single-shard query) use this. It runs Algorithm 1
        and then takes the node carrying the most shards; it never decides
        anything :meth:`assign` did not.
        """
        assignment = self.assign(nodes, request)
        chosen = assignment.busiest()
        if chosen is None:
            raise SchedulerError(
                f"no Fog Search Node maintains any required shard of "
                f"{sorted(request.shards)}"
            )
        # The three authorization-oblivious arms have no cost vector to show:
        # their rule reads a cursor or a queue depth, not SC_j. An empty tuple
        # says that honestly; a populated one would imply they consulted costs
        # they are defined not to consult.
        return Selection(
            node=chosen,
            variant=self.variant,
            costs=assignment.costs,
            assignment=assignment,
        )

    def score(
        self,
        candidates: Sequence[FogSearchNode],
        request: SearchRequest,
        *,
        cache: Optional[Dict[str, CostVector]] = None,
    ) -> Tuple[NodeCost, ...]:
        """The four weighted terms for each candidate — AASS's rule, alone.

        ``cache`` memoises the raw vectors by node id across one request's shard
        loop. Sound because the four terms do not depend on the shard (see
        :func:`estimate_candidate_count`), and load-bearing because without it a
        query authorized for ``|S_Q|`` shards would cost every eligible node
        ``|S_Q|`` times inside Exp. 2's and Exp. 7's timed regions.
        """
        if cache is None:
            raw = tuple(estimate_costs(node, request) for node in candidates)
        else:
            raw = tuple(
                cache.setdefault(node.node_id, estimate_costs(node, request))
                for node in candidates
            )
        normalized = normalize(
            raw,
            degenerate_value=self.config.scheduler.degenerate_term_value,
            epsilon=self.config.scheduler.epsilon,
        )
        return tuple(
            NodeCost(
                node_id=node.node_id,
                raw=raw_vector,
                normalized=norm_vector,
                score=norm_vector.score(self._weights),
            )
            for node, raw_vector, norm_vector in zip(candidates, raw, normalized)
        )

    def __repr__(self) -> str:
        return (
            f"Scheduler(variant={self.variant!r}, "
            f"weights={self._weights.status!r}, reportable={self.reportable})"
        )


def score_nodes(
    nodes: Sequence[FogSearchNode],
    request: SearchRequest,
    weights: scheme_config.SchedulerWeights,
    *,
    degenerate_value: float = 0.0,
    epsilon: float = 1e-9,
) -> Tuple[NodeCost, ...]:
    """Cost every node without selecting — for inspecting the scoring directly."""
    raw = tuple(estimate_costs(node, request) for node in nodes)
    normalized = normalize(raw, degenerate_value=degenerate_value, epsilon=epsilon)
    return tuple(
        NodeCost(
            node_id=node.node_id,
            raw=raw_vector,
            normalized=norm_vector,
            score=norm_vector.score(weights),
        )
        for node, raw_vector, norm_vector in zip(nodes, raw, normalized)
    )


__all__ = [
    "VARIANTS",
    "VARIANT_NO_LB",
    "VARIANT_ROUND_ROBIN",
    "VARIANT_LEAST_LOADED",
    "VARIANT_AASS",
    "AUTHORIZATION_AWARE",
    "SchedulerError",
    "SearchRequest",
    "CostVector",
    "NodeCost",
    "Selection",
    "ShardAssignment",
    "Scheduler",
    "estimate_costs",
    "estimate_candidate_count",
    "estimate_result_count",
    "sync_lag",
    "normalize",
    "score_nodes",
]
