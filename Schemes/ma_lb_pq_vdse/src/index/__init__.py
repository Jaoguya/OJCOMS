"""Phase IV — Policy-Bound Dynamic Search Index (PDSI) construction.

* ``extract.py`` — Step 1, ``W_i`` and ``Meta_i`` projected from a corpus record.
* ``dsi.py`` — Step 3, index entries, the DSI, and authorization bitmaps.
* ``commit.py`` — Steps 4-5, the per-record Merkle tree, ``Root_i``, ``Commit_i``.

Step 2 (``T_j``, the policy-bound keyword encoding) is absent: the matching
relation between the index token and the query token is an open author decision.
The Phase IV design note set out the discrepancy, the five options, and why §VI
 rules out the exact-match reading. ``dsi.py`` therefore takes tokens as
opaque bytes and never builds one, so the decision changes one module that does
not exist yet rather than this whole phase.

Phase IV is offline: ``skill.md`` excludes index construction from every
measurement. What it produces is the object Exp. 2, 3, 5 and 7-8 measure.
"""
