from __future__ import annotations

import pytest

from src.db.models import SentOrder
from src.poller.classifier import classify_event


def _make_stored(last_state: str = "sale", write_date: str = "2024-01-01 10:00:00") -> SentOrder:
    return SentOrder(
        connection_id=1,
        odoo_order_id=10,
        odoo_order_name="SO010",
        odoo_write_date=write_date,
        last_state=last_state,
        odoo_create_date="2024-01-01 08:00:00",
    )


def test_classify_confirmed_new_order():
    """New order (stored=None, normal name) -> confirmed."""
    order = {"name": "SO001", "state": "sale", "write_date": "2024-01-01 10:00:00"}
    assert classify_event(order, None) == "confirmed"


def test_classify_cancelled():
    """Order transitions from sale -> cancel -> classified as cancelled."""
    order = {"name": "SO010", "state": "cancel", "write_date": "2024-01-02 12:00:00"}
    stored = _make_stored(last_state="sale", write_date="2024-01-01 10:00:00")
    assert classify_event(order, stored) == "cancelled"


def test_classify_cancel_born_no_history():
    """Order arrives as cancel with no prior history -> confirmed (first seen)."""
    order = {"name": "SO010", "state": "cancel", "write_date": "2024-01-01 10:00:00"}
    assert classify_event(order, None) == "confirmed"


def test_classify_updated():
    """Known order with different write_date -> updated."""
    order = {"name": "SO010", "state": "sale", "write_date": "2024-01-02 12:00:00"}
    stored = _make_stored(last_state="sale", write_date="2024-01-01 10:00:00")
    assert classify_event(order, stored) == "updated"


def test_classify_skip():
    """Known order with same write_date -> skip."""
    order = {"name": "SO010", "state": "sale", "write_date": "2024-01-01 10:00:00"}
    stored = _make_stored(last_state="sale", write_date="2024-01-01 10:00:00")
    assert classify_event(order, stored) == "skip"


def test_classify_refunded_no_history():
    """New order with REF- prefix and no history -> refunded."""
    order = {"name": "REF-SO001", "state": "sale", "write_date": "2024-01-01 10:00:00"}
    assert classify_event(order, None) == "refunded"
