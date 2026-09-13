"""Which ledger a run uses, and whether that ledger is the one the manuscript (SVI) names.

One switch, read in one place, so the harness cannot end up running against
InProcessLedger while provenance stamps the run as Fabric-backed. That pairing
is the whole point: ``ledger_faithful`` in run_meta.json must describe the
object the experiment actually called.

    ABCD_LEDGER=fabric   talk to the running Fabric v2.5 network
    ABCD_LEDGER=memory   the in-process hash chain (default)

`fabric` REFUSES to fall back. If the network is down the run stops, because a
silent fallback would produce Exp. 4 numbers measured against a dict under a
run_meta that claims otherwise.
"""

from __future__ import annotations

import os
from typing import Tuple

from . import ledger as ledger_mod


def ledger_backend() -> str:
    return os.environ.get("ABCD_LEDGER", "memory").strip().lower()


def make_ledger(**kwargs):
    """A ledger for one deployment, per ``ABCD_LEDGER``."""
    backend = ledger_backend()
    if backend in ("memory", "", "inprocess"):
        return ledger_mod.InProcessLedger()
    if backend == "fabric":
        from .fabric_ledger import FabricLedger

        return FabricLedger(**kwargs)
    raise ValueError(
        f"unknown ABCD_LEDGER={backend!r}; expected 'fabric' or 'memory'"
    )


def ledger_is_faithful() -> bool:
    """What provenance.reportability()'s ``ledger_faithful`` must be told.

    the manuscript (SVI) specifies Hyperledger Fabric v2.5, so only the Fabric backend is
    faithful to it. Exp. 4 is gated on this because global.yaml puts chain
    consistency inside its measurement boundary.
    """
    return ledger_backend() == "fabric"


__all__ = ["ledger_backend", "make_ledger", "ledger_is_faithful"]
