from __future__ import annotations

from typing import Literal

from src.db.models import SentOrder

EventType = Literal["confirmed", "updated", "cancelled", "refunded", "skip"]


def classify_event(order: dict, stored: SentOrder | None) -> EventType:
    """Classify an Odoo order event relative to its stored state.

    Args:
        order: Raw order dict from Odoo (must contain 'name', 'state', 'write_date').
        stored: The last SentOrder row for this order, or None if first encounter.

    Returns:
        EventType literal indicating what kind of event this is.
    """
    name = order.get("name", "") or ""
    state = order.get("state", "") or ""
    write_date = order.get("write_date", "") or ""

    if stored is None:
        if name.startswith("REF-"):
            return "refunded"
        return "confirmed"

    if stored.odoo_write_date == write_date:
        return "skip"

    if state == "cancel" and stored.last_state != "cancel":
        return "cancelled"

    return "updated"
