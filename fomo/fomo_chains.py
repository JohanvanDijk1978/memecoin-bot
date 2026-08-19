"""Shared request metadata required for FOMO's multi-chain API responses."""

from __future__ import annotations


SUPPORTED_CHAIN_IDS = (1, 56, 143, 4663, 8453, 1399811149)
SUPPORTED_CHAINS_HEADER = ",".join(str(chain_id) for chain_id in SUPPORTED_CHAIN_IDS)
