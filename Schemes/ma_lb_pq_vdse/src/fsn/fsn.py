"""Fog Search Nodes — Phase I Step 4, and the receiving side of Phase V Step 4.

Phase I Step 4 initializes the Fog Search Node set

    F = {FSN_1, FSN_2, ..., FSN_m}

"where each Fog Search Node maintains searchable-index shards and executes
encrypted search requests". This module builds that ``F``: each node owns its
PDSI shard, its synchronized authorization version ``VID_j``, and its request
queue.

Everything here exists because Phase VI Step 3 reads it. The AASS score

    SC_j = L1*C_j^index + L2*C_j^verify + L3*C_j^sync + L4*C_j^queue

estimates its terms from ``|Cand_Q^(j)|`` (the shard's authorization bitmaps),
``N_j`` (its entry count), the per-authority ``Meta_i`` this node has applied
(``C_j^sync`` counts how many of ``V_Q`` it lags -- see
``FogSearchNode.vid_for_authority``), and ``T_j^queue`` (measured waiting time).

FOUR terms, per ``eq:search-cost``. A fifth, ``C_j^auth = |P_Q|``, belonged to
the previous manuscript revision and was removed on 2026-09-07.

**The node owns its shard.** An earlier revision had ``ShardState`` tracking a
separate entry count alongside the index's own, which meant ``N_j`` had two
sources that could drift — and a scheduler costing queries against a stale
``N_j`` produces plausible, wrong Exp. 2 numbers. The node now holds a
:class:`~..index.dsi.DynamicSearchIndex` directly and ``entry_count`` reads
through to it, so there is exactly one count.

**No shared state between nodes.** global.yaml requires each FSN to be an
independent process. Phases I-VI run them in one process, so nothing here holds a
reference to another node, to the AIM, or to the ledger: the AIM pushes
authorization state in (:meth:`FogSearchNode.apply_meta`), Phase V Step 4 pushes
index state in (:meth:`FogSearchNode.apply_sync`), and a node never reaches out.
That is what makes the later process split a change of transport rather than of
behaviour.
"""

from __future__ import annotations

import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from ..index.dsi import DynamicSearchIndex  # noqa: E402
from ..types import AuthorizationMeta, IndexEntry, SyncPayload  # noqa: E402


class FSNError(RuntimeError):
    """Raised on an invalid Fog Search Node operation."""


@dataclass
class QueuedRequest:
    """One pending search request.

    ``enqueued_ns`` is a monotonic timestamp, so ``T_j^queue`` is a measured
    waiting time rather than a queue-length proxy.
    """

    request_id: str
    enqueued_ns: int


@dataclass
class FogSearchNode:
    """``FSN_j`` — its PDSI shard, request queue, and synchronized ``VID_j``."""

    node_id: str
    index: DynamicSearchIndex
    _queue: Deque[QueuedRequest] = field(default_factory=deque, repr=False)
    # Meta_i per authority, as delivered by the AIM in Phase II Step 4 and
    # updated by DIAS in Phase VII Step 4.
    _synced: Dict[str, AuthorizationMeta] = field(default_factory=dict, repr=False)
    # CIDs whose Sync_i this node has applied — the Phase V Step 4 replay guard.
    _applied: Set[str] = field(default_factory=set, repr=False)
    _service_ns: int = field(default=0, repr=False)
    _served: int = field(default=0, repr=False)

    def __post_init__(self) -> None:
        if not self.node_id:
            raise ValueError("node_id must not be empty")

    # -- construction -------------------------------------------------------
    @classmethod
    def create(
        cls,
        node_id: str,
        domains: Iterable[str],
        *,
        bloom_bits_per_entry: int = 10,
        bloom_num_hashes: int = 7,
    ) -> "FogSearchNode":
        domains = frozenset(domains)
        if not domains:
            raise FSNError(
                f"{node_id}: a Fog Search Node must serve at least one domain; "
                f"an unassigned node would never be selected and would distort "
                f"the Exp. 8 utilization spread"
            )
        return cls(
            node_id=node_id,
            index=DynamicSearchIndex(
                domains=domains,
                bloom_bits_per_entry=bloom_bits_per_entry,
                bloom_num_hashes=bloom_num_hashes,
            ),
        )

    # -- shard state (Phase IV Step 3 / Phase V Step 4) ---------------------
    @property
    def entry_count(self) -> int:
        """``N_j`` — index entries held. One source of truth: the shard itself."""
        return self.index.entry_count

    @property
    def domains(self) -> FrozenSet[str]:
        return self.index.domains

    def serves_domain(self, domain: str) -> bool:
        return domain in self.index.domains

    def insert_entries(
        self, entries: Sequence[IndexEntry], *, domain: str
    ) -> Tuple[int, ...]:
        """Add index entries to this node's shard."""
        if not self.serves_domain(domain):
            raise FSNError(
                f"{self.node_id} serves {sorted(self.domains)} and cannot hold "
                f"entries for domain {domain!r}"
            )
        return self.index.insert_record(entries, domain=domain)

    def has_applied(self, cid: str) -> bool:
        return cid in self._applied

    def apply_sync(self, payload: SyncPayload, *, domain: str) -> int:
        """Apply ``Sync_i`` — the receiving side of Phase V Step 4.

        Refuses a replay: a second application of the same ``CID_i`` would insert
        the record's entries twice, inflating ``N_j`` and returning duplicate hits
        for one record — a corruption no Merkle proof would catch, because each
        duplicate entry is individually well-formed.
        """
        if not self.serves_domain(domain):
            raise FSNError(
                f"{self.node_id} serves {sorted(self.domains)} and is not an "
                f"authorized node for domain {domain!r}"
            )
        if self.has_applied(payload.cid):
            raise FSNError(
                f"{self.node_id} has already applied Sync_i for CID "
                f"{payload.cid!r}; re-applying would double-insert the record"
            )
        self.index.insert_record(payload.entries, domain=domain)
        self._applied.add(payload.cid)
        return payload.entry_count

    @property
    def applied_records(self) -> int:
        return len(self._applied)

    # -- authorization state (Phase II Step 4 / Phase VII Step 4) -----------
    def apply_meta(self, authority_id: str, meta: AuthorizationMeta) -> bool:
        """Apply ``Meta_i = (Dom_i, VID_i, C_i^auth)`` from the AIM.

        Returns whether this node's state changed. A node rejects a version older
        than the one it holds: authorization versions only advance
        (``VID' = VID + 1``, Phase VII Step 2), so an older message is a replay or
        a reordered delivery, and applying it would silently roll the node's
        authorization state backwards.
        """
        existing = self._synced.get(authority_id)
        if existing is not None:
            if meta.vid < existing.vid:
                raise FSNError(
                    f"{self.node_id}: refusing Meta for {authority_id!r} at VID "
                    f"{meta.vid}; already synchronized at VID {existing.vid}. "
                    f"Authorization versions only advance."
                )
            if meta == existing:
                return False
        self._synced[authority_id] = meta
        return True

    def synced_authorities(self) -> Tuple[str, ...]:
        return tuple(sorted(self._synced))

    def meta_for_authority(self, authority_id: str) -> AuthorizationMeta:
        try:
            return self._synced[authority_id]
        except KeyError:
            raise FSNError(
                f"{self.node_id} holds no authorization state for "
                f"{authority_id!r}"
            ) from None

    def vid_for_authority(self, authority_id: str) -> int:
        return self.meta_for_authority(authority_id).vid

    def vid_for_domains(self, domains: Iterable[str]) -> int:
        """The synchronized version across ``domains``, as the minimum.

        This is the accurate form of ``VID_j`` for a query: a node is only as
        fresh as the stalest authority among the domains the query touches, since
        it cannot serve a version it has not received.

        Raises if the node holds no state for a requested domain — that is not a
        version-zero node, it is a node that was never synchronized, and treating
        the two alike would let an unsynchronized node look perfectly fresh.
        """
        requested = list(domains)
        if not requested:
            raise ValueError("domains must not be empty")
        versions: List[int] = []
        for domain in requested:
            matches = [
                meta.vid for meta in self._synced.values() if meta.domain == domain
            ]
            if not matches:
                raise FSNError(
                    f"{self.node_id} holds no authorization state for domain "
                    f"{domain!r}"
                )
            versions.extend(matches)
        return min(versions)

    def vid(self) -> int:
        """``VID_j`` — the scalar of Phase VI Step 3's ``C_j^sync``.

        Phase VI writes ``C_j^sync = |VID_U - VID_j|`` with a single ``VID_j``,
        while Phase II Step 4 synchronizes one ``Meta_i`` per authority. The
        aggregation is therefore ours to choose, and it is the **minimum** across
        synchronized authorities: a node that has not applied the newest DIAS for
        any one authority genuinely cannot serve that authority's current state,
        so the minimum is the version the node can actually honour across the
        board. Taking the maximum would let one freshly-synced authority mask
        staleness in every other.

        ``benchmark`` provenance, matching ``user/profile.py::aggregate_vid`` so
        that ``|VID_U - VID_j|`` subtracts comparable quantities. Prefer
        :meth:`vid_for_domains`, which is strictly more accurate when the query's
        domains are known.

        Returns 0 for a node with no synchronized state, which is the honest
        floor: it holds nothing, so it is behind every published version.
        """
        if not self._synced:
            return 0
        return min(meta.vid for meta in self._synced.values())

    def commitment_for_authority(self, authority_id: str) -> bytes:
        return self.meta_for_authority(authority_id).commitment

    # -- request queue (Phase VI / Exp. 7-8) --------------------------------
    def enqueue(self, request_id: str, *, now_ns: Optional[int] = None) -> int:
        """Append a request. Returns the queue length after insertion."""
        self._queue.append(
            QueuedRequest(
                request_id=request_id,
                enqueued_ns=time.perf_counter_ns() if now_ns is None else now_ns,
            )
        )
        return len(self._queue)

    def dequeue(self) -> QueuedRequest:
        if not self._queue:
            raise FSNError(f"{self.node_id}: queue is empty")
        return self._queue.popleft()

    @property
    def queue_length(self) -> int:
        return len(self._queue)

    def queue_wait_ns(self, *, now_ns: Optional[int] = None) -> int:
        """``T_j^queue`` — how long the oldest pending request has waited.

        A measured wait rather than the queue length: two nodes with equal queues
        but different service rates do not have equal waiting times, and the
        published estimator is a time.
        """
        if not self._queue:
            return 0
        now = time.perf_counter_ns() if now_ns is None else now_ns
        return max(0, now - self._queue[0].enqueued_ns)

    def record_service(self, duration_ns: int) -> None:
        """Record time spent serving, for the Exp. 8 utilization sample."""
        if duration_ns < 0:
            raise ValueError("duration_ns must be non-negative")
        self._service_ns += duration_ns
        self._served += 1

    @property
    def service_ns(self) -> int:
        return self._service_ns

    @property
    def served_count(self) -> int:
        return self._served

    def utilization(self, window_ns: int) -> float:
        """Busy fraction over ``window_ns`` — sampled every 100 ms in Exp. 8."""
        if window_ns <= 0:
            raise ValueError("window_ns must be positive")
        return min(1.0, self._service_ns / window_ns)

    def __repr__(self) -> str:
        return (
            f"FogSearchNode(id={self.node_id!r}, "
            f"domains={sorted(self.domains)}, N_j={self.entry_count}, "
            f"VID_j={self.vid()}, queue={self.queue_length})"
        )


def assign_domains_to_fsns(
    domains: Sequence[str], fsn_count: int, *, replication: int = 1
) -> Tuple[Tuple[str, ...], ...]:
    """Distribute ``domains`` across ``fsn_count`` nodes, largest-first.

    ``index.yaml`` sets ``overflow_policy: pack_largest_first``, matching the rule
    ``Dataset/prepare_dataset.py`` uses to balance domains. Exp. 3 sweeps ``d``
    to 10 against ``m = 4``, where nodes take multiple domains.

    ``replication`` is how many nodes hold each domain's shard, and it is what
    gives the scheduler a choice to make. At 1, the eligible set for any shard
    is a SINGLETON: AASS's ``S notin S_j`` guard forces it to the one holder, so
    it cannot balance load at all, while the three oblivious arms spread work
    over nodes that cannot serve the shard and pay a cross-node forward for
    every one. Exp. 8 panel (a) then measures how evenly work can be spread
    while ignoring correctness, and AASS loses it by construction rather than on
    merit. Raised to 2 on 2026-09-12 for that reason.

    Replicas are placed at consecutive offsets, so a domain lands on nodes
    ``i, i+1, ..., i+replication-1`` (mod ``fsn_count``). That keeps every node
    holding the same number of shards -- the placement itself contributes no
    imbalance for the scheduler to be credited with removing.

    Domains are assumed equal-sized, which the frozen corpus makes true
    (285,268 records x 4 exactly), so round-robin over sorted domains IS
    largest-first for our data. The name follows the config; if domains ever
    become unequal, this needs their sizes.
    """
    if fsn_count < 1:
        raise ValueError("fsn_count must be >= 1")
    if not domains:
        raise ValueError("domains must not be empty")
    if len(set(domains)) != len(domains):
        raise ValueError("duplicate domain in the assignment")
    if replication < 1:
        raise ValueError("replication must be >= 1")
    if replication > fsn_count:
        raise FSNError(
            f"replication={replication} exceeds fsn_count={fsn_count}; a shard "
            f"cannot have more holders than there are nodes"
        )
    buckets: List[List[str]] = [[] for _ in range(fsn_count)]
    for index, domain in enumerate(sorted(domains)):
        for offset in range(replication):
            buckets[(index + offset) % fsn_count].append(domain)
    empty = [i for i, bucket in enumerate(buckets) if not bucket]
    if empty:
        raise FSNError(
            f"{len(domains)} domains cannot fill {fsn_count} Fog Search Nodes; "
            f"nodes {empty} would hold no shard and never be selected"
        )
    # De-duplicate while keeping order: at d < m*replication one domain can be
    # placed on the same node twice, and a repeated domain would double-count
    # that node's shard in `policy_pairs`.
    return tuple(tuple(dict.fromkeys(bucket)) for bucket in buckets)


def build_fsn_set(
    domains: Sequence[str],
    fsn_count: int,
    *,
    prefix: str = "FSN",
    bloom_bits_per_entry: int = 10,
    bloom_num_hashes: int = 7,
    replication: int = 1,
) -> Tuple[FogSearchNode, ...]:
    """Phase I Step 4: build ``F = {FSN_1, ..., FSN_m}``, each with its shard.

    Node identifiers are 1-based to match the manuscript's ``FSN_1..FSN_m``.
    """
    assignment = assign_domains_to_fsns(
        domains, fsn_count, replication=replication
    )
    return tuple(
        FogSearchNode.create(
            f"{prefix}{index}",
            node_domains,
            bloom_bits_per_entry=bloom_bits_per_entry,
            bloom_num_hashes=bloom_num_hashes,
        )
        for index, node_domains in enumerate(assignment, start=1)
    )


def build_fsn_set_from_config(config, domains: Sequence[str]) -> Tuple[FogSearchNode, ...]:
    """Build ``F`` with ``m`` and the Bloom sizing taken from configuration."""
    return build_fsn_set(
        domains,
        config.topology.fog_search_nodes,
        bloom_bits_per_entry=config.index.bloom_bits_per_entry,
        bloom_num_hashes=config.index.bloom_num_hashes,
        replication=config.index.replication,
    )


__all__ = [
    "FSNError",
    "QueuedRequest",
    "FogSearchNode",
    "assign_domains_to_fsns",
    "build_fsn_set",
    "build_fsn_set_from_config",
]
