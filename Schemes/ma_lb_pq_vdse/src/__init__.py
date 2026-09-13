"""MA-LB-PQ-VDSE — the proposed framework.

Implements the eight protocol phases of the manuscript
(``Overleaf/MA-LB-PQ-VDSE.tex``). Module-to-phase mapping is in
``skill.md``; the Phase I-II build order and its verification criteria are
in ``../PHASE_I_II_PLAN.md``.

Scope boundary: primitives the paper *cites* — SHA-256, HMAC,
AES-256-GCM, HKDF, Merkle, Bloom, ML-KEM, pairings — come from
``Common/crypto`` so every scheme measures the same primitive cost. What this
package contains is what the paper *contributes*: PDSI, AASS, and DIAS. No
module here is shared with a baseline.
"""
