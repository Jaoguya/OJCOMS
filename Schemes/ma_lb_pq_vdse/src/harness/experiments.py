"""The eight experiments of global.yaml, as measurable objects.

Each declares its sweep variable, its primary and secondary metrics, an **untimed**
``prepare`` and a **timed** ``measure``. The split is the measurement boundary: what
``measure`` touches is what the reported number covers, and each class's docstring
quotes the rule it implements.

The boundaries, from ``skill.md``:

* Exp. 1 — online trapdoor generation only; ML-KEM encapsulation excluded.
* Exp. 2 — AIM check → AASS selection → shard search → response assembly; index
  construction offline.
* Exp. 3 — one trapdoor reused across ``d`` domains; count trapdoors issued.
* Exp. 4 — client-side verification only; IPFS fetch and decryption excluded.
* Exp. 5 — incremental update only; a global rebuild is a Phase VII bug.
* Exp. 6 — DIAS end-to-end until every affected FSN reports the new VID.
* Exp. 7–8 — one closed-loop workload per variant, both metric sets from the
  same runs.

**Titles here follow the manuscript; class names and ``name`` slugs do not.**
Section VI renamed four experiments (Exp. 1 "Policy-State-Aware Token
Generation", Exp. 4 "Fine-Grained Verification Effectiveness", Exp. 5 "Dynamic
Index Update", Exp. 6 "DIAS Synchronization Ablation", Exp. 7 "AASS Search
Throughput"). The ``name`` field of each class is the on-disk results directory
that every banked run was written into, so it keeps its original slug --
``exp1_trapdoor_generation`` and the rest -- and the class names track the
slugs rather than the prose. Renaming either would orphan measured data without
changing anything a reader of the paper sees.

**Records come from a source, not from the corpus directly.** The committed
``dataset_manifest.json`` is the superseded v1, so ``load_verified_corpus()``
refuses to load anything; :class:`SyntheticRecordSource` lets the harness be built
and tested now. It reports ``corpus_type = "synthetic"``, which
``provenance.reportability`` rejects — so nothing it produces can be quoted, by
construction rather than by discipline.
"""

from __future__ import annotations

import dataclasses

import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from Common.crypto import hashes  # noqa: E402
from Common.crypto.rng import DeterministicRNG  # noqa: E402
from Common.timing import gc_quiesced  # noqa: E402

from Common.crypto import config as crypto_config  # noqa: E402
from .. import config as scheme_config  # noqa: E402
from .. import types  # noqa: E402
from ..aim import aim as aim_mod  # noqa: E402
from ..aim import verification as authz_mod  # noqa: E402
from ..authority import authority as authority_mod  # noqa: E402
from ..authority import initializer as init_mod  # noqa: E402
from ..chain import ipfs as ipfs_mod  # noqa: E402
from ..chain import ledger as ledger_mod  # noqa: E402
from ..chain import select as chain_select  # noqa: E402
from ..chain import outsourcing as out_mod  # noqa: E402
from ..fsn import fsn as fsn_mod  # noqa: E402
from ..fsn import pool as fsn_pool  # noqa: E402
from ..fsn import search as search_mod  # noqa: E402
from ..index import commit as commit_mod  # noqa: E402
from ..index import extract as extract_mod  # noqa: E402
from ..index import tokens as tokens_mod  # noqa: E402
from ..scheduler import aass as aass_mod  # noqa: E402
from ..shard import propagation as prop_mod  # noqa: E402
from ..sync import dias as dias_mod  # noqa: E402
from ..user import profile as profile_mod  # noqa: E402
from ..user import token as token_mod  # noqa: E402
from ..verify import ledger as vledger_mod  # noqa: E402
from ..verify import proof as proof_mod  # noqa: E402
from .runner import MetricSpec, Sample  # noqa: E402

MS = "ms"
BYTES = "B"
KB = "KB"
QPS = "queries/s"
COUNT = "count"

#: Open decision 3 — the corpus carries no access policy, so the number of
#: policies per domain is a benchmark decision. 2 is the campaign default and
#: sizes the `domain_policy` bitmap set that produces Exp. 2's `n_eff`, so
#: changing it is results-affecting for every experiment that indexes records.
#:
#: It is a PARAMETER rather than a literal because Exp. 6's independent variable
#: is the *fraction* of policies an authority update affects, swept 10%..100%
#: per SVI. At 2 per domain the corpus yields 8 policies at SVI's d=4, and 10%
#: of 8 is 0.8 -- the sweep's lowest point is not representable, which is the
#: sole reason Exp. 6 used to stipulate its policy topology instead of reading
#: the corpus. Exp. 6 now asks for a resolution that makes every step exact and
#: keeps the records, shards and FSNs real; nothing else passes the argument.
DEFAULT_POLICIES_PER_DOMAIN = 2


# ===========================================================================
# Record sources
# ===========================================================================
@dataclass
class SyntheticRecordSource:
    """In-process records with the frozen corpus's shape but none of its content.

    ``corpus_type`` is ``"synthetic"``, which makes every run built on it
    non-reportable — dataset.yaml admits only ``synthea``. This exists so the harness
    can be written and tested while the v2 manifest is missing, not as a substitute
    for the corpus: keyword co-occurrence here is arbitrary, and Exp. 2's ``n_eff``
    depends on exactly that.
    """

    keywords_per_record: int = 6
    domains: Tuple[str, ...] = extract_mod.DEFAULT_DOMAIN_NAMES
    vocabulary: int = 2006
    corpus_type: str = "synthetic"
    corpus_sha256: Optional[str] = None

    def records(
        self,
        count: int,
        *,
        domains: Optional[int] = None,
        policies_per_domain: Optional[int] = None,
    ):
        domain_count = len(self.domains) if domains is None else domains
        assignment = extract_mod.BucketedPolicyAssignment(
            policies_per_domain=(
                DEFAULT_POLICIES_PER_DOMAIN
                if policies_per_domain is None
                else int(policies_per_domain)
            ),
            domains=max(domain_count, 1)
        )
        # Slicing past the end truncates silently, and a short-but-non-empty
        # slice is still truthy — so an `or` fallback never fires and Exp. 3
        # raised ExtractionError at d=6,8,10 against the 4 default names.
        # Generate names whenever the configured list cannot cover domain_count.
        names = tuple(self.domains[:domain_count])
        if len(names) < domain_count:
            names = tuple(f"dom{i}" for i in range(domain_count))
        for rid in range(count):
            dom_index = rid % domain_count
            record = _SyntheticRecord(
                rid=rid,
                pid=f"patient-{rid:06d}",
                vid=1,
                dom=dom_index,
                ts="2026-08-12T00:00:00Z",
                kw=[
                    f"kw:{(rid * self.keywords_per_record + k) % self.vocabulary:05d}"
                    for k in range(self.keywords_per_record)
                ],
            )
            yield extract_mod.extract(
                record, assignment=assignment, domain_names=names
            )


@dataclass
class CorpusRecordSource:
    """Records streamed from the frozen Synthea corpus — the reportable source.

    Interface-compatible with :class:`SyntheticRecordSource` so the experiments
    do not care which one they were given, but with three real differences:

    * ``corpus_type`` is ``"synthea"`` and ``corpus_sha256`` is the verified
      digest, so runs built on it can actually satisfy dataset.yaml/§15.
    * Keyword co-occurrence is the corpus's own. Exp. 2's ``n_eff`` depends on
      exactly that, which is why the synthetic source can never stand in for a
      reportable number.
    * It **streams**. ``load_verified_corpus()`` materialises all 1.14M records
      (~1-2 GB per process); every experiment here needs
      only the first ``count``, so this verifies the digest and then reads
      lazily, taking what it needs and stopping.

    DOMAIN LIMIT — a real constraint, deliberately not papered over. The frozen
    corpus carries exactly ``len(per_domain_counts)`` domains (4: balanced
    whole-organization buckets, dataset.yaml). Exp. 3 sweeps ``d = 2..10``. For
    ``d <= 4`` each record keeps its real ``dom``, which is the point of using
    the corpus at all. For ``d > 4`` the corpus simply has no such partition,
    and inventing one by re-bucketing ``rid`` would silently replace real
    institutional boundaries with a synthetic split *while still reporting
    ``corpus_type: synthea``* — the exact class of misrepresentation skill.md
    forbids. So it raises instead, naming the decision.
    """

    corpus_dir: Optional[Path] = None
    manifest_path: Optional[Path] = None

    corpus_type: str = field(init=False, default="synthea")
    corpus_sha256: Optional[str] = field(init=False, default=None)
    keywords_per_record: int = field(init=False, default=6)
    domains: Tuple[str, ...] = field(init=False, default=extract_mod.DEFAULT_DOMAIN_NAMES)
    vocabulary: int = field(init=False, default=0)

    _corpus_path: Path = field(init=False, repr=False)
    _available_domains: int = field(init=False, repr=False, default=0)

    def __post_init__(self) -> None:
        from Common.crypto.config import REPO_ROOT
        from Dataset.corpus import verify_corpus
        from Common.crypto.config import load_dataset_config

        config = load_dataset_config()
        output_cfg = config["output"]
        corpus_dir = Path(self.corpus_dir) if self.corpus_dir else (
            REPO_ROOT / "Dataset" / "derived"
        )
        self._corpus_path = corpus_dir / output_cfg["corpus_filename"]
        manifest_path = Path(self.manifest_path) if self.manifest_path else (
            REPO_ROOT / "Dataset" / output_cfg["manifest_filename"]
        )

        # Verifies the corpus against its manifest (SHA-256) and refuses on
        # mismatch. The manifest-vs-dataset.yaml freeze pin is a separate,
        # stricter gate; it is applied by provenance.build_metadata(), which is
        # what decides reportability, so a development run can proceed here
        # while a pin mismatch still correctly marks the run non-reportable.
        manifest = verify_corpus(
            self._corpus_path, manifest_path, require_reportable=False
        )
        self.corpus_type = str(manifest.get("corpus_type", "unknown"))
        self.corpus_sha256 = manifest.get("corpus_sha256")
        self.vocabulary = int(manifest.get("keyword_universe_size", 0))

        # Exp. 2 sizes its record count as `index_size // keywords_per_record`,
        # so this must be the corpus's real mean |W_i|, not a nominal constant.
        kpr = manifest.get("keywords_per_record") or {}
        mean = kpr.get("mean")
        self.keywords_per_record = max(1, int(round(float(mean)))) if mean else 6

        per_domain = manifest.get("per_domain_counts") or {}
        self._available_domains = len(per_domain)
        if self._available_domains:
            self.domains = tuple(
                extract_mod.DEFAULT_DOMAIN_NAMES[i]
                if i < len(extract_mod.DEFAULT_DOMAIN_NAMES) else f"dom{i}"
                for i in range(self._available_domains)
            )

    def records(
        self,
        count: int,
        *,
        domains: Optional[int] = None,
        policies_per_domain: Optional[int] = None,
    ):
        from Dataset.corpus import read_corpus

        domain_count = self._available_domains if domains is None else domains
        domain_count = max(int(domain_count), 1)
        if domain_count > self._available_domains:
            raise ValueError(
                f"the frozen corpus has {self._available_domains} domains "
                f"(per_domain_counts in the manifest), but d={domain_count} was "
                f"requested. Splitting it further would replace real "
                f"institutional boundaries with a synthetic partition while "
                f"still reporting corpus_type={self.corpus_type!r}, which "
                f"skill.md forbids. This needs a recorded benchmark decision: "
                f"either cap Exp. 3 at d<={self._available_domains}, or "
                f"regenerate the corpus with more domains and re-freeze "
                f"(results-affecting), or state in §VI that d>"
                f"{self._available_domains} uses a sub-partitioned corpus."
            )

        assignment = extract_mod.BucketedPolicyAssignment(
            policies_per_domain=(
                DEFAULT_POLICIES_PER_DOMAIN
                if policies_per_domain is None
                else int(policies_per_domain)
            ),
            domains=domain_count,
        )
        names = self.domains[:domain_count]

        # Emitted ROUND-ROBIN across domains, not in raw corpus order.
        #
        # Each record keeps its own real `dom` — nothing is remapped — but the
        # ORDER is balanced. Callers that need every domain represented in a
        # small draw depend on this: Exp3CrossDomain.prepare asks for only
        # `d * 4` records and then does `next(r for r in records if
        # r.domain == domain)` for each domain. The synthetic source made that
        # safe implicitly (`dom = rid % domain_count` is round-robin by
        # construction); the corpus's natural order is grouped, so the first 8
        # records can all be domain 0 and that `next()` died with a bare
        # StopIteration — which took out every Exp. 3 point, d=2 included.
        #
        # Record order is not a measured property of any experiment here, so
        # balancing it changes no reported number; it only makes a small draw
        # representative, which is what the callers already assumed.
        from collections import deque

        buckets: Dict[int, Any] = {d: deque() for d in range(domain_count)}
        emitted = 0
        stream = read_corpus(self._corpus_path)
        exhausted = False
        while emitted < count:
            # Top up until every domain can supply one, or the corpus runs out.
            while not exhausted and not all(buckets[d] for d in range(domain_count)):
                try:
                    record = next(stream)
                except StopIteration:
                    exhausted = True
                    break
                if record.dom < domain_count:
                    buckets[record.dom].append(record)
            progressed = False
            for d in range(domain_count):
                if emitted >= count:
                    break
                if buckets[d]:
                    yield extract_mod.extract(
                        buckets[d].popleft(), assignment=assignment,
                        domain_names=names,
                    )
                    emitted += 1
                    progressed = True
            if not progressed:
                break

        if emitted < count:
            raise ValueError(
                f"corpus exhausted after {emitted} records but {count} were "
                f"requested at d={domain_count}; the sweep point does not fit "
                f"this corpus"
            )


@dataclass
class _SyntheticRecord:
    """Mirrors ``Dataset.corpus.Record``'s field names and shape."""

    rid: int
    pid: str
    vid: int
    dom: int
    ts: str
    kw: List[str]


# ===========================================================================
# A deployment the experiments measure against
# ===========================================================================
@dataclass
class Deployment:
    """Phases I-V, assembled. Built in ``prepare``, so never on a measured path."""

    config: scheme_config.Configuration
    scheme: tokens_mod.TokenScheme
    context: init_mod.GlobalContext
    ledger: ledger_mod.InProcessLedger
    aim: aim_mod.AuthorizationIndexManager
    store: ipfs_mod.InProcessContentStore
    register: out_mod.MetadataRegister
    catalog: prop_mod.IndexCatalog
    authorities: Dict[str, authority_mod.Authority]
    nodes: Tuple[fsn_mod.FogSearchNode, ...]
    owner_profile: types.VersionBoundAuthorizationProfile
    records: List[Dict[str, Any]] = field(default_factory=list)
    domains: Tuple[str, ...] = ()

    def node_for(self, domain: str) -> fsn_mod.FogSearchNode:
        return next(n for n in self.nodes if n.serves_domain(domain))


def build_deployment(
    *,
    config: scheme_config.Configuration,
    source: SyntheticRecordSource,
    records: int,
    domains: Optional[int] = None,
    group_provider=None,
    search_key: Optional[bytes] = None,
) -> Deployment:
    """Phases I-V for one sweep point. Untimed by construction.

    Prefers the real Type-III backend (``charm_type3``, added 2026-08-28) and
    falls back to the injected stand-in only where it is unavailable — on a
    machine without charm, i.e. any non-Linux dev host. The fallback still
    marks the context ``reportable = False`` and provenance still records why,
    so a stand-in run can never be mistaken for a real one; the difference is
    that the real backend is now the default rather than the only option being
    a stub.
    """
    faithful_ops: Optional[Any] = None
    if group_provider is None:
        try:
            from Common.crypto import pairing as _pairing_mod

            _backend = _pairing_mod.get_backend("ma_lb_pq_vdse", reportable=True)
            group_provider = _faithful_group_provider
            faithful_ops = _BackedGroupOperations(_backend)
        except Exception:  # noqa: BLE001 - absence is expected off the host
            group_provider = _unfaithful_group_provider
    # §VI's FOUR DOMAINS, from the config — not the corpus's width.
    #
    # This read `len(source.domains)`, which for the frozen corpus is **10**
    # (`dataset_manifest.json`'s `per_domain_counts`). So Exps. 2, 4, 5, 7 and 8
    # ran on ten domains and ten AAs while §VI says "four administrative
    # healthcare domains" and `global.yaml` says `defaults.domains: 4` and
    # `authorities.count: 4`. Three sources, two answers, and the code followed
    # neither of the two that agreed.
    #
    # Resolved 2026-09-10 in the paper's favour. Exp. 3 is unaffected: it passes
    # `domains=` explicitly because §VI sweeps d = 2…10 for that experiment.
    # RESULTS-AFFECTING for Exps. 2, 4, 5, 7, 8.
    domain_count = (
        int(config.defaults.domains) if domains is None else domains
    )
    domain_names = tuple(
        source.domains[:domain_count]
        if domain_count <= len(source.domains)
        else tuple(f"dom{i}" for i in range(domain_count))
    )

    context = init_mod.initialize(config, group_provider=group_provider)
    scheme = tokens_mod.TokenScheme.from_config(
        config, search_key or hashes.sha256(b"harness", domain=b"harness/search-key")
    )
    # ABCD_LEDGER decides; see chain/select.py. Default is unchanged.
    ledger = chain_select.make_ledger()
    aim = aim_mod.AuthorizationIndexManager()
    store = ipfs_mod.InProcessContentStore()
    register = out_mod.MetadataRegister()
    catalog = prop_mod.IndexCatalog()

    registry = authority_mod.AttributeNamespaceRegistry()
    operations = faithful_ops if faithful_ops is not None else _HarnessGroupOperations()
    authorities: Dict[str, authority_mod.Authority] = {}
    for index, domain in enumerate(domain_names, start=1):
        authority = authority_mod.Authority.create(
            context,
            authority_id=f"AA{index}",
            domain=domain,
            attributes=authority_mod.default_attributes(
                f"AA{index}", config.authorities.attributes_per_authority
            ),
            operations=operations,
            registry=registry,
        )
        authority.register(ledger)
        ledger.publish_authorization_state(authority.state())
        authorities[domain] = authority

    _node_count = min(config.topology.fog_search_nodes, len(domain_names))
    nodes = fsn_mod.build_fsn_set(
        domain_names,
        _node_count,
        bloom_bits_per_entry=config.index.bloom_bits_per_entry,
        bloom_num_hashes=config.index.bloom_num_hashes,
        # Capped at the node count: Exp. 3's low points run d=2 against m=4, and
        # `_node_count` follows d there, so an uncapped replication would ask
        # for more holders than there are nodes.
        replication=min(config.index.replication, _node_count),
    )
    aim_mod.initial_synchronization(
        aim, ledger, [a.authority_id for a in authorities.values()], nodes
    )

    owner_profile = profile_mod.build_profile_from_aim(
        aim,
        uid="DO-1",
        authority_ids=[a.authority_id for a in authorities.values()],
        attributes=sorted(
            attr for a in authorities.values() for attr in a.attributes[:2]
        ),
    )

    deployment = Deployment(
        config=config,
        scheme=scheme,
        context=context,
        ledger=ledger,
        aim=aim,
        store=store,
        register=register,
        catalog=catalog,
        authorities=authorities,
        nodes=nodes,
        owner_profile=owner_profile,
        domains=domain_names,
    )

    for record in source.records(records, domains=domain_count):
        deployment.records.append(_outsource(deployment, record))
    return deployment


def _outsource(deployment: Deployment, record) -> Dict[str, Any]:
    """Phase IV Steps 3-5 and Phase V Steps 1-5 for one record."""
    rid = record.record_id
    ciphertext = hashes.sha256(f"ct-{rid}".encode(), domain=b"harness/ct") * 4
    cid = ipfs_mod.upload_ciphertext(deployment.store, ciphertext)
    entries = tuple(
        types.IndexEntry(
            token=deployment.scheme.index_token(kw),
            cid=cid,
            policy_id=record.policy_id,
            vid=record.metadata.vid,
        )
        for kw in record.keywords
    )
    commitment = commit_mod.commit_record(
        record_id=rid,
        entries=entries,
        policy_id=record.policy_id,
        vid=record.metadata.vid,
        auth_root_do=deployment.owner_profile.auth_root,
    )
    out_mod.register_metadata(
        deployment.register, cid=cid, metadata=record.metadata, commitment=commitment
    )
    out_mod.anchor_initial_commitment(
        deployment.ledger, cid=cid, commitment=commitment, vid=record.metadata.vid
    )
    payload = prop_mod.build_sync_payload(
        entries=entries, metadata=record.metadata, cid=cid
    )
    prop_mod.propagate_to_authorized(
        payload, domain=record.domain, nodes=deployment.nodes
    )
    deployment.catalog.record(payload, domain=record.domain)
    return dict(
        record=record, entries=entries, commitment=commitment, cid=cid,
        ciphertext=ciphertext,
    )


def _unfaithful_group_provider(pairing_params):
    """A group for the harness. NOT from a faithful backend, and it says so.

    ``faithful=False`` propagates into ``GlobalContext.reportable`` and from there
    into ``run_meta.json``'s ``not_reportable_because``, so a figure produced this
    way cannot be quoted without the reason travelling with it.
    """
    return init_mod.GroupDescription(
        curve=str(pairing_params.get("curve", "BN254")),
        backend="harness-injected-not-a-backend",
        g1=b"g1", g2=b"g2", e_g1_g2=b"egt",
        faithful=False,
    )


def _faithful_group_provider(pairing_params):
    """A real Type-III bilinear group from ``Common.crypto.pairing``.

    Replaces ``_unfaithful_group_provider`` when ``charm_type3`` is available.
    ``faithful=True`` is what lets ``group_faithful`` reach
    ``provenance.reportability()`` as satisfied — the proposed scheme could not
    produce a quotable number at all while the stand-in below was the only
    option (``crypto.yaml: backend_implemented: false``).
    """
    from Common.crypto import pairing as pairing_mod

    backend = pairing_mod.get_backend("ma_lb_pq_vdse", reportable=True)
    g1, g2 = backend.random_g1(), backend.random_g2()
    return init_mod.GroupDescription(
        curve=backend.curve,
        backend=backend.name,
        g1=backend.serialize(g1),
        g2=backend.serialize(g2),
        e_g1_g2=backend.serialize(backend.pair(g1, g2)),
        faithful=True,
    )


class _BackedGroupOperations:
    """Real group operations over the configured Type-III backend.

    Elements cross this interface as serialised bytes (that is the shape the
    scheme's phases already use), so each operation deserialises, works in the
    group, and re-serialises. That costs a little per call but keeps the
    measured cost *real* rather than the hash stand-in's constant-time SHA-256,
    which understated every group operation in the scheme.
    """

    def __init__(self, backend) -> None:
        self._b = backend
        self._group = backend.group

    def _g1(self, raw: bytes):
        return self._group.deserialize(raw)

    def random_exponent(self) -> bytes:
        return self._b.serialize(self._b.random_zr())

    def exponentiate_gt(self, base: bytes, exponent: bytes) -> bytes:
        return self._b.serialize(self._g1(base) ** self._g1(exponent))

    def exponentiate_g1(self, base: bytes, exponent: bytes) -> bytes:
        return self._b.serialize(self._g1(base) ** self._g1(exponent))

    def multiply_g1(self, left: bytes, right: bytes) -> bytes:
        return self._b.serialize(self._g1(left) * self._g1(right))

    def hash_to_g1(self, data: bytes) -> bytes:
        return self._b.serialize(self._b.hash_to_g1(data))


class _HarnessGroupOperations:
    """Hash-based stand-ins. Not a group; see the Phase III test discussion."""

    def __init__(self) -> None:
        self._counter = 0

    def random_exponent(self) -> bytes:
        self._counter += 1
        return hashes.sha256(
            self._counter.to_bytes(4, "big"), domain=b"harness/exponent"
        )

    def exponentiate_gt(self, base: bytes, exponent: bytes) -> bytes:
        return hashes.sha256(base, exponent, domain=b"harness/gt")

    def exponentiate_g1(self, base: bytes, exponent: bytes) -> bytes:
        return hashes.sha256(base, exponent, domain=b"harness/g1")

    def multiply_g1(self, left: bytes, right: bytes) -> bytes:
        return hashes.sha256(left, right, domain=b"harness/g1-mul")

    def hash_to_g1(self, data: bytes) -> bytes:
        return hashes.sha256(data, domain=b"harness/g1-hash")


def _enrol(deployment: Deployment, uid: str, domains: Sequence[str]):
    """A Data User authorized across ``domains``, with its resolver."""
    authority_ids = [deployment.authorities[d].authority_id for d in domains]
    attributes = sorted(
        attr for d in domains for attr in deployment.authorities[d].attributes[:3]
    )
    profile = profile_mod.build_profile_from_aim(
        deployment.aim, uid=uid, authority_ids=authority_ids, attributes=attributes
    )
    mapping = {}
    for domain in domains:
        policies = {
            r["record"].policy_id
            for r in deployment.records
            if r["record"].domain == domain
        }
        mapping[(uid, domain)] = tuple(sorted(policies))
    return profile, authority_ids, attributes, authz_mod.MappingPolicyResolver(mapping)


# ===========================================================================
# Exp. 1 — Policy-State-Aware Token Generation
# ===========================================================================
@dataclass
class Exp1TrapdoorGeneration:
    """"online trapdoor generation only… ML-KEM encapsulation is excluded".

    ``measure`` calls ``generate_search_token`` and nothing else: ``q`` PRF
    evaluations plus a nonce draw. The KEM keypair and the VAP are built in
    ``prepare``, which is where session establishment belongs — reporting it here
    would fold a one-time cost into a per-query curve.
    """

    config: scheme_config.Configuration
    source: SyntheticRecordSource
    name: str = "exp1_trapdoor_generation"
    number: int = 1
    variable: str = "keywords_per_query"
    values: Tuple[Any, ...] = ()
    primary: MetricSpec = MetricSpec("latency", MS, is_timing=True)
    secondaries: Tuple[MetricSpec, ...] = (
        MetricSpec("trapdoor_size", BYTES),
        MetricSpec("tokens", COUNT),
    )

    def __post_init__(self) -> None:
        if not self.values:
            self.values = tuple(
                self.config.experiment("exp1").values
            )

    def prepare(self, value: Any) -> Any:
        deployment = build_deployment(
            config=self.config, source=self.source, records=8
        )
        profile, _, _, _ = _enrol(deployment, "DU-1", deployment.domains[:1])
        keywords = [f"kw:{i:05d}" for i in range(int(value))]
        return deployment.scheme, profile, keywords

    def measure(self, prepared: Any) -> Sample:
        scheme, profile, keywords = prepared
        started = time.perf_counter_ns()
        token = token_mod.generate_search_token(scheme, profile, keywords)
        elapsed = time.perf_counter_ns() - started
        cost = token_mod.trapdoor_cost(token)
        return Sample(
            primary=elapsed / 1e6,
            secondaries={
                "trapdoor_size": float(cost.size_bytes),
                "tokens": float(cost.keyword_count),
            },
        )


# ===========================================================================
# Exp. 2 — Search Latency
# ===========================================================================
#: Seed for Exp. 2's query draw. Fixed so a sweep is reproducible, and separate
#: from the corpus seed so re-drawing queries cannot change which records exist.
EXP2_QUERY_SEED = 20260903

#: A keyword must appear in at least this many records to be worth querying, and
#: in at most this share of them. Both bounds are taken verbatim from
#: ``yue_ge/src/workload.py::select_keywords``, which in turn mirrors
#: ``guo_vdsse/src/exp2_search.py`` -- the point is that all three schemes draw
#: queries at comparable selectivity, so the numbers compare.
QUERY_MIN_FREQUENCY = 5
QUERY_MAX_SHARE = 0.5


def select_query_keywords(
    freq: "Counter[str]",
    rng: DeterministicRNG,
    count: int,
    *,
    total_records: Optional[int] = None,
) -> List[str]:
    """Draw ``count`` query keywords at the selectivity the baselines use.

    Skip keywords too rare to produce a measurable traversal, and ones so common
    they swamp the scan and hide the scaling. Falls back to the whole vocabulary
    when the bounds leave too few -- a small point must still produce a query
    rather than failing the sweep.
    """
    ceiling = total_records * QUERY_MAX_SHARE if total_records else float("inf")
    eligible = [
        kw for kw, n in freq.items() if n >= QUERY_MIN_FREQUENCY and n <= ceiling
    ]
    if len(eligible) < count:
        eligible = sorted(freq)
    if not eligible:
        raise ValueError("no keyword in the built index is queryable")
    eligible.sort()
    return list(rng.choice(eligible, size=min(count, len(eligible)), replace=False))


@dataclass
class Exp2SearchLatency:
    """"the full online path: AIM authorization check → AASS selection → shard
    search → response assembly. Index construction is offline and excluded."

    All four stages are inside ``measure``; the index is built in ``prepare``.
    ``n_eff`` is reported alongside latency because it is "the only thing that can
    demonstrate the paper's claim".
    """

    config: scheme_config.Configuration
    source: SyntheticRecordSource
    name: str = "exp2_search_latency"
    number: int = 2
    variable: str = "index_size"
    values: Tuple[Any, ...] = ()
    primary: MetricSpec = MetricSpec("latency", MS, is_timing=True)
    secondaries: Tuple[MetricSpec, ...] = (
        MetricSpec("n_eff", COUNT),
        MetricSpec("entries_traversed", COUNT),
        # THE SAME QUANTITY AS `n_eff` HERE, under the name the other four
        # schemes now use. `n_eff` is not comparable across schemes -- matched
        # entries here, traversal counters in [30]/[35]/[54] -- so SVI Exp. 2's
        # "query selectivity is kept constant" could not be checked against it
        # in either direction. Emitted under a name that means one thing.
        MetricSpec("matched_records", COUNT),
    )

    def __post_init__(self) -> None:
        if not self.values:
            self.values = tuple(self.config.experiment("exp2").values)

    def prepare(self, value: Any) -> Any:
        """``N`` is the index size in RECORDS, which is what the axis says.

        This used to size the deployment as ``int(value) //
        keywords_per_record``. With the frozen corpus's ``|W_i| ~= 32`` that
        turned N=10^6 into 31,250 records, while every baseline indexes
        ``records[:N]`` -- guo (``exp2_search.py``), yue_ge
        (``runner.py:130``) and perera (``runner.py:87``) all slice records.
        So at the same point on a shared x-axis we searched a corpus 32x
        smaller than the schemes we were compared against, under
        `generate_plots.py`'s label "Index size $N$ (records)" and SVI's "from
        $10^4$ to $10^6$ encrypted records".

        Identical to the defect Exp. 4 carried and had fixed (see
        ``Exp4VerificationOverhead.prepare``): the same ``// keywords_per_record``
        idiom, the same entries-under-a-records-caption confusion, the same
        broken like-for-like. Fixed here the same way.
        """
        record_count = max(1, int(value))
        # REFUSE A POINT THAT CANNOT FIT, rather than be OOM-killed.
        #
        # An OOM kill is SIGKILL: no traceback, no partial results, nothing in
        # the log. That is how the 2026-08-28 campaign died (rc=137, anon-rss
        # 15.67 GB) and guo's runner has guarded itself this way since. Exp. 2
        # needed no guard while it built `N // 32` records; sizing N in RECORDS
        # (2026-09-07) multiplied the index by ~32 and brought the top of the
        # sweep within reach of the ceiling.
        #
        # ~6.8 KB/record measured by tracemalloc over the synthetic source at
        # |W_i| = 6, scaled by the frozen corpus's |W_i| = 31.70. Rough -- a
        # linear extrapolation of Python allocations, not an RSS reading -- but
        # it puts N=5*10^5 near 18 GB and N=10^6 near 36 GB against a 16 GiB
        # host, so the top two points need a larger instance exactly as guo's
        # do. Better a clear refusal than a silent kill.
        _BYTES_PER_RECORD = 6_800 * self.source.keywords_per_record / 6
        crypto_config.assert_memory_for(
            record_count * _BYTES_PER_RECORD,
            f"exp2 index at N={record_count:,} records",
        )
        deployment = build_deployment(
            config=self.config, source=self.source, records=record_count
        )
        domain = deployment.domains[0]
        profile, authority_ids, attributes, resolver = _enrol(
            deployment, "DU-1", (domain,)
        )
        # WAS: keywords[0] of the FIRST record in the domain, reused for every
        # run. That made this experiment measure the repeatability of one
        # arbitrary query while guo and yue_ge each draw a NEW keyword per run
        # (yue_ge/exp2_search_latency/runner.py:130,158). The two are not the
        # same estimand: Peony++ is output-sensitive, so its spread across a
        # keyword draw is the measurement, and our flat curve was an artefact of
        # never varying the query rather than evidence of stable latency.
        #
        # Now: draw warmups+repetitions keywords at the baselines' selectivity
        # and advance one per call, so all three schemes answer the same
        # question. n_eff varies run to run as a result -- that is the point.
        frequency: "Counter[str]" = Counter()
        in_domain = 0
        carriers: Dict[str, Tuple[str, ...]] = {}
        for entry in deployment.records:
            if entry["record"].domain == domain:
                in_domain += 1
                record_keywords = tuple(dict.fromkeys(entry["record"].keywords))
                frequency.update(record_keywords)
                # One representative record per keyword, for the co-occurrence
                # completion below. First writer wins so the choice is a
                # deterministic function of corpus order, not of the draw.
                for keyword in record_keywords:
                    carriers.setdefault(keyword, record_keywords)
        measurement = self.config.measurement
        draws = measurement.warmup_runs + measurement.repetitions
        rng = DeterministicRNG(EXP2_QUERY_SEED).spawn(f"exp2/N={value}")
        anchors = select_query_keywords(
            frequency, rng, draws, total_records=in_domain
        )

        # A q-KEYWORD CONJUNCTIVE QUERY, and the q keywords must CO-OCCUR.
        #
        # This used to issue `[keyword]` -- ONE keyword -- against
        # `global.yaml`'s `keywords_per_query: 5`, sourced to SVI "each query
        # contains five keywords". Exactly the defect the 2026-09-06 sweep
        # fixed for Exp. 7/8 (`experiments.py`, SchedulerAblation), whose entry
        # claimed "every other experiment honours it"; Exp. 2 and Exp. 3 did
        # not, and were missed.
        #
        # Independently drawn keywords will not do. Over a corpus averaging
        # |W_i| ~= 32 out of a 2,023-keyword universe, five separately-frequent
        # keywords essentially never all land in one record, so the conjunction
        # matches NOTHING -- the failure that made guo's Exp. 2 measure a search
        # short-circuiting on its first miss, and psa_exp2 bank n_eff = 0.0 at
        # every point. So the ANCHOR is drawn at the baselines' selectivity
        # (yue_ge is single-keyword by construction and draws exactly this way),
        # and the query is completed from a record that carries it. The anchor
        # sets selectivity; the completion makes the query answerable.
        q = max(1, int(self.config.defaults.keywords_per_query))
        queries: List[List[str]] = []
        for anchor in anchors:
            carrier = carriers.get(anchor, (anchor,))
            query = [anchor] + [k for k in carrier if k != anchor]
            queries.append(query[:q])
        return dict(
            deployment=deployment, profile=profile, authority_ids=authority_ids,
            attributes=attributes, resolver=resolver,
            keywords=anchors, queries=queries, cursor=[0],
        )

    def measure(self, prepared: Any) -> Sample:
        d = prepared["deployment"]
        # One keyword per call, cycling. Warm-ups consume the first entries, as
        # they do for yue_ge, so the retained runs see the same draw the
        # baselines' retained runs see.
        pool = prepared["queries"]
        cursor = prepared["cursor"]
        query = pool[cursor[0] % len(pool)]
        cursor[0] += 1
        started = time.perf_counter_ns()
        token = token_mod.generate_search_token(
            d.scheme, prepared["profile"], query
        )
        decision = authz_mod.verify_search_request(          # AIM check
            d.aim, token, prepared["profile"],
            authority_ids=prepared["authority_ids"],
            attributes=prepared["attributes"],
            resolver=prepared["resolver"],
        )
        if not decision.accepted:
            raise RuntimeError(f"authorization rejected: {decision.reason}")
        request = aass_mod.SearchRequest(
            tokens=token.tokens,
            authorized=decision.authorized_shards,
            vid_u=token.vid_u,
            query_versions=decision.query_versions,
        )
        selection = aass_mod.Scheduler(aass_mod.VARIANT_AASS).select(  # AASS
            d.nodes, request
        )
        response = search_mod.execute_search(                # shard search
            selection.node, token.tokens, decision.authorized_shards
        )
        hits = tuple(response.hits)                          # response assembly
        elapsed = time.perf_counter_ns() - started
        return Sample(
            primary=elapsed / 1e6,
            secondaries={
                "n_eff": float(response.n_eff),
                "entries_traversed": float(response.statistics.entries_traversed),
                "matched_records": float(response.n_eff),
            },
        )


# ===========================================================================
# Exp. 3 — Cross-Domain Search Scalability
# ===========================================================================
@dataclass
class Exp3CrossDomain:
    """"Issues ONE authorization-bound trapdoor reused across domains. Count
    trapdoors issued as a secondary metric."

    The secondary is the point: it stays at 1 as ``d`` grows, where a baseline
    issues ``d``.
    """

    config: scheme_config.Configuration
    source: SyntheticRecordSource
    name: str = "exp3_crossdomain_scalability"
    number: int = 3
    variable: str = "domains"
    values: Tuple[Any, ...] = ()
    primary: MetricSpec = MetricSpec("latency", MS, is_timing=True)
    secondaries: Tuple[MetricSpec, ...] = (
        MetricSpec("trapdoors_issued", COUNT),
        MetricSpec("nodes_searched", COUNT),
    )

    def __post_init__(self) -> None:
        if not self.values:
            self.values = tuple(self.config.experiment("exp3").values)

    def prepare(self, value: Any) -> Any:
        """PER-DOMAIN index size fixed, so total data grows with ``d``.

        §VI Exp. 3: "The query size and per-domain index size are fixed to
        isolate cross-domain search overhead." This built ``records=d*4`` — a
        per-domain size of FOUR records, so d=10 indexed 40 records while every
        baseline indexed 100,000 at the same point on the shared axis. The
        convention was right and the size made the comparison meaningless.

        The baselines had the opposite defect: they fixed TOTAL at 100,000 and
        sharded by ``d``, so their per-domain size SHRANK 50,000 -> 10,000 and
        Scheme [35]'s latency actually FELL as ``d`` grew. §VI read that pair as
        the proposed scheme "exhibiting slower growth" when the curve directions
        were set by the two designs.

        All five now hold per-domain fixed at ``global.yaml``'s
        ``exp3 -> held_constant.per_domain_index_size``.
        """
        domain_count = int(value)
        held = self.config.experiment("exp3").held_constant or {}
        per_domain = int(held.get("per_domain_index_size", 0))
        if per_domain <= 0:
            raise ValueError(
                "global.yaml exp3_crossdomain_scalability.held_constant."
                "per_domain_index_size must be a positive record count; §VI "
                "fixes the per-domain index size and the value cannot come "
                "from a literal here"
            )
        record_count = per_domain * domain_count
        # Same guard and the same measured constant as Exp. 2: a refusal beats
        # an OOM kill, which is SIGKILL and leaves no traceback.
        _BYTES_PER_RECORD = 6_800 * self.source.keywords_per_record / 6
        crypto_config.assert_memory_for(
            record_count * _BYTES_PER_RECORD,
            f"exp3 index at d={domain_count} x {per_domain:,} records/domain",
        )
        deployment = build_deployment(
            config=self.config, source=self.source,
            records=record_count, domains=domain_count,
        )
        # A q-KEYWORD CONJUNCTIVE QUERY, per §VI's "each query contains five
        # keywords". This issued ONE keyword until 2026-09-09 — the defect the
        # 2026-09-06 sweep fixed for Exp. 7/8 and the 2026-09-03 one for Exp. 2,
        # both of which missed Exp. 3.
        q = max(1, int(self.config.defaults.keywords_per_query))
        shared = [f"kw:{i:05d}" for i in range(q)]
        # One representative record per domain, in ONE pass. The per-domain
        # `next(...)` scan was O(d*N) and N is now 10,000x larger.
        first_in_domain: Dict[str, Any] = {}
        for entry in deployment.records:
            first_in_domain.setdefault(entry["record"].domain, entry)
        # Index the shared keywords in every domain so one trapdoor can hit all
        # of them — the property this experiment measures.
        for domain in deployment.domains:
            node = deployment.node_for(domain)
            record = first_in_domain[domain]
            node.insert_entries(
                [
                    types.IndexEntry(
                        token=deployment.scheme.index_token(keyword),
                        cid=record["cid"],
                        policy_id=record["record"].policy_id,
                        vid=record["record"].metadata.vid,
                    )
                    for keyword in shared
                ],
                domain=domain,
            )
        profile, authority_ids, attributes, resolver = _enrol(
            deployment, "DU-1", deployment.domains
        )
        return dict(
            deployment=deployment, profile=profile, authority_ids=authority_ids,
            attributes=attributes, resolver=resolver, keywords=shared,
        )

    def measure(self, prepared: Any) -> Sample:
        d = prepared["deployment"]
        started = time.perf_counter_ns()
        token = token_mod.generate_search_token(
            d.scheme, prepared["profile"], prepared["keywords"]
        )
        decision = authz_mod.verify_search_request(
            d.aim, token, prepared["profile"],
            authority_ids=prepared["authority_ids"],
            attributes=prepared["attributes"],
            resolver=prepared["resolver"],
        )
        if not decision.accepted:
            raise RuntimeError(f"authorization rejected: {decision.reason}")
        responses = search_mod.execute_search_across(
            d.nodes, token.tokens, decision.authorized_shards
        )
        search_mod.merge_responses(responses)
        elapsed = time.perf_counter_ns() - started
        return Sample(
            primary=elapsed / 1e6,
            secondaries={
                # ONE trapdoor, however many domains. This is the claim -- so it
                # is COUNTED from the token the DU actually issued, not asserted.
                #
                # It was the literal `1.0` until 2026-09-08. That made the
                # experiment's headline secondary unfalsifiable: a regression
                # that made the trapdoor domain-dependent would have kept
                # reporting 1.0, and §VI cites this number as the difference
                # between the proposed scheme and baselines that issue d of
                # them. `len(token.tokens)` is q under Option D -- one PRF
                # evaluation per keyword, no domain factor -- so it stays 1.0
                # for the current q=1 workload and no banked number moves,
                # while a per-domain trapdoor would now read q*d.
                #
                # Note this measures what it says only while the query is
                # single-keyword: when 3-4 raises q to the published 5, the
                # honest reading of this column is "trapdoors, not one per
                # domain", i.e. q rather than q*d, and §VI's sentence should
                # say so.
                "trapdoors_issued": float(len(token.tokens)),
                "nodes_searched": float(len(responses)),
            },
        )


# ===========================================================================
# Exp. 4 — Fine-Grained Verification Effectiveness
# ===========================================================================
@dataclass
class Exp4Verification:
    """"Verification is client-side: Merkle proof check, ``Commit_i*``
    recomputation, and blockchain-consistency check. IPFS fetch and decryption are
    EXCLUDED."

    ``measure`` calls ``verify_response`` and nothing else; the bundles are built
    in ``prepare``, since generating them is the Fog Search Node's Phase VI Step 5
    work, not the client's.
    """

    config: scheme_config.Configuration
    source: SyntheticRecordSource
    name: str = "exp4_verification_overhead"
    number: int = 4
    variable: str = "returned_results"
    values: Tuple[Any, ...] = ()
    primary: MetricSpec = MetricSpec("latency", MS, is_timing=True)
    secondaries: Tuple[MetricSpec, ...] = (
        MetricSpec("proof_size", KB),
        MetricSpec("path_length", COUNT),
    )

    def __post_init__(self) -> None:
        if not self.values:
            self.values = tuple(self.config.experiment("exp4").values)

    def prepare(self, value: Any) -> Any:
        """``r`` RETURNED RECORDS, one verification bundle each.

        This used to size the deployment as ``ceil(r / keywords_per_record)``
        and then take ``r`` BUNDLES from it. With the frozen corpus's
        ``|W_i| ~= 32`` that made r=1000 into 32 records carrying 1000 index
        entries, so the per-record half of Phase VIII -- the commitment
        recomputation and the chain-consistency check -- ran 32 times where §VI
        says it runs once per returned ciphertext, and the figure's x-axis
        counted index entries under a caption reading "returned results".

        It also broke the comparison the figure exists to make. Every baseline
        sweeps r as RECORDS -- perera slices ``verifiable[:r]``, yue_ge returns
        r result ids, guo picks a keyword matching ~r documents -- so at the
        same x, Scheme [54] verified 1000 signatures while we checked 32
        commitments. That is not a like-for-like point on a shared axis.

        Now r records produce r bundles, one per record, which is what §VI
        describes and what the baselines measure.
        """
        wanted = int(value)
        deployment = build_deployment(
            config=self.config, source=self.source, records=wanted
        )
        bundles: List[proof_mod.VerificationBundle] = []
        for record in deployment.records:
            responses = proof_mod.build_response(
                record["commitment"], record["entries"]
            )
            if responses:
                # ONE bundle per returned ciphertext. Taking every entry's
                # bundle is what conflated entries with records above.
                bundles.append(responses[0])
            if len(bundles) >= wanted:
                break
        return dict(
            deployment=deployment,
            bundles=tuple(bundles[:wanted]),
            auth_root=deployment.owner_profile.auth_root,
        )

    def measure(self, prepared: Any) -> Sample:
        d = prepared["deployment"]
        # ONE namespace walk for the whole response, not one per record.
        # `lookup_anchor` sorts the entire version-identifier namespace on every
        # call, so the per-record checker made Step 3 O(r^2) and, measured, 99.6%
        # of the chain step against 0.4% for the three comparisons it exists to
        # make. Those comparisons are still per record -- BC_i carries subscript
        # i, and per-record anchoring is what lets a rejected record be named --
        # so nothing about what is verified changes. The fetch is lazy, so its
        # cost lands inside the timed region below rather than in `prepare`.
        checker = vledger_mod.batched_chain_checker(
            d.ledger,
            [b.cid for b in prepared["bundles"]],
            check_chain_integrity=False,
        )
        # Collector paused across the timed region -- see Common/timing.py. This
        # measurement is byte-identical every run (the bundles are an immutable
        # fixture built in prepare), so a gen-2 pause landing in one of the ten
        # was pure interpreter schedule: it put run 4 at r=1000 at 45.4 ms
        # against 15.13-15.51 ms for the other nine, on two separate days.
        with gc_quiesced():
            started = time.perf_counter_ns()
            batch = proof_mod.verify_response(
                prepared["bundles"],
                auth_root=prepared["auth_root"],
                # Phase VIII Step 2's VID_i = VID_U compares two different
                # counters. Skipped so the measurement is of the
                # cryptographic work rather than of a check that rejects every
                # record.
                require_version_match=False,
                chain_check=checker,
            )
            elapsed = time.perf_counter_ns() - started
        if batch.accepted_count != batch.record_count:
            raise RuntimeError(
                f"{len(batch.rejected)} of {batch.record_count} bundles failed "
                f"verification"
            )
        return Sample(
            primary=elapsed / 1e6,
            secondaries={
                "proof_size": batch.total_proof_kb,
                "path_length": batch.mean_path_length,
            },
        )


# ===========================================================================
# Exp. 5 — Dynamic Index Update
# ===========================================================================
@dataclass
class Exp5KeywordUpdate:
    """"Measure incremental update only. A global index rebuild indicates a Phase
    VII implementation bug."

    ``k`` counts (keyword, document) pairs. The secondaries are the evidence that
    the update was incremental: Merkle nodes recomputed, and entries rewritten.
    """

    config: scheme_config.Configuration
    source: SyntheticRecordSource
    name: str = "exp5_keyword_update"
    number: int = 5
    variable: str = "keyword_document_pairs"
    values: Tuple[Any, ...] = ()
    primary: MetricSpec = MetricSpec("latency", MS, is_timing=True)
    secondaries: Tuple[MetricSpec, ...] = (
        MetricSpec("merkle_nodes_recomputed", COUNT),
        MetricSpec("entries_rewritten", COUNT),
    )

    def __post_init__(self) -> None:
        if not self.values:
            self.values = tuple(self.config.experiment("exp5").values)

    def prepare(self, value: Any) -> Any:
        pairs = int(value)
        per_record = self.source.keywords_per_record
        record_count = max(1, -(-pairs // per_record))
        deployment = build_deployment(
            config=self.config, source=self.source, records=record_count
        )
        return dict(deployment=deployment, pairs=pairs)

    def measure(self, prepared: Any) -> Sample:
        d = prepared["deployment"]
        pairs = prepared["pairs"]
        applied = 0
        nodes_recomputed = 0
        entries_rewritten = 0
        started = time.perf_counter_ns()
        for record in d.records:
            if applied >= pairs:
                break
            domain = record["record"].domain
            receipt = dias_mod.synchronize(
                dias_mod.UpdateRequest(
                    operation=dias_mod.Operation.MODIFY,
                    cid=record["cid"],
                    delta=dias_mod.UpdateDelta(
                        policy_id=f"{domain}/updated-{applied}"
                    ),
                ),
                authority=d.authorities[domain],
                nodes=d.nodes,
                commitment=record["commitment"],
                entries=record["entries"],
                auth_root_do=d.owner_profile.auth_root,
            )
            applied += receipt.index_evolution.entries_touched
            nodes_recomputed += receipt.merkle_nodes_recomputed
            entries_rewritten += receipt.entries_rewritten
            if receipt.commitment_evolution.rebuilt:
                raise RuntimeError(
                    "a Modify rebuilt the record's tree; Step 4 must path-update "
                    "when the leaf count is unchanged"
                )
        elapsed = time.perf_counter_ns() - started
        return Sample(
            primary=elapsed / 1e6,
            secondaries={
                "merkle_nodes_recomputed": float(nodes_recomputed),
                "entries_rewritten": float(entries_rewritten),
            },
        )


# ===========================================================================
# Exp. 6 — DIAS Synchronization Ablation
# ===========================================================================
#: Exp. 6 ablation — one variant per half of the DIAS claim.
#:
#: The manuscript (Exp. 6) names the three arms *DIAS*, *Incremental-All* and
#: *Full-State Synchronization*. The constants below carry those names; their
#: STRING VALUES deliberately still read ``ias`` / ``broadcast`` /
#: ``full_rebuild`` because the value is the on-disk slug in
#: ``exp6_authorization_sync__<slug>/``, and every banked result sits in a
#: directory named that way. Renaming the values would orphan measured data to
#: buy nothing — the reader never sees a slug, only the figure legend, which
#: ``Plots/generate_plots.py::EXP6_VARIANTS`` maps to the manuscript names.
#:
#:   DIAS (``ias``)                     the published rule: selective
#:                                      delivery, and only the affected
#:                                      authority evolves. The default.
#:   Incremental-All (``broadcast``)    tests SELECTIVE. DIAS_i goes to every
#:                                      FSN rather than only those holding the
#:                                      affected shard — the alternative
#:                                      Phase VII Step 3 names and rejects.
#:   Full-State (``full_rebuild``)      tests INCREMENTAL. Every authority
#:                                      recomputes its commitment and the AIM
#:                                      republishes it, instead of Phase II
#:                                      Step 4's "unaffected authorities retain
#:                                      their existing states".
#:
#: These are ABLATIONS of the proposed scheme, not baselines from other papers,
#: exactly as Exp. 7-8's four scheduler variants are. Neither is a strawman built
#: on a known-slow implementation: ``full_rebuild`` calls the SAME
#: ``Authority.commitment()`` the proposed path calls and differs only in how many
#: authorities it calls it for. The superseded O(delta^2) ``RevocationList`` is
#: deliberately NOT used here — measuring against an old bug would overstate the
#: advantage.
VARIANT_DIAS = "ias"
VARIANT_INCREMENTAL_ALL = "broadcast"
VARIANT_FULL_STATE = "full_rebuild"
EXP6_VARIANTS: Tuple[str, ...] = (
    VARIANT_DIAS, VARIANT_INCREMENTAL_ALL, VARIANT_FULL_STATE
)


def _incremental_all_selector(message, nodes):
    """Every FSN, not only those serving the affected domain."""
    return tuple(nodes)


@dataclass
class Exp6AuthorizationSync:
    """"DIAS propagation: authority commitment recomputation → Merkle path update
    → DIAS message → selective FSN propagation, until all affected FSNs report the
    new VID. Report FSNs touched."

    ``synchronize`` verifies that postcondition itself, so a run that returns has
    reached it.

    **Boundary.** Phase VII Step 5 (anchoring ``BC_i'``) is OUTSIDE this
    measurement: no ledger is passed to ``synchronize``, so nothing is anchored on
    the timed path. That matches global.yaml, whose Exp. 6 boundary ends at "until
    all affected FSNs report the new ``VID``", and ``tab:cost``'s authorization-
    synchronization row ``O(delta)T_H + O(log d)T_MT``, which carries no chain
    term. Anchoring is also identical across all three variants, so including it
    could not change which one wins — only add a constant. §VI must state the
    exclusion and report the anchor cost separately, the way global.yaml already
    handles ML-KEM encapsulation for Exp. 1.

    **What ``fsns_touched`` can and cannot show.** ``assign_domains_to_fsns`` gives
    each domain to exactly ONE node, and a ``DIASMessage`` carries exactly one
    domain, so selective delivery touches exactly one node for ANY ``d`` and ``m``.
    Under ``ias`` the metric is therefore a constant 1 BY CONSTRUCTION, and is
    evidence of nothing unless read against ``broadcast``'s ``m``. Every run before
    2026-09-03 reported it alone, which is why global.yaml's "selective propagation
    is the claim" had no measurement behind it.

    **Why ``delivered_kb`` is measured and not derived.** It is the quantity the
    selective claim is actually about: the bytes the network carries per update.
    ``dias_message_size x fsns_touched`` looks like the same number and is not,
    because under ``full_rebuild`` only ONE of the deliveries is a DIAS message.
    The other ``(d-1) x m`` are ``AuthorizationMeta`` republishes, which are a
    third the size — ``Meta_i = (Dom_i, VID_i, C_i^auth)`` against a message that
    also carries the entries, the root and the commit. The product therefore
    charges full_rebuild ~3x the bytes it sends. Summing the real encodings here
    is the only way the panel says what it claims to.
    """

    config: scheme_config.Configuration
    source: SyntheticRecordSource
    name: str = "exp6_authorization_sync"
    number: int = 6
    variable: str = "authorization_updates"
    values: Tuple[Any, ...] = ()
    variant: str = VARIANT_DIAS
    primary: MetricSpec = MetricSpec("latency", MS, is_timing=True)
    secondaries: Tuple[MetricSpec, ...] = (
        MetricSpec("dias_message_size", KB),
        MetricSpec("fsns_touched", COUNT),
        MetricSpec("delivered_kb", KB),
    )

    def __post_init__(self) -> None:
        if not self.values:
            self.values = tuple(self.config.experiment("exp6").values)
        if self.variant not in EXP6_VARIANTS:
            raise ValueError(
                f"unknown Exp. 6 variant {self.variant!r}; "
                f"valid: {', '.join(EXP6_VARIANTS)}"
            )

    def prepare(self, value: Any) -> Any:
        deployment = build_deployment(
            config=self.config, source=self.source, records=8
        )
        return dict(deployment=deployment, updates=int(value))

    def measure(self, prepared: Any) -> Sample:
        d = prepared["deployment"]
        record = d.records[0]
        domain = record["record"].domain
        authority = d.authorities[domain]
        others = tuple(a for dom, a in d.authorities.items() if dom != domain)
        selector = (
            _incremental_all_selector if self.variant == VARIANT_INCREMENTAL_ALL else None
        )
        rebuild = self.variant == VARIANT_FULL_STATE
        # Sized BEFORE the timer, not inside the loop. Meta_i is
        # (Dom_i, VID_i, C_i^auth) and none of the three changes length while
        # the loop runs -- only `authority` is revoked, and its VID is not in
        # this sum -- so one encoding is the size of every republish. Doing it
        # per update would put an encode() on the timed path and charge
        # full_rebuild for measurement work the other two variants do not do.
        rebuild_kb_per_update = 0.0
        if rebuild:
            rebuild_kb_per_update = sum(
                len(other.meta().encode()) / 1024.0 for other in others
            ) * len(d.nodes)
        total_bytes = 0.0
        touched = 0
        delivered = 0.0
        started = time.perf_counter_ns()
        for index in range(prepared["updates"]):
            receipt = dias_mod.synchronize(
                dias_mod.UpdateRequest(
                    operation=dias_mod.Operation.REVOKE,
                    cid=record["cid"],
                    delta=dias_mod.UpdateDelta(revoked=(f"patient-{index}",)),
                ),
                authority=authority,
                nodes=d.nodes,
                commitment=record["commitment"],
                entries=record["entries"],
                auth_root_do=d.owner_profile.auth_root,
                aim=d.aim,
                select_nodes=selector,
            )
            total_bytes += receipt.message.size_kb
            touched += receipt.touched_count
            delivered += receipt.delivered_kb
            if rebuild:
                # Global authorization reconstruction: every OTHER authority
                # recomputes C_k^auth and the AIM republishes it to every node.
                # Authority.commitment() is uncached by design, so this is the
                # real recomputation rather than a re-read of a stored value.
                for other in others:
                    meta = other.meta()
                    d.aim.register_meta(other.authority_id, meta)
                    for node in d.nodes:
                        node.apply_meta(other.authority_id, meta)
                        touched += 1
                delivered += rebuild_kb_per_update
        elapsed = time.perf_counter_ns() - started
        updates = max(1, prepared["updates"])
        return Sample(
            primary=elapsed / 1e6,
            secondaries={
                "dias_message_size": total_bytes / updates,
                "fsns_touched": touched / updates,
                "delivered_kb": delivered / updates,
            },
        )


# ===========================================================================
# Exp. 7 / 8 — throughput and load balancing, from ONE set of runs
# ===========================================================================
@dataclass
class _WorkloadOutcome:
    """Both metric sets from a single closed-loop replay."""

    throughput: float
    p50_ms: float
    p95_ms: float
    rejected: int
    utilization_stddev: float
    max_utilization: float
    cross_node_forwards: int
    max_queue_depth: int


def _default_ramp_seconds() -> float:
    """global.yaml's ramp, from global.yaml with a hard fallback.

    Resolved once at import rather than per call so there is a single name to
    override -- the test suite zeroes RAMP_SECONDS in conftest, and reading the
    config inside _ramp() would silently defeat that.
    """
    try:
        configured = scheme_config.load().experiment("exp8").ramp_seconds
    except Exception:  # noqa: BLE001 - a missing key must not break collection
        configured = None
    return 30.0 if configured is None else float(configured)


#: global.yaml: "Cold vs warm. Defaults: Exp. 1-6 warm, Exp. 7-8 warm after a 30 s
#: ramp." Applied once per sweep point, inside prepare(), which run_point()
#: excludes from every timing.
RAMP_SECONDS = _default_ramp_seconds()


@dataclass
class SchedulerAblation:
    """The shared engine for Exp. 7 and Exp. 8.

    global.yaml: "Exp. 7 and Exp. 8 report different metrics over **the same
    recorded arrival trace**, replayed once per experiment per variant." The
    trace is recorded once in prepare() and both experiments record the same
    one; they do NOT share a replay. Each runs its own, so the two differ by
    timing noise and Exp. 8's sigma cannot be paired run-for-run with a
    specific Exp. 7 throughput. The per-point cross-variant comparison the
    figures show is unaffected.

    **Not reportable, for two reasons beyond the λ sweep.** skill.md requires each
    FSN to be an independent process; this replays in one interpreter, so a
    concurrency figure would not measure the stated topology. Both reasons are
    recorded in ``run_meta.json``.
    """

    config: scheme_config.Configuration
    source: SyntheticRecordSource
    variant: str = aass_mod.VARIANT_AASS

    #: Set False only to compare against the legacy single-interpreter path.
    #: skill.md requires independent FSN processes and
    #: provenance.reportability() blocks a concurrency result without them.
    independent_processes: bool = True

    def replay(self, deployment: Deployment, requests, concurrency: int) -> _WorkloadOutcome:
        if self.independent_processes:
            try:
                return self._replay_multiprocess(deployment, requests, concurrency)
            except RuntimeError as exc:
                # Only fork-unavailability (macOS/Windows dev hosts) falls back.
                # The run is then correctly NOT reportable, and says why rather
                # than quietly measuring a different topology.
                print(f"  FSN pool unavailable ({exc}); falling back to the "
                      f"single-interpreter path — NOT reportable", flush=True)
        return self._replay_single_interpreter(deployment, requests, concurrency)

    def _replay_multiprocess(
        self, deployment: Deployment, requests, concurrency: int
    ) -> _WorkloadOutcome:
        """Each FSN in its own OS process, as skill.md requires.

        The scheduler still chooses the node in the parent — that decision IS
        the thing Exp. 7-8 ablate. What changes is that the chosen node then
        executes in its own process, so nodes genuinely contend for cores and
        per-node utilization can actually differ.
        """
        scheduler = aass_mod.Scheduler(
            self.variant, config=self.config, reportable=False
        )
        node_ids = [node.node_id for node in deployment.nodes]
        by_id = {node.node_id: node for node in deployment.nodes}
        rejected = 0
        forwards = 0
        dispatched = 0
        latency_starts: Dict[int, int] = {}
        collected: List[fsn_pool.NodeOutcome] = []
        peak_depth = 0

        with fsn_pool.FogSearchNodePool(deployment.nodes) as pool:
            self._worker_pids = pool.worker_pids
            started = time.perf_counter()
            for token, decision in requests:
                request = aass_mod.SearchRequest(
                    tokens=token.tokens,
                    authorized=decision.authorized_shards,
                    vid_u=token.vid_u,
                    query_versions=decision.query_versions,
                )
                try:
                    selection = scheduler.select(deployment.nodes, request)
                except aass_mod.SchedulerError:
                    rejected += 1
                    continue
                latency_starts[dispatched] = time.perf_counter_ns()
                # Enqueue BEFORE dispatch, so the depth the next select() reads
                # already includes this request. The parent's node objects carry
                # the queue the scheduler reasons about; the worker holds the
                # index. Both are the same node, split across the fork.
                depth = selection.node.enqueue(str(dispatched))
                if depth > peak_depth:
                    peak_depth = depth
                pool.dispatch(
                    dispatched, selection.node.node_id,
                    token.tokens,
                    # The shards M_Q gave THIS node, not the whole of S_Q.
                    # Phase VI Step 4: "Each selected FSN searches only its
                    # assigned authorized shard". Handing it S_Q would have it
                    # filter on pairs another node was assigned -- harmless to
                    # the result (its bitmaps for those pairs are empty) and
                    # wrong as a measurement, since the bitmap union it walks is
                    # the C_j^index the scheduler costed it for.
                    selection.assignment.shards_for(selection.node.node_id)
                    or decision.authorized_shards,
                    # Empty for Option D, whose H(w) token carries no policy
                    # and so has nothing to group by.
                    groups=getattr(token, "groups", None) or None,
                )
                dispatched += 1
                # Retire what has finished, so depth falls as well as rises.
                for outcome in pool.drain():
                    by_id[outcome.node_id].dequeue()
                    collected.append(outcome)
                # §VI Exp. 8(c): "a cross-node forward occurs when a scheduler
                # assigns a required shard to an FSN that does not maintain it".
                # That is a property of M_Q, so it is counted where M_Q is
                # built -- Algorithm 1's `S notin S_j -> continue` makes it 0 for
                # `aass` by construction, while the three oblivious variants
                # place shards without consulting S_j and pay for it.
                #
                # It was previously counted per authorized DOMAIN against the one
                # node the scheduler returned, which at d = m = 4 gave k-1 for
                # every variant -- a constant, and the reason the metric was
                # dropped. The per-shard assignment restores it as evidence.
                forwards += selection.assignment.forwards
            tail = pool.collect(dispatched - len(collected))
            # Retire the tail too. Every enqueue must have its dequeue or the
            # depth carries over into the next replay, and prepare()'s ramp plus
            # 30 measured runs would leave `least_loaded` and `C_j^queue`
            # reading a queue that only ever grew.
            for outcome in tail:
                by_id[outcome.node_id].dequeue()
            outcomes = collected + tail
            wall = time.perf_counter() - started

        latencies = [o.service_ns / 1e6 for o in outcomes if o.ok]
        rejected += sum(1 for o in outcomes if not o.ok)

        window_ns = max(1, int(wall * 1e9))
        util_map = fsn_pool.utilization_by_node(outcomes, node_ids, window_ns)
        utilizations = list(util_map.values())
        ordered = sorted(latencies) or [0.0]
        return _WorkloadOutcome(
            throughput=(len(latencies) / wall) if wall > 0 else 0.0,
            p50_ms=ordered[len(ordered) // 2],
            p95_ms=ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
            rejected=rejected,
            utilization_stddev=(
                statistics.pstdev(utilizations) if len(utilizations) > 1 else 0.0
            ),
            max_utilization=max(utilizations) if utilizations else 0.0,
            cross_node_forwards=forwards,
            max_queue_depth=peak_depth,
        )

    def _replay_single_interpreter(
        self, deployment: Deployment, requests, concurrency: int
    ) -> _WorkloadOutcome:
        scheduler = aass_mod.Scheduler(
            self.variant, config=self.config, reportable=False
        )
        latencies: List[float] = []
        rejected = 0
        forwards = 0
        peak_depth = 0
        started = time.perf_counter()
        for token, decision in requests:
            request = aass_mod.SearchRequest(
                tokens=token.tokens,
                authorized=decision.authorized_shards,
                vid_u=token.vid_u,
                query_versions=decision.query_versions,
            )
            try:
                selection = scheduler.select(deployment.nodes, request)
            except aass_mod.SchedulerError:
                rejected += 1
                continue
            began = time.perf_counter_ns()
            depth = selection.node.enqueue(str(len(latencies)))
            if depth > peak_depth:
                peak_depth = depth
            try:
                search_mod.execute_search(
                    selection.node,
                    token.tokens,
                    # M_Q's shards for this node — see the multiprocess path.
                    selection.assignment.shards_for(selection.node.node_id)
                    or decision.authorized_shards,
                )
            except search_mod.SearchRejected:
                selection.node.dequeue()
                rejected += 1
                continue
            selection.node.dequeue()
            latencies.append((time.perf_counter_ns() - began) / 1e6)
            # Same forward count as the multiprocess path -- see there.
            forwards += selection.assignment.forwards
        wall = time.perf_counter() - started

        window_ns = max(1, int(wall * 1e9))
        utilizations = [node.utilization(window_ns) for node in deployment.nodes]
        ordered = sorted(latencies) or [0.0]
        return _WorkloadOutcome(
            throughput=(len(latencies) / wall) if wall > 0 else 0.0,
            p50_ms=ordered[len(ordered) // 2],
            p95_ms=ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))],
            rejected=rejected,
            utilization_stddev=(
                statistics.pstdev(utilizations) if len(utilizations) > 1 else 0.0
            ),
            max_utilization=max(utilizations) if utilizations else 0.0,
            cross_node_forwards=forwards,
            max_queue_depth=peak_depth,
        )

    def _population(self, deployment: Deployment) -> List[Any]:
        """Data Users spanning 1..d authorized domains, no domain favoured.

        A single user authorized across every domain — which is what this was —
        makes the workload degenerate: every request carries the same authorized
        shard set, so the arms differ only in queue state. Varying the scope is
        what lets ``cross_node_forwards`` separate them, now that Algorithm 1
        assigns per shard and only AASS applies the ``S notin S_j`` guard.

        (This paragraph previously blamed ``C_j^auth = |P_Q|``, a fifth cost
        term of the previous manuscript revision. ``eq:search-cost`` has four
        and the code was aligned to it on 2026-09-07.)

        **Benchmark choice, not published.** Neither §VI nor skill.md fixes how many
        domains one query spans; §VI fixes only d=4. Uniform over subset sizes
        1..d with the starting domain rotated is the neutral choice — it spans
        the range from single-domain queries (where authorization locality
        decides everything) to all-domain queries (where it cannot matter), and
        rotating the offset makes every domain appear equally often, so no FSN is
        structurally favoured.
        """
        domains = tuple(deployment.domains)
        # Computed ONCE. _enrol() rescans deployment.records per domain per user,
        # which at N=10^5 over 16 users would be ~10^7 record comparisons.
        policies_by_domain: Dict[str, Tuple[str, ...]] = {}
        for domain in domains:
            policies_by_domain[domain] = tuple(sorted({
                r["record"].policy_id
                for r in deployment.records
                if r["record"].domain == domain
            }))

        population = []
        for size in range(1, len(domains) + 1):
            for offset in range(len(domains)):
                subset = sorted(
                    domains[(offset + i) % len(domains)] for i in range(size)
                )
                uid = f"DU-d{size}-o{offset}"
                authority_ids = [
                    deployment.authorities[d].authority_id for d in subset
                ]
                attributes = sorted(
                    attr for d in subset
                    for attr in deployment.authorities[d].attributes[:3]
                )
                profile = profile_mod.build_profile_from_aim(
                    deployment.aim, uid=uid,
                    authority_ids=authority_ids, attributes=attributes,
                )
                resolver = authz_mod.MappingPolicyResolver({
                    (uid, d): policies_by_domain[d] for d in subset
                })
                population.append(
                    (profile, authority_ids, attributes, resolver, tuple(subset))
                )
        return population

    def _ramp(self, deployment: Deployment, requests, concurrency: int) -> None:
        """global.yaml: "Exp. 7-8 warm after a 30 s ramp."

        Here rather than in ``measure`` because ``run_point`` calls ``prepare``
        once per point and excludes it from every timing — which is what "warm
        AFTER a ramp" means: ramp once, then take the 10 runs on a warm system.
        Putting it in ``measure`` would ramp 10 times per point and cost ~1.7 h
        across the campaign to measure the same steady state.
        """
        if not requests:
            return
        if RAMP_SECONDS <= 0:
            return
        deadline = time.perf_counter() + RAMP_SECONDS
        while time.perf_counter() < deadline:
            self.replay(deployment, requests, concurrency)

    def prepare(self, value: Any) -> Any:
        """Build the deployment, RECORD the arrival trace, then ramp.

        All variants and both experiments see the same workload — so the
        request list is materialised here and replayed, not regenerated per
        variant. Identical in CONTENT, not in bytes: every SearchToken carries
        a fresh random nonce, which must vary. Locked by
        test_exp7_and_exp8_record_the_same_arrival_trace, which digests the
        trace excluding the nonce, and by
        test_search_token_nonce_is_fresh_per_token, which pins that the nonce
        itself is never reproducible.
        """
        concurrency = int(value)
        # global.yaml fixes index_size at 10^5 for every experiment that does not
        # sweep it, and Exp. 7-8 sweep concurrency. This built `records=32` —
        # 8 entries per shard, 3,125x under the default. Measured consequence:
        # execute_search is linear in shard size while select() is flat, so at
        # 32 records the scheduler cost 23.8us against a 9.0us search and no
        # variant could saturate a node, because the work unit was cheaper than
        # the IPC round-trip delivering it. At 8,000 records the same search is
        # 297us. Sized as Exp. 2 sizes it, so "N" means the same thing in both.
        # N IN RECORDS, as Exp. 2 sizes it and as §VI's axis reads.
        #
        # This divided by `keywords_per_record`, so at the frozen corpus's
        # |W_i| = 31.70 the default `index_size: 100000` became ~3,125 records
        # — a 32x smaller index than Exp. 2 builds at the same configured value.
        # The comment claimed "sized as Exp. 2 sizes it", which stopped being
        # true when Exp. 2 dropped its own division on 2026-09-07.
        record_count = max(1, int(self.config.defaults.index_size))
        deployment = build_deployment(
            config=self.config, source=self.source, records=record_count
        )
        population = self._population(deployment)
        by_domain: Dict[str, List[Any]] = {d: [] for d in deployment.domains}
        for entry in deployment.records:
            by_domain[entry["record"].domain].append(entry)

        requests = []
        for index in range(concurrency):
            profile, authority_ids, attributes, resolver, subset = (
                population[index % len(population)]
            )
            # Draw from a domain this user actually holds, or the AIM rejects
            # the request and the trace silently shrinks.
            domain = subset[index % len(subset)]
            pool_for_domain = by_domain[domain]
            if not pool_for_domain:
                continue
            record = pool_for_domain[index % len(pool_for_domain)]
            # q KEYWORDS, per global.yaml and §VI's "each query contains five
            # keywords". This was `[keywords[0]]` -- one keyword, uncommented --
            # so Exp. 7's throughput and Exp. 8's spread were both measured on a
            # q=1 workload and reported against a paper that says 5. Same class
            # as the 2026-09-03 Exp. 2 defect: the number was real, the workload
            # was not the published one. Deduplicated because
            # generate_search_token rejects a repeated keyword, and a record can
            # carry the same keyword twice.
            query_keywords = list(dict.fromkeys(record["record"].keywords))[
                : self.config.defaults.keywords_per_query
            ]
            token = token_mod.generate_search_token(
                deployment.scheme, profile, query_keywords
            )
            decision = authz_mod.verify_search_request(
                deployment.aim, token, profile,
                authority_ids=authority_ids, attributes=attributes,
                resolver=resolver,
            )
            if decision.accepted:
                requests.append((token, decision))
        requests = tuple(requests)
        self._ramp(deployment, requests, concurrency)
        return dict(
            deployment=deployment, requests=requests, concurrency=concurrency
        )


@dataclass
class Exp7Throughput(SchedulerAblation):
    """Exp. 7: throughput (queries/s), with p50/p95 and rejections."""

    name: str = "exp7_search_throughput"
    number: int = 7
    variable: str = "concurrency"
    values: Tuple[Any, ...] = ()
    primary: MetricSpec = MetricSpec("throughput", QPS)
    secondaries: Tuple[MetricSpec, ...] = (
        MetricSpec("latency_p95", MS),
        MetricSpec("rejected", COUNT),
    )

    def __post_init__(self) -> None:
        if not self.values:
            self.values = tuple(self.config.experiment("exp7").values)

    def measure(self, prepared: Any) -> Sample:
        outcome = self.replay(
            prepared["deployment"], prepared["requests"], prepared["concurrency"]
        )
        return Sample(
            primary=outcome.throughput,
            secondaries={
                "latency_p95": outcome.p95_ms,
                "rejected": float(outcome.rejected),
            },
        )


@dataclass
class Exp8LoadBalance(SchedulerAblation):
    """Exp. 8: the utilization spread of the SAME runs Exp. 7 measures."""

    name: str = "exp8_load_balance"
    number: int = 8
    variable: str = "concurrency"
    values: Tuple[Any, ...] = ()
    primary: MetricSpec = MetricSpec("utilization_stddev", COUNT)
    secondaries: Tuple[MetricSpec, ...] = (
        MetricSpec("max_node_utilization", COUNT),
        # §VI's Fig. 8 is THREE panels: (a) utilization std. dev., (b) max FSN
        # utilization, (c) cross-node forwards. The order here IS the panel
        # order, because Plots/generate_plots.py indexes panels positionally
        # (metric 0 = primary, 1 = first secondary, 2 = second).
        #
        # `cross_node_forwards` was dropped on 2026-08-30 as invariant: the
        # scheduler returned one node per request and the count was measured per
        # authorized DOMAIN, so at d = m = 4 it was k-1 under every variant (600
        # for all four arms over 400 requests). It is restored because Algorithm
        # 1 is now implemented per SHARD: `aass` skips ineligible nodes and
        # scores 0 by construction, while `no_lb`, `round_robin` and
        # `least_loaded` place shards without consulting S_j. The metric varies
        # across arms again, which is what §VI claims for it.
        MetricSpec("cross_node_forwards", COUNT),
        # Peak queue depth measures "prevents node congestion". Kept as a third
        # secondary, past the panels the figure draws.
        MetricSpec("max_queue_depth", COUNT),
    )

    def __post_init__(self) -> None:
        if not self.values:
            self.values = tuple(self.config.experiment("exp8").values)

    def measure(self, prepared: Any) -> Sample:
        outcome = self.replay(
            prepared["deployment"], prepared["requests"], prepared["concurrency"]
        )
        return Sample(
            primary=outcome.utilization_stddev,
            secondaries={
                "max_node_utilization": outcome.max_utilization,
                "cross_node_forwards": float(outcome.cross_node_forwards),
                "max_queue_depth": float(outcome.max_queue_depth),
            },
        )


# ===========================================================================
# Exp. 9 — Verification Granularity under Tampering
# ===========================================================================
# NOT AN EXPERIMENT IN THE MANUSCRIPT. Section VI has eight experiments and no
# Exp. 9: this arm's numbers are panel (b) of the manuscript's Exp. 4 figure
# ("invalid-result localization and valid-result retention"), while Exp4Verification
# supplies panel (a). The split is a HARNESS split, kept because the two halves
# sweep different variables -- Exp. 4 sweeps r at zero tampering, this sweeps the
# tamper count t at pinned r -- and a single sweep cannot produce both. See
# ``Plots/generate_plots.py``, where ExperimentSpec(4, ...) draws panel (b) with
# ``folder="exp4_verification_overhead__granularity"``.


EXPERIMENTS = {
    1: Exp1TrapdoorGeneration,
    2: Exp2SearchLatency,
    3: Exp3CrossDomain,
    4: Exp4Verification,
    5: Exp5KeywordUpdate,
    6: Exp6AuthorizationSync,
    7: Exp7Throughput,
    8: Exp8LoadBalance,
}


def build_experiment(
    number: int,
    config: scheme_config.Configuration,
    source: Optional[SyntheticRecordSource] = None,
    variant: Optional[str] = None,
):
    """Instantiate one experiment by its global.yaml number.

    ``variant`` selects the scheduler for Exp. 7-8, which global.yaml defines as a
    four-way ablation (no_lb / round_robin / least_loaded / aass). It was
    reachable only by editing the dataclass default, so every campaign so far
    measured `aass` alone and the ablation the paper claims had never been run.

    It is ignored for Exp. 1-6, and deliberately so for Exp. 6: that experiment
    measures DIAS propagation -- commitment recomputation, Merkle path update,
    selective FSN delivery -- and the scheduler decides which FSN serves a
    QUERY. It plays no part in propagating an authorization change, so running
    four variants there would measure the same thing four times.
    """
    if number not in EXPERIMENTS:
        raise KeyError(
            f"no experiment {number}; Section VI defines 1-8. The "
            f"tamper-granularity sweep is Exp. 4's `granularity` arm, not an "
            f"experiment of its own"
        )
    kwargs = dict(config=config, source=source or SyntheticRecordSource())
    if variant and "variant" in {f.name for f in dataclasses.fields(EXPERIMENTS[number])}:
        kwargs["variant"] = variant
    return EXPERIMENTS[number](**kwargs)


__all__ = [
    "SyntheticRecordSource",
    "CorpusRecordSource",
    "Deployment",
    "build_deployment",
    "EXPERIMENTS",
    "build_experiment",
    "Exp1TrapdoorGeneration",
    "Exp2SearchLatency",
    "Exp3CrossDomain",
    "Exp4Verification",
    "Exp5KeywordUpdate",
    "Exp6AuthorizationSync",
    "Exp7Throughput",
    "Exp8LoadBalance",
]
