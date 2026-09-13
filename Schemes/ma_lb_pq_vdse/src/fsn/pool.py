"""Run each Fog Search Node as an INDEPENDENT OS PROCESS.

global.yaml states: "Each FSN is an independent process holding its own index
shard, authorization version ``VID_j``, and request queue." The harness
replayed all of them inside one interpreter, which
``provenance.reportability()`` correctly refused to call a concurrency result:
under the GIL only one node executes at a time, so per-node utilization cannot
genuinely differ and Exp. 8's "standard deviation of FSN utilization" measures
scheduling bookkeeping rather than contention.

That also silently invalidated the λ sweep: with no real concurrency the AASS
score has nothing to discriminate on, so every weight vector scored alike and
the sweep returned a utilization spread of exactly 0.5 for essentially all
1001 vectors.

DESIGN
------
One forked worker per node, each owning its own ``FogSearchNode`` and its own
request queue. The parent sends ``(request_id, tokens, authorized)`` and
receives ``(request_id, ok, service_ns)``; only plain tuples cross the
boundary, so nothing depends on a live object reference surviving a fork.

Fork is required, not incidental: ``FogSearchNode`` is picklable (verified),
but a spawned worker would re-import and rebuild its shard, which would both
cost startup time inside the measured window and give each worker a *different*
index than the scheduler is reasoning about.

WHAT IS AND IS NOT MEASURED HERE
--------------------------------
``service_ns`` is measured INSIDE the worker, around the search call only, so
it excludes queue wait and IPC. That is deliberate: utilization means the
fraction of the window a node spent *serving*, and charging it for the
parent's dispatch overhead would make utilization an artifact of the harness.
End-to-end latency is timed in the parent, which is where a client would see
it.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import queue as queue_mod
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import fsn as fsn_mod
from . import search as search_mod


@dataclass(frozen=True)
class NodeOutcome:
    """What the parent learns about one dispatched request."""

    node_id: str
    ok: bool
    service_ns: int
    error: str = ""
    #: Index entries the shard actually walked for this request, and hits
    #: returned. Carried back so a concurrency run can say WHY a node was busy,
    #: not just how long. Added 2026-09-06 to diagnose PSA Exp. 8, whose four
    #: arms did not separate while Option D's separated ~9x: without this the
    #: only way to tell "the scheduler is broken" from "every request does no
    #: work" is to guess. Defaulted, so nothing that constructs a NodeOutcome
    #: without them breaks.
    entries_traversed: int = 0
    hits: int = 0


def _worker(node: "fsn_mod.FogSearchNode",
            inbox: "mp.Queue", outbox: "mp.Queue") -> None:
    """Own one FSN for the life of the run. Never returns until told to stop.

    Runs in a forked child. The node object was inherited through fork, so it
    is this process's private copy -- which is the entire point: two workers
    updating their own ``_service_ns`` is what makes per-node utilization real
    rather than a shared counter touched under the GIL.
    """
    while True:
        item = inbox.get()
        if item is None:  # shutdown sentinel
            return
        request_id, tokens, authorized, groups = item
        started = time.perf_counter_ns()
        ok, error = True, ""
        traversed, hits = 0, 0
        try:
            response = search_mod.execute_search(
                node, tokens, authorized, groups=groups
            )
            traversed = response.statistics.entries_traversed
            hits = len(response.hits)
        except search_mod.SearchRejected as exc:
            ok, error = False, f"rejected: {exc}"
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            ok, error = False, f"{type(exc).__name__}: {exc}"
        service_ns = time.perf_counter_ns() - started
        outbox.put(
            (request_id, node.node_id, ok, service_ns, error, traversed, hits)
        )


class FogSearchNodePool:
    """A forked worker per node, with per-node inboxes.

    Used as a context manager so workers are always reaped -- a leaked pool
    would keep m6i.xlarge cores busy and quietly corrupt the next measurement.
    """

    def __init__(self, nodes: Sequence["fsn_mod.FogSearchNode"]) -> None:
        if "fork" not in mp.get_all_start_methods():
            raise RuntimeError(
                "independent FSN processes require the 'fork' start method; "
                "this platform does not provide it (macOS/Windows). Run on the "
                "Linux experiment host, or accept the single-interpreter path "
                "and its reportability blocker."
            )
        self._ctx = mp.get_context("fork")
        self._nodes = list(nodes)
        self._inboxes: Dict[str, Any] = {}
        self._outbox = self._ctx.Queue()
        self._procs: List[Any] = []
        self._started = False

    def __enter__(self) -> "FogSearchNodePool":
        for node in self._nodes:
            inbox = self._ctx.Queue()
            proc = self._ctx.Process(
                target=_worker, args=(node, inbox, self._outbox), daemon=True
            )
            proc.start()
            self._inboxes[node.node_id] = inbox
            self._procs.append(proc)
        self._started = True
        return self

    def __exit__(self, *exc_info: Any) -> None:
        for inbox in self._inboxes.values():
            try:
                inbox.put(None)
            except Exception:  # noqa: BLE001 - shutting down regardless
                pass
        for proc in self._procs:
            proc.join(timeout=5)
            if proc.is_alive():
                proc.terminate()
        self._started = False

    @property
    def worker_pids(self) -> List[int]:
        """Evidence the topology is real -- recorded in run_meta.json."""
        return [p.pid for p in self._procs if p.pid]

    def dispatch(self, request_id: int, node_id: str,
                 tokens: Sequence[bytes],
                 authorized: Sequence[Tuple[str, str]],
                 groups=None) -> None:
        if not self._started:
            raise RuntimeError("pool used outside its context manager")
        self._inboxes[node_id].put(
            (request_id, list(tokens), list(authorized), groups)
        )

    def drain(self) -> List[NodeOutcome]:
        """Take the completions available right now, without blocking.

        The parent is the only producer into each worker's inbox and the only
        consumer of the outbox, so dispatched-minus-drained IS that node's true
        queue depth -- no load report has to cross back from the worker. But it
        is only true if the parent drains WHILE it dispatches: draining only at
        the end leaves every depth monotonically increasing, which is what left
        ``queue_length`` useless to ``least_loaded`` and ``C_j^queue``.
        """
        results: List[NodeOutcome] = []
        while True:
            try:
                (_rid, node_id, ok, service_ns, error,
                 traversed, hits) = self._outbox.get_nowait()
            except queue_mod.Empty:
                return results
            results.append(
                NodeOutcome(node_id=node_id, ok=ok,
                            entries_traversed=traversed, hits=hits,
                            service_ns=service_ns, error=error)
            )

    def collect(self, count: int, timeout: float = 60.0) -> List[NodeOutcome]:
        """Drain exactly ``count`` completions.

        Blocks, because the caller dispatched that many and the window is not
        over until they land. A timeout raises rather than returning a short
        list: silently reporting throughput over fewer requests than were
        issued would overstate it.
        """
        results: List[NodeOutcome] = []
        deadline = time.monotonic() + timeout
        while len(results) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"collected {len(results)}/{count} FSN responses before "
                    f"{timeout}s elapsed; reporting the partial set would "
                    f"overstate throughput"
                )
            (_rid, node_id, ok, service_ns, error,
             traversed, hits) = self._outbox.get(
                timeout=remaining
            )
            results.append(
                NodeOutcome(node_id=node_id, ok=ok,
                            entries_traversed=traversed, hits=hits,
                            service_ns=service_ns, error=error)
            )
        return results


def utilization_by_node(
    outcomes: Sequence[NodeOutcome],
    node_ids: Sequence[str],
    window_ns: int,
) -> Dict[str, float]:
    """Busy fraction per node over the window.

    Computed from the workers' own ``service_ns``, so a node that served
    nothing reports 0.0 rather than being absent -- Exp. 8's metric is the
    spread ACROSS nodes, and dropping idle nodes would understate it, which is
    the direction that would flatter the scheduler.
    """
    if window_ns <= 0:
        raise ValueError("window_ns must be positive")
    busy: Dict[str, int] = {node_id: 0 for node_id in node_ids}
    for outcome in outcomes:
        if outcome.node_id in busy:
            busy[outcome.node_id] += outcome.service_ns
    return {nid: min(1.0, ns / window_ns) for nid, ns in busy.items()}
