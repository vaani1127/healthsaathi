"""Anchoring study (SPEC 8, C3) and checkpoint cost.

    uv run python -m e2d_experiments.anchoring snapshot   # timestamps from the database
    uv run python -m e2d_experiments.anchoring enrich     # gas and block times from the chains
    uv run python -m e2d_experiments.anchoring bench      # proof size and tree build time
    uv run python -m e2d_experiments.anchoring report     # tables and figures

The snapshot holds no patient data: clinic ids (synthetic demo clinics), leaf positions,
timestamps, tree sizes and transaction references only.
"""
