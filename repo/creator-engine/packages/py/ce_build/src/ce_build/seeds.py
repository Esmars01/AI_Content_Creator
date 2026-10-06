"""Seeds (§12.5).

- default seed = hash(`generation.seed_namespace`, node_key, take_index) — never a version id, so
  edits keep caches and duplicated videos (same namespace) reuse their source's cache;
- spec seed override = `generation.seed_overrides[node_key]` (an explicit user regenerate);
- attempt seed: infrastructure retries reuse the seed; QC retries use hash(base_seed, retry_n);
- effective seed: the seed of the accepted artifact, recorded in the BuildManifest.

The cache key uses the seed basis (default or override); a QC retry's accepted artifact is stored
under that same key with its effective seed (§12.2, DECISIONS D28).
"""

from __future__ import annotations

import hashlib

__all__ = ["MAX_SEED", "attempt_seed", "default_seed", "seed_basis"]

MAX_SEED = 2**31 - 1


def _hash_int(*parts: object) -> int:
    digest = hashlib.sha256("\x1f".join(str(p) for p in parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % MAX_SEED


def default_seed(seed_namespace: object, node_key: str, take_index: int | None) -> int:
    return _hash_int("ce-seed-v1", seed_namespace, node_key, take_index or 0)


def seed_basis(seed_namespace: object, node_key: str, take_index: int | None, overrides: dict[str, int]) -> int:
    return int(overrides[node_key]) if node_key in overrides else default_seed(seed_namespace, node_key, take_index)


def attempt_seed(base_seed: int, qc_retry: int) -> int:
    """QC retries vary the seed deterministically; infrastructure retries (qc_retry=0) keep it."""
    return base_seed if qc_retry == 0 else _hash_int("ce-attempt-v1", base_seed, qc_retry)
