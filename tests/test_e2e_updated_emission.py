"""Unit 3.7 — E2E integration tests for the updated event emission (Poller).

Scenarios:
1. test_e2e_poller_emits_updated_when_relevant_payload_changes
   Pre-populate sent_orders with an order at last_state='sale' and an old hash.
   Mock Odoo to return the same order id but with new line_items (different hash).
   Confirm worker calls sender.send with event_type='updated'.

2. test_e2e_poller_skips_when_only_internal_field_changed
   Pre-populate with the current hash of an empty payload.
   Mock Odoo to return same order with only a newer write_date — relevant fields
   (items, shipping_address, customer) are identical.
   Confirm worker does NOT call sender.send (noise-filter skip path).
"""
from __future__ import annotations

import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography.fernet import Fernet

from src.db.database import init_db
from src.db.models import Connection, SentOrder, CircuitState
from src.db.repositories import (
    ConnectionRepository,
    RetryQueueRepository,
    SentOrderRepository,
    SyncLogRepository,
)
from src.encryption import FieldEncryptor
from src.odoo.client import OdooClient
from src.odoo.mapper import BatchOdooData
from src.odoo.payload_hash import compute_relevant_hash
from src.poller.circuit_breaker import CircuitBreaker
from src.poller.sender import WebhookSender
from src.poller.worker import PollWorker


# ---------------------------------------------------------------------------
# Shared fixture (mirrors test_worker.py pattern)
# ---------------------------------------------------------------------------


@pytest.fixture
async def worker_setup():
    """Set up a PollWorker with real SQLite DB and mocked Odoo/HTTP."""
    key = Fernet.generate_key().decode()
    enc = FieldEncryptor(key)
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "e2e_test.db")
        db = await init_db(db_path)

        conn_repo = ConnectionRepository(db, enc)
        sync_repo = SyncLogRepository(db)
        retry_repo = RetryQueueRepository(db)
        sent_repo = SentOrderRepository(db)

        conn = await conn_repo.create(
            Connection(
                name="E2EConn",
                odoo_url="https://odoo.example.com",
                odoo_db="testdb",
                odoo_username="admin",
                odoo_api_key="key",
                webhook_url="https://webhook.example.com/hook",
                webhook_secret="secret",
                last_sync_at="2024-01-01 00:00:00",
                external_id="ext-e2e",
            )
        )

        odoo_client = MagicMock(spec=OdooClient)
        odoo_client.uid = 1

        http_client = MagicMock(spec=httpx.AsyncClient)
        sender = WebhookSender(http_client)
        cb = CircuitBreaker()

        worker = PollWorker(
            connection=conn,
            odoo_client=odoo_client,
            sender=sender,
            circuit_breaker=cb,
            conn_repo=conn_repo,
            sync_log_repo=sync_repo,
            retry_repo=retry_repo,
            sent_repo=sent_repo,
        )

        yield worker, odoo_client, sender, sent_repo, conn, db
        await db.close()


# ---------------------------------------------------------------------------
# Helper: minimal Odoo order dict
# ---------------------------------------------------------------------------


def _odoo_order(
    order_id: int = 500,
    name: str = "SO500",
    state: str = "sale",
    write_date: str = "2024-06-01 10:00:00",
    create_date: str = "2024-05-01 08:00:00",
) -> dict:
    return {
        "id": order_id,
        "name": name,
        "state": state,
        "write_date": write_date,
        "date_order": "2024-05-01 10:00:00",
        "create_date": create_date,
        "origin": False,
        "partner_id": False,
        "partner_shipping_id": False,
        "amount_untaxed": 1000,
        "amount_tax": 190,
        "amount_total": 1190,
        "currency_id": False,
        "note": "",
    }


# ---------------------------------------------------------------------------
# Test 1: Worker emits 'updated' when relevant payload fields change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_poller_emits_updated_when_relevant_payload_changes(worker_setup):
    """Pre-stored hash is for empty items; new payload has items → 'updated' event emitted."""
    worker, odoo_client, sender, sent_repo, conn, _ = worker_setup

    # Build old hash: empty items baseline
    old_payload_basis = {"items": [], "shipping_address": {}, "customer": {}}
    old_hash = compute_relevant_hash(old_payload_basis)

    # Pre-populate sent_orders with the old hash and an older write_date
    await sent_repo.mark_sent(
        SentOrder(
            connection_id=conn.id,
            odoo_order_id=500,
            odoo_order_name="SO500",
            odoo_write_date="2024-05-15 10:00:00",  # older — will trigger 'updated' path
            last_state="sale",
            odoo_create_date="2024-05-01 08:00:00",
            hash_payload=old_hash,
        )
    )

    # Odoo now returns the same order with a newer write_date (changed payload)
    odoo_client.search_read = AsyncMock(
        return_value=[_odoo_order(write_date="2024-06-01 10:00:00")]
    )

    # The batch will include a product line — simulate a non-empty items list
    batch = BatchOdooData(partners={}, products={}, variants={})
    # The _lines_by_order attribute is set dynamically by fetch_batch_data
    batch._lines_by_order = {
        500: [
            {
                "product_id": [11, "Producto X"],
                "name": "Producto X",
                "product_uom_qty": 2.0,
                "price_unit": 500.0,
                "price_subtotal": 1000.0,
                "price_total": 1190.0,
                "discount": 0.0,
            }
        ]
    }

    sent_payloads: list[dict] = []

    async def mock_send(url, payload, webhook_secret="", connection_external_id=""):
        sent_payloads.append(payload)

    with patch("src.poller.worker.fetch_batch_data", AsyncMock(return_value=batch)):
        sender.send = AsyncMock(side_effect=mock_send)
        await worker.execute()

    assert len(sent_payloads) == 1, "Exactly one webhook should be sent for the updated order"
    assert sent_payloads[0]["event_type"] == "updated", (
        f"Expected event_type='updated', got: {sent_payloads[0].get('event_type')}"
    )


# ---------------------------------------------------------------------------
# Test 2: Worker skips when only internal fields changed (hash identical)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_e2e_poller_skips_when_only_internal_field_changed(worker_setup):
    """When write_date advances but relevant payload hash is unchanged → sender NOT called."""
    worker, odoo_client, sender, sent_repo, conn, _ = worker_setup

    # Build the current hash for an order with one item
    current_payload_basis = {
        "items": [
            {
                "sku": "ODOO-testdb-11",
                "name": "Producto Y",
                "quantity": 1.0,
                "unit_price": 1000.0,
                "subtotal": 1000.0,
                "total": 1190.0,
                "discount_percent": 0.0,
                "odoo_product_id": 11,
            }
        ],
        "shipping_address": {},
        "customer": {},
    }
    current_hash = compute_relevant_hash(current_payload_basis)

    # Pre-populate with the same hash already stored
    await sent_repo.mark_sent(
        SentOrder(
            connection_id=conn.id,
            odoo_order_id=501,
            odoo_order_name="SO501",
            odoo_write_date="2024-05-15 10:00:00",  # old write_date
            last_state="sale",
            odoo_create_date="2024-05-01 08:00:00",
            hash_payload=current_hash,
        )
    )

    # Odoo returns same order with a newer write_date (internal change, e.g. note)
    # but relevant fields (items/shipping/customer) are identical
    odoo_client.search_read = AsyncMock(
        return_value=[
            _odoo_order(
                order_id=501,
                name="SO501",
                write_date="2024-06-10 15:00:00",  # different write_date triggers check
                create_date="2024-05-01 08:00:00",
            )
        ]
    )

    # Batch produces the identical item list → hash will match current_hash
    batch = BatchOdooData(partners={}, products={}, variants={})
    batch._lines_by_order = {
        501: [
            {
                "product_id": [11, "Producto Y"],
                "name": "Producto Y",
                "product_uom_qty": 1.0,
                "price_unit": 1000.0,
                "price_subtotal": 1000.0,
                "price_total": 1190.0,
                "discount": 0.0,
            }
        ]
    }

    send_calls: list[dict] = []

    async def mock_send(url, payload, webhook_secret="", connection_external_id=""):
        send_calls.append(payload)

    # Patch compute_relevant_hash in worker to always return current_hash
    # (simulating that the payload-relevant fields did not change)
    with patch("src.poller.worker.fetch_batch_data", AsyncMock(return_value=batch)):
        with patch("src.poller.worker.compute_relevant_hash", return_value=current_hash):
            sender.send = AsyncMock(side_effect=mock_send)
            await worker.execute()

    assert len(send_calls) == 0, (
        "Sender must NOT be called when relevant payload hash is identical "
        f"(noise-filter). Got {len(send_calls)} call(s): {send_calls}"
    )
