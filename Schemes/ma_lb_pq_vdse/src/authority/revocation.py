"""RevRoot_i — the authenticated revocation root of Phase II Step 3.

Phase II Step 3: "Each authority initializes its authorization state by assigning
a version identifier ``VID_i`` and constructing an **authenticated revocation
root** ``RevRoot_i`` over the current revocation list." Phase VII Step 2 then
"updates its revocation root ``RevRoot_k'``" whenever revocation state changes.

*Authenticated* is why this is a Merkle tree rather than a flat digest: a root
that can only be recomputed from the whole list proves nothing to a party
holding one identifier, whereas a Merkle root admits an inclusion proof. Nothing
in Phases I-II consumes such a proof yet, but the construction the paper names is
the one built here, and :meth:`RevocationList.prove` exposes it.

Two design points the manuscript leaves open, both settled here and tested:

**The root is a function of the set, not of insertion order.** "Over the
current revocation list" reads as a function of the *set* of revoked
identifiers. A verifier who had to know the order in which revocations happened
could not recompute the root at all.

This was first built as a sorted array Merkle tree, which has that property but
pays for it: inserting one identifier shifts every leaf after it, so a single
revocation costs a full O(n) rebuild, and Phase VII reads the root once per
update. Measured on the pinned host, ``delta`` revocations cost **O(n^2.02)** —
0.8 s at ``delta = 1,000``, 53 s at 8,000, extrapolating to ~8,700 s per run at
the published ``delta = 10^5``, or ~84 h across 35 runs. The 2026-08-28 campaign
was killed with zero Exp. 6 points because of it (skill.md item 10).

It now uses :class:`Common.crypto.merkle.SetMerkleTrie`, a canonical binary
radix Merkle trie keyed by the leaf digest. The shape depends on the key set
alone, so the root is still order-independent and ``restore`` still returns to
the exact previous root — while an insertion or deletion rewrites only the
O(log n) nodes on one path. That is the incremental update Phase VII Step 2
already claims to perform, rather than a rebuild wearing its name.

**The empty list needs a sentinel.** ``Common/crypto/merkle.py`` refuses a
zero-leaf tree ("a Merkle tree needs at least one leaf"), and rightly — there is
no meaningful root over nothing. But every authority starts with an empty
revocation list at Phase II Step 3, so ``RevRoot_i`` must be defined there.
:data:`EMPTY_REVOCATION_ROOT` is a domain-separated constant that cannot collide
with any populated tree's root: a populated root is built from
``merkle.hash_leaf``/``hash_node``, which prefix their inputs with ``0x00``/
``0x01``, while the sentinel is a ``hashes.sha256`` digest under its own domain
tag. Using ``bytes(32)`` or an all-zero root instead would collide with any tree
an adversary could contrive to produce zeros, and would make "no revocations"
indistinguishable from "root not yet computed".
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from Common.crypto import hashes, merkle  # noqa: E402

# Domain tag for revocation-list leaves, so a revoked-identifier leaf cannot be
# reinterpreted as a leaf of the searchable index or of any other tree in the
# scheme.
_LEAF_DOMAIN = b"revocation/leaf/v1"

#: RevRoot_i for an empty revocation list — the state every authority is in at
#: Phase II Step 3. Distinct by construction from every populated root.
EMPTY_REVOCATION_ROOT = hashes.sha256(
    b"empty-revocation-list", domain=b"revocation/empty/v1"
)


class RevocationError(RuntimeError):
    """Raised on an invalid revocation-list operation."""


def revocation_leaf(identifier: str) -> bytes:
    """Canonical leaf bytes for a revoked identifier.

    The identifier is domain-tagged and hashed rather than used raw, so leaves
    are fixed width and the tree does not carry plaintext user identifiers.
    """
    if not isinstance(identifier, str):
        raise TypeError(
            f"revoked identifier must be str, got {type(identifier).__name__}"
        )
    if not identifier:
        raise ValueError("revoked identifier must not be empty")
    return hashes.sha256(identifier.encode("utf-8"), domain=_LEAF_DOMAIN)


class RevocationList:
    """The revocation list of one Attribute Authority, with its Merkle root.

    The root is maintained incrementally: each revocation or restoration rewrites
    one root-to-leaf path in O(log n), so reading :meth:`root` is free and the
    cost of a revocation does not grow with how many identifiers are already
    revoked. Exp. 6 sweeps ``delta`` to 10^5 and reads the root after every
    update, so anything worse than that measures this class instead of the DIAS
    mechanism it is meant to measure.
    """

    def __init__(self, revoked: Iterable[str] = ()) -> None:
        self._revoked: set[str] = set()
        self._trie = merkle.SetMerkleTrie()
        for identifier in revoked:
            self.revoke(identifier)

    # -- state --------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._revoked)

    def __contains__(self, identifier: str) -> bool:
        return identifier in self._revoked

    @property
    def is_empty(self) -> bool:
        return not self._revoked

    @property
    def revoked(self) -> Tuple[str, ...]:
        """The revoked set in canonical (sorted) order."""
        return tuple(sorted(self._revoked))

    @property
    def path_updates(self) -> int:
        """How many root-to-leaf paths have been rewritten.

        Exposed so a caller can assert that ``delta`` revocations rewrote
        exactly ``delta`` paths — i.e. that the update really was incremental.
        A test that only checked the root would pass either way, which is how
        the O(delta^2) rebuild survived as long as it did.
        """
        return self._trie.path_updates

    # -- mutation -----------------------------------------------------------
    def revoke(self, identifier: str) -> None:
        """Add one identifier. Idempotent; updates one Merkle path."""
        leaf = revocation_leaf(identifier)  # validate before mutating
        if identifier not in self._revoked:
            self._revoked.add(identifier)
            self._trie.insert(leaf)

    def revoke_many(self, identifiers: Iterable[str]) -> int:
        """Add several identifiers. Returns how many were newly revoked."""
        added = 0
        for identifier in identifiers:
            if identifier not in self._revoked:
                self.revoke(identifier)
                added += 1
        return added

    def restore(self, identifier: str) -> None:
        """Remove an identifier from the revocation list.

        Present because ``RevRoot_i`` is defined over the *current* list and the
        list is not append-only in principle. The ledger's history of published
        ``C_i^auth`` values is what remains immutable.
        """
        if identifier not in self._revoked:
            raise RevocationError(f"{identifier!r} is not revoked")
        self._revoked.discard(identifier)
        self._trie.delete(revocation_leaf(identifier))

    # -- root ---------------------------------------------------------------
    def root(self) -> bytes:
        """RevRoot_i over the current list.

        Returns :data:`EMPTY_REVOCATION_ROOT` when the list is empty, and the
        Merkle root over the sorted leaves otherwise.
        """
        root = self._trie.root()
        return EMPTY_REVOCATION_ROOT if root is None else root

    def tree(self) -> merkle.SetMerkleTrie:
        """The underlying trie. Raises when the list is empty."""
        if not self._revoked:
            raise RevocationError(
                "an empty revocation list has no Merkle tree; its root is the "
                "EMPTY_REVOCATION_ROOT sentinel"
            )
        return self._trie

    def prove(self, identifier: str) -> merkle.MerkleProof:
        """Inclusion proof that ``identifier`` is revoked.

        O(log n), and it does not rebuild anything — the previous version built
        a whole fresh tree and then did an O(n) ``list.index`` to find the leaf
        position, on every single call.
        """
        if identifier not in self._revoked:
            raise RevocationError(f"{identifier!r} is not revoked; nothing to prove")
        return self._trie.prove(revocation_leaf(identifier))

    def verify(self, identifier: str, proof: merkle.MerkleProof) -> bool:
        """Check an inclusion proof against the current root."""
        if self.is_empty:
            return False
        return merkle.MerkleTree.verify(proof, self.root())

    def __repr__(self) -> str:  # pragma: no cover - diagnostic only
        return (
            f"RevocationList(size={len(self._revoked)}, "
            f"root={self.root().hex()[:16]}...)"
        )


def revocation_root(revoked: Sequence[str]) -> bytes:
    """One-shot ``RevRoot_i`` over a revocation list."""
    return RevocationList(revoked).root()


__all__ = [
    "EMPTY_REVOCATION_ROOT",
    "RevocationError",
    "RevocationList",
    "revocation_leaf",
    "revocation_root",
]
