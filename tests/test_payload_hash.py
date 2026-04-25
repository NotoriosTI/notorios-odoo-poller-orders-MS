from __future__ import annotations

import pytest

from src.odoo.payload_hash import compute_relevant_hash


def test_hash_is_deterministic():
    """Same payload always returns the same sha256 hex string (length 64)."""
    payload = {
        "items": [{"sku": "ABC", "qty": 2, "price": 10.0}],
        "shipping_address": {"city": "Santiago"},
        "customer": {"name": "Juan"},
        "event_type": "updated",
        "order_id": "SO001",
    }
    h1 = compute_relevant_hash(payload)
    h2 = compute_relevant_hash(payload)
    assert h1 == h2
    assert len(h1) == 64
    assert all(c in "0123456789abcdef" for c in h1)


def test_hash_changes_on_relevant_field():
    """Changing items qty changes the hash."""
    base = {
        "items": [{"sku": "ABC", "qty": 1}],
        "shipping_address": {},
        "customer": {},
    }
    modified = {
        "items": [{"sku": "ABC", "qty": 99}],
        "shipping_address": {},
        "customer": {},
    }
    assert compute_relevant_hash(base) != compute_relevant_hash(modified)


def test_hash_is_key_order_independent():
    """Different key order in the payload dict produces the same hash."""
    payload_a = {
        "items": [{"sku": "X", "qty": 3}],
        "customer": {"email": "a@b.com"},
        "shipping_address": {"city": "Lima"},
    }
    payload_b = {
        "shipping_address": {"city": "Lima"},
        "items": [{"sku": "X", "qty": 3}],
        "customer": {"email": "a@b.com"},
    }
    assert compute_relevant_hash(payload_a) == compute_relevant_hash(payload_b)
