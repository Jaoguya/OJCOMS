"""Canonical record types and byte encodings for MA-LB-PQ-VDSE.

Every integrity claim in the scheme is a hash over a concatenation of
heterogeneous fields:

    C_i^auth   = H(ID_i || Dom_i || H(A_i) || VID_i || RevRoot_i)   Phase II Step 3
    AuthRoot_U = H(UID || H(S_U) || VID_U || H(C_U))                Phase III Step 4
    T_j        = H(w_j || PID_i || VID_i || Dom_i)                  Phase IV Step 2
    Commit_i   = H(Root_i || PID_i || VID_i || AuthRoot_DO)         Phase IV Step 5

The manuscript's ``||`` is plain concatenation, which is ambiguous in two
separate ways: it cannot distinguish different splits of the same byte string
(``("ab","c")`` vs ``("a","bc")``), and it cannot distinguish a field's *type*
(the integer ``VID_i = 53`` from the string ``"53"``). Either ambiguity lets two
different authorization states produce one commitment, which would defeat the
binding property the commitments exist to provide.

This module therefore defines ONE canonical encoding, used by every record that
is hashed or published, and every protocol record as a frozen dataclass over
it. Two rules follow from the ambiguity above and are enforced here rather than
by convention:

* every field is length-prefixed and type-tagged (``canonical``);
* every record type carries its own domain tag, so two records with identical
  field values but different meanings cannot share a digest.

**Group elements are ``bytes`` at this layer.** Pairing elements are
backend-specific objects (charm), so serialising them is the pairing layer's
job, not this one's: ``abe/ma_cpabe.py`` converts backend elements to and from
bytes and everything here deals in the serialised form. That keeps this module
and ``chain/ledger.py`` fully testable on a host with no pairing backend
installed, which the macOS development host is.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from Common.crypto import hashes  # noqa: E402

# Digest width for every commitment in the scheme: §VI specifies SHA-256 for
# hashing and Merkle-tree construction (crypto.yaml -> ma_lb_pq_vdse
# .hash_output_bits: 256). Records validate against this rather than accepting
# any byte string, so a truncated or wrong-algorithm digest fails at
# construction instead of silently producing a valid-looking commitment.
DIGEST_BYTES = 32


class EncodingError(TypeError):
    """A value cannot be canonically encoded."""


class SecretMaterialError(RuntimeError):
    """Raised on any attempt to encode or publish secret key material."""


# ===========================================================================
# Canonical encoding
# ===========================================================================
# Type tags. Distinct tags are what make the encoding injective ACROSS types:
# without them, canonical(b"") and canonical(0) and canonical("") would all be
# the empty string with a zero length prefix.
_T_BYTES = b"\x01"
_T_UINT = b"\x02"
_T_NEGINT = b"\x03"
_T_STR = b"\x04"
_T_SEQ = b"\x05"
_T_RECORD = b"\x06"
_T_BOOL = b"\x07"


def _u32(n: int) -> bytes:
    if n < 0 or n > 0xFFFFFFFF:
        raise EncodingError(f"length {n} does not fit in 4 bytes")
    return n.to_bytes(4, "big")


def _int_magnitude(n: int) -> bytes:
    """Minimal big-endian magnitude. Zero encodes as empty, so it is unique."""
    if n == 0:
        return b""
    return n.to_bytes((n.bit_length() + 7) // 8, "big")


def canonical(value: Any) -> bytes:
    """Injective byte encoding of ``value``.

    Injective means: two values encode to the same bytes only if they are the
    same value. That is the property the commitments depend on.

    Supported: ``bytes``, ``bool``, ``int`` (any sign), ``str``, ``Sequence``,
    and :class:`Record`. ``dict`` is deliberately REJECTED — a mapping has no
    inherent order, and silently sorting one here would hide an ordering
    decision that belongs at the call site where its consequences are visible
    (see :class:`PublicParameters`, which holds sorted pairs).
    """
    # bool before int: bool is a subclass of int, and True would otherwise
    # encode identically to 1.
    if isinstance(value, bool):
        return _T_BOOL + (b"\x01" if value else b"\x00")
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = bytes(value)
        return _T_BYTES + _u32(len(raw)) + raw
    if isinstance(value, int):
        magnitude = _int_magnitude(abs(value))
        tag = _T_NEGINT if value < 0 else _T_UINT
        return tag + _u32(len(magnitude)) + magnitude
    if isinstance(value, str):
        raw = value.encode("utf-8")
        return _T_STR + _u32(len(raw)) + raw
    if isinstance(value, Record):
        body = value.encode()
        return (
            _T_RECORD
            + _u32(len(value.DOMAIN))
            + value.DOMAIN
            + _u32(len(body))
            + body
        )
    if isinstance(value, dict):
        raise EncodingError(
            "dict is not canonically encodable: pass a sorted sequence of "
            "(key, value) pairs so the ordering is explicit"
        )
    if isinstance(value, Sequence):
        parts = [canonical(item) for item in value]
        return _T_SEQ + _u32(len(parts)) + b"".join(parts)
    raise EncodingError(f"cannot canonically encode {type(value).__name__}")


def digest(value: Any, *, domain: bytes) -> bytes:
    """Domain-separated SHA-256 over the canonical encoding of ``value``."""
    return hashes.sha256(canonical(value), domain=domain)


def _check_digest(name: str, value: bytes) -> None:
    if not isinstance(value, (bytes, bytearray)):
        raise TypeError(f"{name} must be bytes, got {type(value).__name__}")
    if len(value) != DIGEST_BYTES:
        raise ValueError(
            f"{name} must be {DIGEST_BYTES} bytes (SHA-256), got {len(value)}"
        )


def _check_identifier(name: str, value: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str, got {type(value).__name__}")
    if not value:
        raise ValueError(f"{name} must not be empty")


def _check_vid(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"VID must be int, got {type(value).__name__}")
    # Phase VII Step 2 defines VID' = VID + 1, and Phase VI's synchronisation
    # cost is C_j^sync = |VID_U - VID_j|; both require a monotone counter, so a
    # negative version is a bug rather than an unusual input.
    if value < 0:
        raise ValueError(f"VID must be non-negative, got {value}")


# ===========================================================================
# Record base
# ===========================================================================
@dataclass(frozen=True)
class Record:
    """A protocol record with a canonical encoding and a domain tag.

    Frozen because these are published objects: a record whose fields could
    change after its digest was anchored on the ledger would break the binding
    the anchor is for.
    """

    DOMAIN: ClassVar[bytes] = b""

    def _encoded_fields(self) -> Tuple[Any, ...]:
        raise NotImplementedError

    def encode(self) -> bytes:
        """Canonical bytes for this record's fields (no domain tag)."""
        return canonical(self._encoded_fields())

    def digest(self) -> bytes:
        """Domain-separated SHA-256 of this record."""
        return hashes.sha256(self.encode(), domain=self.DOMAIN)


# ===========================================================================
# Phase I — System Initialization
# ===========================================================================
@dataclass(frozen=True)
class PrimitiveSuite(Record):
    """The primitive set P of Phase I Step 1, plus the resolved pairing.

    Phase I Step 1 fixes P = {H, SHA-256, AES-256-GCM, HKDF, ML-KEM}. This
    record does not implement any of them — they come from ``Common/crypto``
    (skill.md: primitives a paper *cites* are shared). It records WHICH
    instantiation was resolved, so the choice lands in ``run_meta.json`` and a
    reader can tell whether a result came from liboqs ML-KEM or a pure-Python
    fallback, or from a Type-I curve substituted for the published Type-III.
    """

    DOMAIN: ClassVar[bytes] = b"suite/v1"

    hash_algorithm: str
    aead_algorithm: str
    kdf_algorithm: str
    kem_algorithm: str
    kem_backend: str
    pairing_type: str
    pairing_curve: str
    pairing_backend: str

    def __post_init__(self) -> None:
        for name in (
            "hash_algorithm",
            "aead_algorithm",
            "kdf_algorithm",
            "kem_algorithm",
            "kem_backend",
            "pairing_type",
            "pairing_curve",
            "pairing_backend",
        ):
            _check_identifier(name, getattr(self, name))
        # crypto.yaml -> ma_lb_pq_vdse.pairing.allow_symmetric_backend: false.
        # Phase I Step 1 publishes e : G_1 x G_2 -> G_T; a symmetric curve
        # changes both group-element sizes and pairing cost, so substituting
        # one is a construction change, not a backend detail.
        if self.pairing_type != "type-3":
            raise ValueError(
                f"the proposed scheme publishes a Type-III pairing "
                f"(e : G_1 x G_2 -> G_T); refusing pairing_type="
                f"{self.pairing_type!r}"
            )

    def _encoded_fields(self) -> Tuple[Any, ...]:
        return (
            self.hash_algorithm,
            self.aead_algorithm,
            self.kdf_algorithm,
            self.kem_algorithm,
            self.kem_backend,
            self.pairing_type,
            self.pairing_curve,
            self.pairing_backend,
        )


@dataclass(frozen=True)
class AuthorityPublicKey(Record):
    """PK_i = (g_1, g_2, e(g_1,g_2)^{alpha_i}, g_1^{beta_i}) — Phase I Step 2.

    Elements are serialised group elements; see the module docstring on why
    this layer holds bytes rather than backend objects.
    """

    DOMAIN: ClassVar[bytes] = b"authority-pk/v1"

    g1: bytes
    g2: bytes
    e_g1g2_alpha: bytes
    g1_beta: bytes

    def __post_init__(self) -> None:
        for name in ("g1", "g2", "e_g1g2_alpha", "g1_beta"):
            value = getattr(self, name)
            if not isinstance(value, (bytes, bytearray)):
                raise TypeError(f"{name} must be bytes")
            if not value:
                raise ValueError(f"{name} must not be empty")

    def _encoded_fields(self) -> Tuple[Any, ...]:
        return (self.g1, self.g2, self.e_g1g2_alpha, self.g1_beta)


@dataclass(frozen=True)
class AuthorityMasterKey:
    """MSK_i = (alpha_i, beta_i) — Phase I Step 2. NEVER published.

    Deliberately NOT a :class:`Record`: it has no canonical encoding and no
    digest, so it cannot be handed to the ledger, hashed into a commitment, or
    reached through any code path that publishes. ``encode`` exists only to
    raise, so an attempt to serialise it fails loudly at the call site instead
    of quietly leaking alpha_i into an anchored record.
    """

    alpha: bytes
    beta: bytes

    def __post_init__(self) -> None:
        for name in ("alpha", "beta"):
            value = getattr(self, name)
            if not isinstance(value, (bytes, bytearray)):
                raise TypeError(f"{name} must be bytes")
            if not value:
                raise ValueError(f"{name} must not be empty")

    def encode(self) -> bytes:  # pragma: no cover - exists to raise
        raise SecretMaterialError(
            "MSK_i = (alpha_i, beta_i) is master secret key material and has "
            "no published encoding"
        )

    def digest(self) -> bytes:  # pragma: no cover - exists to raise
        raise SecretMaterialError("refusing to digest master secret key material")

    def __repr__(self) -> str:
        return "AuthorityMasterKey(alpha=<redacted>, beta=<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True)
class PublicParameters(Record):
    """PP = (G_1, G_2, G_T, e, g_1, g_2, {PK_i}, P) — Phase I Step 3.

    The groups and the map are identified by the suite's curve and backend
    rather than carried as objects: two runs agree on PP exactly when they
    resolved the same curve, and that is what the digest needs to capture.

    ``authority_public_keys`` is a tuple of ``(ID_i, PK_i)`` pairs sorted by
    ID, not a dict, so the PP digest does not depend on the order authorities
    happened to register in.
    """

    DOMAIN: ClassVar[bytes] = b"public-parameters/v1"

    suite: PrimitiveSuite
    g1: bytes
    g2: bytes
    authority_public_keys: Tuple[Tuple[str, AuthorityPublicKey], ...] = field(
        default=()
    )

    def __post_init__(self) -> None:
        ids = [authority_id for authority_id, _ in self.authority_public_keys]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate authority ID in PP")
        if list(ids) != sorted(ids):
            raise ValueError(
                "authority_public_keys must be sorted by authority ID so the "
                "PP digest is independent of registration order; use "
                "PublicParameters.build()"
            )

    @classmethod
    def build(
        cls,
        suite: PrimitiveSuite,
        g1: bytes,
        g2: bytes,
        authority_public_keys: Sequence[Tuple[str, AuthorityPublicKey]] = (),
    ) -> "PublicParameters":
        """Construct PP, sorting the authority keys by ID."""
        return cls(
            suite=suite,
            g1=g1,
            g2=g2,
            authority_public_keys=tuple(
                sorted(authority_public_keys, key=lambda pair: pair[0])
            ),
        )

    @property
    def authority_count(self) -> int:
        """N_AA — the number of participating Attribute Authorities."""
        return len(self.authority_public_keys)

    def public_key(self, authority_id: str) -> AuthorityPublicKey:
        for candidate_id, public_key in self.authority_public_keys:
            if candidate_id == authority_id:
                return public_key
        raise KeyError(f"no public key in PP for authority {authority_id!r}")

    def _encoded_fields(self) -> Tuple[Any, ...]:
        return (
            self.suite,
            self.g1,
            self.g2,
            [list(pair) for pair in self.authority_public_keys],
        )


# ===========================================================================
# Phase II — Multi-Authority Registration and Authorization-State Commitment
# ===========================================================================
@dataclass(frozen=True)
class AuthorityRegistration(Record):
    """Reg_i = (ID_i, Dom_i, PK_i) — Phase II Step 1."""

    DOMAIN: ClassVar[bytes] = b"authority-registration/v1"

    authority_id: str
    domain: str
    public_key: AuthorityPublicKey

    def __post_init__(self) -> None:
        _check_identifier("authority_id", self.authority_id)
        _check_identifier("domain", self.domain)

    def _encoded_fields(self) -> Tuple[Any, ...]:
        return (self.authority_id, self.domain, self.public_key)


@dataclass(frozen=True)
class AuthorityState(Record):
    """State_i = (ID_i, PK_i, C_i^auth, VID_i) — Phase II Step 4.

    ``commitment`` is C_i^auth as computed in Phase II Step 3. This record
    carries it; it does not compute it (that is ``authority/authority.py``, so
    that the commitment rule lives in one place and this layer cannot grow a
    second, divergent copy of it).
    """

    DOMAIN: ClassVar[bytes] = b"authority-state/v1"

    authority_id: str
    public_key: AuthorityPublicKey
    commitment: bytes
    vid: int

    def __post_init__(self) -> None:
        _check_identifier("authority_id", self.authority_id)
        _check_digest("commitment", self.commitment)
        _check_vid(self.vid)

    def _encoded_fields(self) -> Tuple[Any, ...]:
        return (self.authority_id, self.public_key, self.commitment, self.vid)


@dataclass(frozen=True)
class AuthorizationMeta(Record):
    """Meta_i = (Dom_i, VID_i, C_i^auth) — Phase II Step 4.

    What the AIM synchronises to the Fog Search Nodes. Strictly smaller than
    State_i: the FSNs receive the domain, version, and commitment, not the
    authority public key, which is the point of the AIM sitting between them.
    """

    DOMAIN: ClassVar[bytes] = b"authorization-meta/v1"

    domain: str
    vid: int
    commitment: bytes

    def __post_init__(self) -> None:
        _check_identifier("domain", self.domain)
        _check_vid(self.vid)
        _check_digest("commitment", self.commitment)

    def _encoded_fields(self) -> Tuple[Any, ...]:
        return (self.domain, self.vid, self.commitment)


# ===========================================================================
# Phase III — User Registration and Version-Bound Authorization Profile
# ===========================================================================
@dataclass(frozen=True)
class UserRequest(Record):
    """``Req_U = (UID, Dom, Role, Cred)`` — Phase III Step 1.

    ``kem_encapsulation_key`` is ``pk_U^KEM``, which the manuscript introduces in
    Step 3 without saying who generates it or how it reaches the authority. It is
    carried here, inside the authenticated request, for a security reason rather
    than convenience: Step 3 encrypts ``SK_{U,i}`` to whatever ``pk_U^KEM`` the
    authority holds, so an unbound KEM key could be substituted by an adversary
    who would then receive the user's attribute keys. Binding it into ``Req_U``
    makes the substitution visible. The user is also the only party that can hold
    the matching decapsulation key, so the user is the only party that can
    generate it.
    """

    DOMAIN: ClassVar[bytes] = b"user-request/v1"

    uid: str
    domain: str
    role: str
    credential: bytes
    kem_encapsulation_key: bytes

    def __post_init__(self) -> None:
        _check_identifier("uid", self.uid)
        _check_identifier("domain", self.domain)
        _check_identifier("role", self.role)
        for name in ("credential", "kem_encapsulation_key"):
            value = getattr(self, name)
            if not isinstance(value, (bytes, bytearray)):
                raise TypeError(f"{name} must be bytes")
            if not value:
                raise ValueError(f"{name} must not be empty")

    def _encoded_fields(self) -> Tuple[Any, ...]:
        return (
            self.uid,
            self.domain,
            self.role,
            self.credential,
            self.kem_encapsulation_key,
        )


@dataclass(frozen=True)
class AttributeKeyShare:
    """``SK_{U,i} <- KeyGen(MSK_i, S_{U,i})`` — Phase III Step 2. NEVER published.

    Deliberately NOT a :class:`Record`, on the same grounds as
    :class:`AuthorityMasterKey`: this is user secret key material, so it must not
    be reachable by anything that publishes. ``encode`` and ``digest`` raise.

    Sealing it for transport is the one legitimate serialisation, and it goes
    through :meth:`to_sealed_bytes`, whose name says what it is for. Phase III
    Step 3 is the only caller.

    ``key_material`` is **opaque** here. The manuscript gives ``KeyGen`` as an
    interface and never defines the structure of ``SK_{U,i}``
    (Phase III open decision 1), so this layer carries whatever
    the ABE construction produces without asserting a shape it has no basis for.
    """

    authority_id: str
    uid: str
    attributes: Tuple[str, ...]
    key_material: bytes

    def __post_init__(self) -> None:
        _check_identifier("authority_id", self.authority_id)
        _check_identifier("uid", self.uid)
        if not self.attributes:
            raise ValueError("an attribute key share must cover at least one attribute")
        if len(set(self.attributes)) != len(self.attributes):
            raise ValueError("duplicate attribute in the key share")
        if not isinstance(self.key_material, (bytes, bytearray)):
            raise TypeError("key_material must be bytes")
        if not self.key_material:
            raise ValueError("key_material must not be empty")

    def encode(self) -> bytes:  # pragma: no cover - exists to raise
        raise SecretMaterialError(
            "SK_{U,i} is user secret key material and has no published encoding; "
            "use to_sealed_bytes() and seal it under a per-delivery key"
        )

    def digest(self) -> bytes:  # pragma: no cover - exists to raise
        raise SecretMaterialError("refusing to digest user secret key material")

    def to_sealed_bytes(self) -> bytes:
        """Canonical bytes for AEAD sealing in Phase III Step 3 ONLY.

        Not a publication path: the result is only ever passed to
        ``AES-256-GCM.Enc`` under a key derived from a per-user ML-KEM shared
        secret. The authority and attribute set travel with the key material so
        the recipient can verify it received the share it expected.
        """
        return canonical(
            [self.authority_id, self.uid, sorted(self.attributes), self.key_material]
        )

    @classmethod
    def from_sealed_bytes(cls, raw: bytes) -> "AttributeKeyShare":
        """Inverse of :meth:`to_sealed_bytes`, for the receiving user."""
        authority_id, uid, attributes, key_material = _decode_sealed_share(raw)
        return cls(
            authority_id=authority_id,
            uid=uid,
            attributes=tuple(attributes),
            key_material=key_material,
        )

    def __repr__(self) -> str:
        return (
            f"AttributeKeyShare(authority_id={self.authority_id!r}, "
            f"uid={self.uid!r}, |S_U,i|={len(self.attributes)}, "
            f"key_material=<redacted>)"
        )

    __str__ = __repr__


@dataclass(frozen=True)
class EncryptedKeyDelivery(Record):
    """Phase III Step 3 output: ``(ct_i, EncKey_i)`` bound to its recipient.

    A :class:`Record`, unlike the share it carries: this is ciphertext, so it can
    be transported and audited. ``uid``, ``authority_id`` and ``vid`` are the
    values bound into the HKDF ``info`` and the AEAD associated data, carried
    alongside so the recipient can derive the same key and so a delivery cannot
    be replayed to a different user or across a version change.
    """

    DOMAIN: ClassVar[bytes] = b"key-delivery/v1"

    authority_id: str
    uid: str
    vid: int
    kem_ciphertext: bytes
    sealed_key: bytes

    def __post_init__(self) -> None:
        _check_identifier("authority_id", self.authority_id)
        _check_identifier("uid", self.uid)
        _check_vid(self.vid)
        for name in ("kem_ciphertext", "sealed_key"):
            value = getattr(self, name)
            if not isinstance(value, (bytes, bytearray)):
                raise TypeError(f"{name} must be bytes")
            if not value:
                raise ValueError(f"{name} must not be empty")

    def _encoded_fields(self) -> Tuple[Any, ...]:
        return (
            self.authority_id,
            self.uid,
            self.vid,
            self.kem_ciphertext,
            self.sealed_key,
        )

    @property
    def size_bytes(self) -> int:
        return len(self.kem_ciphertext) + len(self.sealed_key)


@dataclass(frozen=True)
class VersionBoundAuthorizationProfile(Record):
    """``VAP_U = (UID, D_U, AuthRoot_U, VID_U, C_U)`` — Phase III Step 4.

    Authorization metadata, not key material: the AIM maintains it, and the AIM
    is not trusted with keys. ``domains`` and ``commitments`` are stored in
    canonical sorted order so the profile's digest does not depend on the order
    the user's authorities were enumerated in.
    """

    DOMAIN: ClassVar[bytes] = b"vap/v1"

    uid: str
    domains: Tuple[str, ...]
    auth_root: bytes
    vid: int
    commitments: Tuple[bytes, ...]

    def __post_init__(self) -> None:
        _check_identifier("uid", self.uid)
        _check_digest("auth_root", self.auth_root)
        _check_vid(self.vid)
        if not self.domains:
            raise ValueError("D_U must name at least one authorized domain")
        if list(self.domains) != sorted(self.domains):
            raise ValueError(
                "domains must be sorted so the VAP digest is order-independent; "
                "use profile.build_profile()"
            )
        if len(set(self.domains)) != len(self.domains):
            raise ValueError("duplicate domain in D_U")
        if not self.commitments:
            raise ValueError("C_U must contain at least one authority commitment")
        if list(self.commitments) != sorted(self.commitments):
            raise ValueError(
                "commitments must be sorted so H(C_U) is order-independent; "
                "use profile.build_profile()"
            )
        for commitment in self.commitments:
            _check_digest("commitment", commitment)

    def _encoded_fields(self) -> Tuple[Any, ...]:
        return (
            self.uid,
            list(self.domains),
            self.auth_root,
            self.vid,
            list(self.commitments),
        )

    @property
    def authority_count(self) -> int:
        """``N_U`` — the number of participating Attribute Authorities."""
        return len(self.commitments)


# ===========================================================================
# Phase IV — Policy-Bound Dynamic Search Index
# ===========================================================================
@dataclass(frozen=True)
class RecordMetadata(Record):
    """``Meta_i = (PID_i, VID_i, Dom_i, TS_i)`` — Phase IV Step 1.

    ``policy_id`` is the manuscript's ``PID_i``, the **access-policy
    identifier** (notation table, Phase IV Step 1). It is *not*
    the corpus's ``Record.pid``, which ``prepare_dataset.py:346`` fills with a
    patient pseudonym — see ``index/extract.py`` on why conflating the two would
    silently create one policy per patient.
    """

    DOMAIN: ClassVar[bytes] = b"record-metadata/v1"

    policy_id: str
    vid: int
    domain: str
    timestamp: str

    def __post_init__(self) -> None:
        _check_identifier("policy_id", self.policy_id)
        _check_vid(self.vid)
        _check_identifier("domain", self.domain)
        _check_identifier("timestamp", self.timestamp)

    def _encoded_fields(self) -> Tuple[Any, ...]:
        return (self.policy_id, self.vid, self.domain, self.timestamp)


@dataclass(frozen=True)
class IndexEntry(Record):
    """``I_j = (T_j, CID_i, PID_i, VID_i)`` — Phase IV Step 3.

    The Merkle leaf of Phase IV Step 4 is ``L_j = H(I_j)`` over this record's
    canonical encoding, so the entry is exactly what the integrity commitment
    covers: changing any field — including the policy or version carried as
    payload — changes ``Root_i``.
    """

    DOMAIN: ClassVar[bytes] = b"index-entry/v1"

    token: bytes
    cid: str
    policy_id: str
    vid: int

    def __post_init__(self) -> None:
        if not isinstance(self.token, (bytes, bytearray)):
            raise TypeError("token must be bytes")
        if not self.token:
            raise ValueError("token must not be empty")
        _check_identifier("cid", self.cid)
        _check_identifier("policy_id", self.policy_id)
        _check_vid(self.vid)

    def _encoded_fields(self) -> Tuple[Any, ...]:
        return (self.token, self.cid, self.policy_id, self.vid)

    def with_policy(self, *, policy_id: str, vid: int) -> "IndexEntry":
        """A copy under a new policy binding, with the token unchanged.

        Phase IV Step 3: entries are "independently updateable, allowing
        insertions, deletions, and policy modifications without rebuilding the
        entire searchable index". Under the recommended matching option the
        token does not encode the policy, so a policy change rewrites payload
        only — which is what keeps Exp. 5 incremental.
        """
        return IndexEntry(
            token=self.token, cid=self.cid, policy_id=policy_id, vid=vid
        )


# ===========================================================================
# Phase V — Secure Data Outsourcing
# ===========================================================================
@dataclass(frozen=True)
class SyncPayload(Record):
    """``Sync_i = (I_i, PID_i, VID_i, CID_i)`` — Phase V Step 4.

    ``I_i`` is the *record's* entry set — note the index shift from Phase IV's
    ``I_j``, a single entry, to ``I_i``, all of one record's entries.

    **Carries no ciphertext, deliberately.** Step 4 gives each Fog Search Node
    "only searchable-index shards and corresponding metadata"; the entries
    reference IPFS by ``CID_i`` and ``CT_i`` stays there. A payload carrying data
    would turn every node into a replica of the encrypted store, which is exactly
    what Step 4's low-overhead claim rests on not happening.
    """

    DOMAIN: ClassVar[bytes] = b"sync-payload/v1"

    entries: Tuple["IndexEntry", ...]
    policy_id: str
    vid: int
    cid: str

    def __post_init__(self) -> None:
        if not self.entries:
            raise ValueError(
                "Sync_i must carry at least one index entry; the corpus "
                "guarantees |W_i| >= 5"
            )
        _check_identifier("policy_id", self.policy_id)
        _check_identifier("cid", self.cid)
        _check_vid(self.vid)
        # The payload binds ONE (PID_i, VID_i) pair, exactly as Commit_i does. An
        # entry under a different pair would be synchronised under a policy it
        # does not belong to, and the node's bitmap would authorize it wrongly.
        mismatched = [
            entry
            for entry in self.entries
            if entry.policy_id != self.policy_id or entry.vid != self.vid
        ]
        if mismatched:
            raise ValueError(
                f"{len(mismatched)} of {len(self.entries)} entries carry a "
                f"policy/version other than ({self.policy_id!r}, {self.vid}); "
                f"Sync_i binds a single pair"
            )
        if any(entry.cid != self.cid for entry in self.entries):
            raise ValueError(
                f"every entry in Sync_i must reference CID {self.cid!r}; a "
                f"payload spanning records would be applied atomically to one "
                f"shard when its records may belong to different domains"
            )

    def _encoded_fields(self) -> Tuple[Any, ...]:
        return (list(self.entries), self.policy_id, self.vid, self.cid)

    @property
    def entry_count(self) -> int:
        """``|I_i|`` — equals ``|W_i|`` for a freshly indexed record."""
        return len(self.entries)

    @property
    def size_bytes(self) -> int:
        """On-wire size of the synchronisation message.

        The Phase V counterpart of Exp. 6's DIAS message size, and the figure that
        backs Step 4's "low synchronization overhead" claim.
        """
        return len(self.encode())


@dataclass(frozen=True)
class CatalogEntry(Record):
    """``(CID_i, PID_i, VID_i)`` — one row of Phase V Step 5's catalog.

    ``domain`` is carried alongside the published triple because locating a
    *shard* requires knowing which node holds it, and shards are domain-keyed —
    the triple alone identifies the record but not its location.
    """

    DOMAIN: ClassVar[bytes] = b"catalog-entry/v1"

    cid: str
    policy_id: str
    vid: int
    domain: str

    def __post_init__(self) -> None:
        _check_identifier("cid", self.cid)
        _check_identifier("policy_id", self.policy_id)
        _check_identifier("domain", self.domain)
        _check_vid(self.vid)

    def _encoded_fields(self) -> Tuple[Any, ...]:
        return (self.cid, self.policy_id, self.vid, self.domain)


@dataclass(frozen=True)
class OutsourcedMetadata(Record):
    """``Meta_i = (CID_i, PID_i, VID_i, Root_i, Commit_i)`` — Phase V Step 2.

    **Third distinct record the manuscript calls ``Meta_i``**, alongside
    :class:`AuthorizationMeta` (Phase II Step 4) and
    :class:`RecordMetadata` (Phase IV Step 1). One symbol, three tuples;
    they are separate types here because they carry different fields, are produced
    by different parties, and travel to different places.

    "The metadata are maintained by the cloud--fog infrastructure and synchronized
    with the consortium blockchain" — so this is the off-chain register, and
    :class:`BlockchainAnchor` is what actually goes on-chain.
    """

    DOMAIN: ClassVar[bytes] = b"outsourced-metadata/v1"

    cid: str
    policy_id: str
    vid: int
    root: bytes
    commit: bytes

    def __post_init__(self) -> None:
        _check_identifier("cid", self.cid)
        _check_identifier("policy_id", self.policy_id)
        _check_vid(self.vid)
        _check_digest("root", self.root)
        _check_digest("commit", self.commit)

    def _encoded_fields(self) -> Tuple[Any, ...]:
        return (self.cid, self.policy_id, self.vid, self.root, self.commit)


@dataclass(frozen=True)
class BlockchainAnchor(Record):
    """``BC_i = (CID_i, Commit_i, Root_i, VID_i, TS_i)`` — the on-chain transaction.

    One record for two steps that publish the same tuple: Phase V Step 3
    anchors a record's initial state, and Phase VII Step 5 anchors each
    update as ``BC_i'``. Phase VIII Step 2 verifies against whichever is
    current. Three phases share it, which is why it lives here rather than in any
    one of them.

    "Since only compact metadata are recorded, the blockchain storage complexity
    remains independent of the encrypted IoMT data size" — no ciphertext, and no
    index entries either: five fields, two of them digests.
    """

    DOMAIN: ClassVar[bytes] = b"blockchain-anchor/v1"

    cid: str
    commit: bytes
    root: bytes
    vid: int
    timestamp_ns: int

    def __post_init__(self) -> None:
        _check_identifier("cid", self.cid)
        _check_digest("commit", self.commit)
        _check_digest("root", self.root)
        _check_vid(self.vid)
        if self.timestamp_ns < 0:
            raise ValueError(f"timestamp_ns must be non-negative, got {self.timestamp_ns}")

    def _encoded_fields(self) -> Tuple[Any, ...]:
        return (self.cid, self.commit, self.root, self.vid, self.timestamp_ns)


@dataclass(frozen=True)
class SearchToken(Record):
    """``ST = (T_Q, AuthRoot_U, VID_U, rho)`` — Phase VI Step 1.

    ``T_Q`` is the keyword-token set from ``index/tokens.py``; ``rho`` is "a fresh
    random nonce preventing replay attacks", which only prevents anything if a
    verifier tracks it — see ``aim/verification.py``.

    Note on the published ``T_Q = {H(w_i || VID_U)}``: Option D makes the token
    ``H(w)`` alone. Nothing is lost, because ``ST`` **already transmits ``VID_U``
    in the clear** as its own field — hashing it into every token gave the server
    no information it did not already have, while making one trapdoor unusable
    across domains whose versions differ.
    """

    DOMAIN: ClassVar[bytes] = b"search-token/v1"

    tokens: Tuple[bytes, ...]
    auth_root: bytes
    vid_u: int
    nonce: bytes

    def __post_init__(self) -> None:
        if not self.tokens:
            raise ValueError("T_Q must contain at least one keyword token")
        if len(set(self.tokens)) != len(self.tokens):
            raise ValueError("duplicate token in T_Q")
        _check_digest("auth_root", self.auth_root)
        _check_vid(self.vid_u)
        if len(self.nonce) < 16:
            raise ValueError(
                f"rho must be at least 16 bytes to prevent replay, got "
                f"{len(self.nonce)}"
            )

    def _encoded_fields(self) -> Tuple[Any, ...]:
        return (list(self.tokens), self.auth_root, self.vid_u, self.nonce)

    @property
    def keyword_count(self) -> int:
        """``q`` — the Exp. 1 sweep variable."""
        return len(self.tokens)

    @property
    def size_bytes(self) -> int:
        """Exp. 1 secondary metric: trapdoor size, in bytes."""
        return len(self.encode())


def _decode_sealed_share(raw: bytes) -> Tuple[str, str, list, bytes]:
    """Minimal decoder for :meth:`AttributeKeyShare.to_sealed_bytes`.

    Deliberately narrow: it reverses exactly the one encoding it has to, rather
    than being a general canonical decoder. A general decoder would be a second
    parser for the format every commitment in the scheme depends on, and the
    encoder is the only side that needs to be trusted for those.
    """
    view = memoryview(raw)
    offset = 0

    def read_tag(expected: bytes) -> None:
        nonlocal offset
        tag = bytes(view[offset : offset + 1])
        if tag != expected:
            raise EncodingError(
                f"sealed share: expected tag {expected.hex()} at offset {offset}, "
                f"got {tag.hex()}"
            )
        offset += 1

    def read_length() -> int:
        nonlocal offset
        length = int.from_bytes(view[offset : offset + 4], "big")
        offset += 4
        return length

    def read_str() -> str:
        read_tag(_T_STR)
        length = read_length()
        nonlocal offset
        value = bytes(view[offset : offset + length]).decode("utf-8")
        offset += length
        return value

    def read_bytes() -> bytes:
        read_tag(_T_BYTES)
        length = read_length()
        nonlocal offset
        value = bytes(view[offset : offset + length])
        offset += length
        return value

    read_tag(_T_SEQ)
    count = read_length()
    if count != 4:
        raise EncodingError(f"sealed share: expected 4 fields, got {count}")
    authority_id = read_str()
    uid = read_str()
    read_tag(_T_SEQ)
    attribute_count = read_length()
    attributes = [read_str() for _ in range(attribute_count)]
    key_material = read_bytes()
    if offset != len(raw):
        raise EncodingError(
            f"sealed share: {len(raw) - offset} trailing bytes after decoding"
        )
    return authority_id, uid, attributes, key_material


__all__ = [
    "DIGEST_BYTES",
    "EncodingError",
    "SecretMaterialError",
    "canonical",
    "digest",
    "Record",
    "PrimitiveSuite",
    "AuthorityPublicKey",
    "AuthorityMasterKey",
    "PublicParameters",
    "AuthorityRegistration",
    "AuthorityState",
    "AuthorizationMeta",
    "UserRequest",
    "AttributeKeyShare",
    "EncryptedKeyDelivery",
    "VersionBoundAuthorizationProfile",
    "RecordMetadata",
    "IndexEntry",
    "SyncPayload",
    "CatalogEntry",
    "OutsourcedMetadata",
    "BlockchainAnchor",
    "SearchToken",
]
