"""``Ledger`` backed by Hyperledger Fabric v2.5 — the adapter the manuscript (SVI) assumed.

Why this exists
---------------
the manuscript (SVI) says the ledger is Hyperledger Fabric v2.5 and global.yaml puts "chain
consistency" inside Exp. 4's measurement boundary. The harness ran
``InProcessLedger``, an in-memory dict, so Exp. 4's chain check cost a hash-table
lookup instead of a network round trip and ``provenance.reportability()`` refused
to mark the experiment reportable. That blocker is what this clears.

Why the Endorser service and not the Gateway
--------------------------------------------
Exp. 4's timed path reads an anchor back, once per returned record. That is a
QUERY: no ordering, no commit, no orderer involvement. ``Endorser.ProcessProposal``
is exactly that call and goes straight to the peer, so what gets timed is one
gRPC round trip and the peer's LevelDB read — the thing the paper claims to
measure. Routing it through the Gateway would add a hop that is Fabric's
convenience layer, not the scheme's cost.

Writes go through the same proposal path plus an orderer broadcast, but every
write here happens in untimed setup, so its latency does not reach a figure.

Why the hash chain stays in Python
----------------------------------
``verify_chain()`` must recompute the same links whichever backend produced the
entries, so ``_entry_hash`` stays the single definition and the chaincode stores
its output as opaque hex. A second implementation of that digest in Go could
drift, and a drifted digest reads as tampering rather than as a bug.

The canonical decoder ``chain/ledger.py``'s ``LedgerEntry`` docstring asks for is
:func:`_decode_entry`: Fabric returns bytes this process did not write, so the
typed ``record`` is left unset and ``payload`` is what callers verify against.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .ledger import (
    ChainIntegrityError,
    ImmutabilityError,
    Ledger,
    LedgerEntry,
    NotFoundError,
    _GENESIS,
    _entry_hash,
)

#: Where network.sh writes the compiled Fabric protobufs.
_PROTO_DIR = Path(__file__).resolve().parents[4] / "infra" / "fabric" / "pyproto"


class FabricUnavailable(RuntimeError):
    """Raised when the network or its generated bindings are not present.

    Deliberately not a silent fallback to ``InProcessLedger``: a run that
    believes it is talking to Fabric and is not would stamp
    ``reportable: true`` on a number measured against a dict, which is the exact
    misrepresentation this adapter exists to remove.
    """


def _load_protos():
    if str(_PROTO_DIR) not in sys.path:
        sys.path.insert(0, str(_PROTO_DIR))
    try:
        import grpc  # noqa: F401
        from common import common_pb2
        from msp import identities_pb2
        from peer import (chaincode_pb2, proposal_pb2, peer_pb2_grpc,
                          transaction_pb2, proposal_response_pb2)
        from orderer import ab_pb2_grpc
    except ImportError as exc:  # noqa: BLE001
        raise FabricUnavailable(
            f"Fabric bindings unavailable ({exc}). Run "
            f"./infra/fabric/network.sh up, which compiles them into "
            f"{_PROTO_DIR}."
        ) from exc
    import grpc
    return (grpc, common_pb2, identities_pb2, chaincode_pb2, proposal_pb2,
            peer_pb2_grpc, transaction_pb2, proposal_response_pb2, ab_pb2_grpc)


def _low_s(r: int, s: int) -> tuple:
    """Fabric rejects a signature whose ``s`` is above half the curve order.

    Both ``(r, s)`` and ``(r, n - s)`` verify, so an unnormalised signature is
    malleable and Fabric's MSP refuses it outright with "signature is not
    normalized". `cryptography` does not normalise, so this does.
    """
    n = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
    return (r, n - s) if s > n // 2 else (r, s)


class FabricLedger(Ledger):
    """Append-only ledger on a Fabric channel.

    ``endpoint`` is the peer's gRPC address; ``msp_dir`` the signing identity's
    MSP directory, as produced by cryptogen.
    """

    def __init__(
        self,
        *,
        endpoint: Optional[str] = None,
        channel: Optional[str] = None,
        chaincode: Optional[str] = None,
        msp_dir: Optional[str] = None,
        msp_id: str = "Org1MSP",
        tls_root_cert: Optional[str] = None,
        timeout: float = 30.0,
        run_tag: Optional[str] = None,
    ) -> None:
        (self._grpc, self._common, self._identities, self._cc, self._proposal,
         self._peer_grpc, self._tx, self._presp, self._ab_grpc) = _load_protos()

        root = Path(__file__).resolve().parents[4] / "infra" / "fabric"
        crypto = root / "crypto-config" / "peerOrganizations" / "org1.abcd.local"
        self.endpoint = endpoint or os.environ.get("ABCD_FABRIC_PEER", "localhost:7051")
        self.channel = channel or os.environ.get("ABCD_CHANNEL", "abcd")
        self.chaincode = chaincode or os.environ.get("ABCD_CC_NAME", "abcdledger")
        self.msp_id = msp_id
        self.timeout = timeout
        msp = Path(msp_dir) if msp_dir else (
            crypto / "users" / "Admin@org1.abcd.local" / "msp"
        )
        tls_ca = Path(tls_root_cert) if tls_root_cert else (
            crypto / "peers" / "peer0.org1.abcd.local" / "tls" / "ca.crt"
        )
        # Fabric is PERSISTENT, unlike the dict it replaces. Every sweep point
        # calls prepare() and re-anchors the same record ids, so without
        # isolation the second point dies on ImmutabilityError against keys the
        # first one committed -- correct behaviour from the chaincode, and fatal
        # to a sweep. Each ledger instance therefore writes under its own
        # namespace prefix. Immutability still holds where it means something,
        # within a run, and cross-run collisions become impossible instead of
        # requiring the channel to be torn down between points.
        self.run_tag = run_tag or os.environ.get(
            "ABCD_RUN_TAG", base64.b16encode(os.urandom(6)).decode().lower()
        )
        # Typed records this INSTANCE wrote, by (namespace, key).
        #
        # chain/ledger.py's typed accessors (get_registration, get_state, ...)
        # require entry.record to be the right Record subclass, and Record has
        # encode() but no decode(): reconstructing one from canonical bytes
        # needs a per-type decoder that does not exist. That is precisely the
        # gap ledger.py:105 names as "the one piece of work the interface swap
        # still requires", and it is NOT closed here.
        #
        # What this cache does instead is remember the object on the way in, so
        # a deployment can read back what it just anchored. It does NOT make the
        # read cheaper or fake it: get() still performs the full Fabric round
        # trip and still pays for it, and the cache only supplies the typed view
        # of bytes Fabric returned. An entry written by a DIFFERENT process
        # still comes back with record=None, and a typed accessor on it still
        # raises -- honestly, rather than by guessing a subclass.
        self._typed: Dict[tuple, Any] = {}
        self._identity, self._key = self._load_identity(msp)
        self._stub = self._connect(tls_ca)

    def _ns(self, namespace: str) -> str:
        return f"{self.run_tag}:{namespace}"

    # -- identity and transport ---------------------------------------------
    def _load_identity(self, msp: Path):
        from cryptography.hazmat.primitives import serialization

        try:
            cert = next((msp / "signcerts").glob("*.pem")).read_bytes()
            key_pem = next((msp / "keystore").glob("*")).read_bytes()
        except (StopIteration, FileNotFoundError) as exc:
            raise FabricUnavailable(
                f"no MSP material under {msp}; ./infra/fabric/network.sh up "
                f"generates it with cryptogen"
            ) from exc
        key = serialization.load_pem_private_key(key_pem, password=None)
        identity = self._identities.SerializedIdentity(
            mspid=self.msp_id, id_bytes=cert
        )
        return identity, key

    def _connect(self, tls_ca: Path):
        if tls_ca.is_file():
            creds = self._grpc.ssl_channel_credentials(tls_ca.read_bytes())
            # The peer's certificate is issued to `peer`, not to the address the
            # client dials, so the authority override is required rather than
            # cosmetic; without it every RPC fails the hostname check.
            channel = self._grpc.secure_channel(
                self.endpoint, creds,
                options=[("grpc.ssl_target_name_override", "peer")],
            )
        else:
            channel = self._grpc.insecure_channel(self.endpoint)
        return self._peer_grpc.EndorserStub(channel)

    # -- proposal construction ----------------------------------------------
    def _signed_proposal(self, function: str, args: List[str]):
        creator = self._identity.SerializeToString()
        nonce = os.urandom(24)
        tx_id = hashlib.sha256(nonce + creator).hexdigest()

        spec = self._cc.ChaincodeSpec(
            type=self._cc.ChaincodeSpec.GOLANG,
            chaincode_id=self._cc.ChaincodeID(name=self.chaincode),
            input=self._cc.ChaincodeInput(
                args=[function.encode()] + [a.encode() for a in args]
            ),
        )
        payload = self._proposal.ChaincodeProposalPayload(
            input=self._cc.ChaincodeInvocationSpec(
                chaincode_spec=spec
            ).SerializeToString()
        )
        extension = self._proposal.ChaincodeHeaderExtension(
            chaincode_id=self._cc.ChaincodeID(name=self.chaincode)
        )
        now = time.time()
        timestamp = self._common.google_dot_protobuf_dot_timestamp__pb2.Timestamp() \
            if hasattr(self._common, "google_dot_protobuf_dot_timestamp__pb2") else None
        if timestamp is None:
            from google.protobuf.timestamp_pb2 import Timestamp
            timestamp = Timestamp()
        timestamp.seconds = int(now)
        timestamp.nanos = int((now - int(now)) * 1e9)

        channel_header = self._common.ChannelHeader(
            type=self._common.ENDORSER_TRANSACTION,
            version=1,
            timestamp=timestamp,
            channel_id=self.channel,
            tx_id=tx_id,
            epoch=0,
            extension=extension.SerializeToString(),
        )
        signature_header = self._common.SignatureHeader(
            creator=creator, nonce=nonce
        )
        header = self._common.Header(
            channel_header=channel_header.SerializeToString(),
            signature_header=signature_header.SerializeToString(),
        )
        proposal = self._proposal.Proposal(
            header=header.SerializeToString(),
            payload=payload.SerializeToString(),
        )
        proposal_bytes = proposal.SerializeToString()
        signed = self._proposal.SignedProposal(
            proposal_bytes=proposal_bytes,
            signature=self._sign(proposal_bytes),
        )
        # The envelope must carry the SAME header and proposal payload the peer
        # endorsed, or the orderer's validation rejects it as a different
        # transaction, so they travel back with the signed proposal.
        return signed, header, payload

    def _sign(self, message: bytes) -> bytes:
        from cryptography.hazmat.primitives import hashes as c_hashes
        from cryptography.hazmat.primitives.asymmetric import ec, utils

        digest = hashlib.sha256(message).digest()
        raw = self._key.sign(digest, ec.ECDSA(utils.Prehashed(c_hashes.SHA256())))
        r, s = utils.decode_dss_signature(raw)
        return utils.encode_dss_signature(*_low_s(r, s))

    def _call(self, function: str, args: List[str]) -> str:
        """One Endorser round trip. THIS is what Exp. 4 times, once per record."""
        signed, _, _ = self._signed_proposal(function, args)
        response = self._stub.ProcessProposal(signed, timeout=self.timeout)
        status = response.response.status
        if status != 200:
            message = response.response.message or ""
            if "IMMUTABLE" in message:
                raise ImmutabilityError(message)
            if "NOTFOUND" in message:
                raise NotFoundError(message)
            raise RuntimeError(f"fabric status {status}: {message}")
        return response.response.payload.decode()

    def _submit(self, function: str, args: List[str]) -> str:
        """Endorse, then order and commit. Used by writes only.

        ProcessProposal alone does NOT write anything: it endorses a proposal
        and returns the simulated result. Without the broadcast below, append()
        looked like it worked and committed nothing, and the first read after it
        failed with NotFoundError.
        """
        signed, header, prop_payload = self._signed_proposal(function, args)
        response = self._stub.ProcessProposal(signed, timeout=self.timeout)
        if response.response.status != 200:
            message = response.response.message or ""
            if "IMMUTABLE" in message:
                raise ImmutabilityError(message)
            if "NOTFOUND" in message:
                raise NotFoundError(message)
            raise RuntimeError(f"fabric status {response.response.status}: {message}")

        endorsed = self._tx.ChaincodeEndorsedAction(
            proposal_response_payload=response.payload,
            endorsements=[response.endorsement],
        )
        action_payload = self._tx.ChaincodeActionPayload(
            chaincode_proposal_payload=prop_payload.SerializeToString(),
            action=endorsed,
        )
        transaction = self._tx.Transaction(actions=[
            self._tx.TransactionAction(
                header=header.signature_header,
                payload=action_payload.SerializeToString(),
            )
        ])
        envelope_payload = self._common.Payload(
            header=header, data=transaction.SerializeToString()
        ).SerializeToString()
        envelope = self._common.Envelope(
            payload=envelope_payload, signature=self._sign(envelope_payload)
        )
        ack = self._orderer().Broadcast(iter([envelope]), timeout=self.timeout)
        for reply in ack:
            if reply.status != self._common.SUCCESS:
                raise RuntimeError(f"orderer rejected the transaction: {reply.status}")
            break
        return response.response.payload.decode()

    def _orderer(self):
        if getattr(self, "_orderer_stub", None) is None:
            root = Path(__file__).resolve().parents[4] / "infra" / "fabric"
            ca = (root / "crypto-config" / "ordererOrganizations" / "abcd.local"
                  / "orderers" / "orderer.abcd.local" / "tls" / "ca.crt")
            endpoint = os.environ.get("ABCD_FABRIC_ORDERER", "localhost:7050")
            creds = self._grpc.ssl_channel_credentials(ca.read_bytes())
            channel = self._grpc.secure_channel(
                endpoint, creds,
                options=[("grpc.ssl_target_name_override", "orderer")],
            )
            self._orderer_stub = self._ab_grpc.AtomicBroadcastStub(channel)
        return self._orderer_stub

    def _await_commit(self, namespace: str, key: str, deadline: float = 20.0) -> None:
        """Block until the write is readable.

        Broadcast returns when the orderer ACCEPTS the transaction, not when the
        peer has committed it, and BatchTimeout is 1s. Reading straight after a
        write therefore races the block cut. Polling the value is cruder than
        subscribing to the commit event but needs no second connection, and this
        runs only in untimed setup.
        """
        started = time.monotonic()
        while time.monotonic() - started < deadline:
            try:
                self._call("Get", [self._ns(namespace), key])
                return
            except NotFoundError:
                time.sleep(0.05)
        raise RuntimeError(
            f"{namespace}/{key} did not commit within {deadline}s; the orderer "
            f"accepted it but the peer never delivered the block"
        )

    # -- storage primitives -------------------------------------------------
    def initialize(self) -> None:
        """No-op: the channel and chaincode are created by network.sh.

        Namespaces are implicit in the composite key, so there is nothing to
        create per namespace, and a chaincode call here would put an untimed
        round trip in every construction.
        """
        return None

    def append(self, namespace: str, key: str, record) -> LedgerEntry:
        payload = record.encode()
        domain = getattr(record, "DOMAIN", b"")
        timestamp_ns = time.time_ns()
        previous = self.chain_head()
        sequence = self.entry_count()
        entry_hash = _entry_hash(
            sequence=sequence,
            namespace=namespace,
            key=key,
            payload=payload,
            domain=domain,
            timestamp_ns=timestamp_ns,
            previous_hash=previous,
        )
        self._submit("Append", [
            self._ns(namespace), key,
            base64.b64encode(payload).decode(),
            base64.b64encode(domain).decode(),
            str(timestamp_ns),
            previous.hex(),
            entry_hash.hex(),
        ])
        self._await_commit(namespace, key)
        self._typed[(namespace, key)] = record
        return LedgerEntry(
            sequence=sequence, namespace=namespace, key=key, payload=payload,
            domain=domain, timestamp_ns=timestamp_ns, previous_hash=previous,
            entry_hash=entry_hash, record=record,
        )

    def get(self, namespace: str, key: str) -> LedgerEntry:
        # The round trip happens first and unconditionally -- this is the call
        # Exp. 4 times, once per returned record.
        entry = _decode_entry(json.loads(self._call("Get", [self._ns(namespace), key])))
        cached = self._typed.get((namespace, key))
        if cached is None:
            return entry
        return dataclasses.replace(entry, record=cached)

    def keys(self, namespace: str, *, prefix: str = "") -> List[str]:
        return sorted(json.loads(self._call("Keys", [self._ns(namespace), prefix])))

    def chain_head(self) -> bytes:
        head = json.loads(self._call("MetaJSON", []))["head"]
        return bytes.fromhex(head) if head else _GENESIS

    def entry_count(self) -> int:
        return int(json.loads(self._call("MetaJSON", []))["count"])

    def verify_chain(self) -> bool:
        entries = [_decode_entry(e) for e in json.loads(self._call("All", []))]
        entries.sort(key=lambda e: e.sequence)
        previous = _GENESIS
        for entry in entries:
            if entry.previous_hash != previous:
                raise ChainIntegrityError(
                    f"entry {entry.sequence} links to "
                    f"{entry.previous_hash.hex()[:12]}, expected "
                    f"{previous.hex()[:12]}"
                )
            if entry.recompute_hash() != entry.entry_hash:
                raise ChainIntegrityError(
                    f"entry {entry.sequence} hash does not recompute"
                )
            previous = entry.entry_hash
        return True


def _decode_entry(blob: Dict[str, Any]) -> LedgerEntry:
    """The canonical decoder ``ledger.py:105`` says a Fabric adapter needs.

    ``record`` stays None: these bytes were written by some other process, and
    reconstructing a typed object would mean guessing which ``Record`` subclass
    produced them. Callers verify against ``payload``, which is what is hashed
    and anchored.
    """
    return LedgerEntry(
        sequence=int(blob["sequence"]),
        namespace=blob["namespace"],
        key=blob["key"],
        payload=base64.b64decode(blob["payload"]),
        domain=base64.b64decode(blob["domain"]),
        timestamp_ns=int(blob["timestamp_ns"]),
        previous_hash=bytes.fromhex(blob["previous_hash"]),
        entry_hash=bytes.fromhex(blob["entry_hash"]),
        record=None,
    )


__all__ = ["FabricLedger", "FabricUnavailable"]
