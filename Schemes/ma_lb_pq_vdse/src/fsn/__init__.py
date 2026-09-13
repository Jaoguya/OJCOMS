"""Fog Search Nodes — distributed edge servers holding index shards.

Phase I Step 4 initializes ``F = {FSN_1, ..., FSN_m}``. Each node maintains its
own shard, its synchronized authorization version ``VID_j``, and its request
queue, and holds no reference to any other node — global.yaml requires them to
become independent processes, which Phase VI introduces.
"""
