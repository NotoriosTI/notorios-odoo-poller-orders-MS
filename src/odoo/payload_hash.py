from __future__ import annotations

import hashlib
import json
from typing import Any


def compute_relevant_hash(payload: dict[str, Any]) -> str:
    """Compute a deterministic sha256 hash of the relevant fields in a webhook payload.

    Only items, shipping_address and customer are considered — fields that, when
    unchanged, mean the update is noise and can be skipped.

    Returns:
        64-char lowercase hex sha256 digest.
    """
    relevant = {
        "items": payload.get("items", []),
        "shipping_address": payload.get("shipping_address", {}),
        "customer": payload.get("customer", {}),
    }
    canonical = json.dumps(relevant, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()
