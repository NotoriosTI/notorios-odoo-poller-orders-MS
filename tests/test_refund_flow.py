"""Unit 4.4 — Refund flow tests for the poller.

Scenarios:
1. test_classify_ref_order_with_history_is_refunded
   A REF-* order seen again (stored exists, write_date changed) → still classified
   as 'refunded', NOT 'updated'.

2. test_classify_ref_order_with_history_same_write_date_is_skip
   A REF-* order seen again with same write_date → 'skip' (idempotent).

3. test_mapper_refunded_payload_includes_refund_ids
   map_order_to_webhook_payload with event_type='refunded' → payload contains
   'original_order_id' and 'refund_mirror_odoo_id'.

4. test_mapper_refunded_original_order_id_none_when_no_origin
   If order has no origin field, original_order_id=None.

5. test_e2e_poller_emits_refunded_for_ref_order
   Worker sees new sale.order with name='REF-SO100' and origin='SO100'.
   Odoo lookup for 'SO100' returns id=100.
   Worker emits webhook with event_type='refunded', original_order_id=100,
   refund_mirror_odoo_id=<REF order id>.

6. test_e2e_poller_does_not_emit_confirmed_for_ref_order
   Worker sees REF-* order → event_type != 'confirmed'.

7. test_e2e_poller_refunded_original_order_id_none_when_origin_not_found
   Worker sees REF-* order where Odoo search for original returns empty.
   original_order_id=None, but event is still emitted with event_type='refunded'.
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
from src.odoo.mapper import BatchOdooData, map_order_to_webhook_payload
from src.poller.circuit_breaker import CircuitBreaker
from src.poller.classifier import classify_event
from src.poller.sender import WebhookSender
from src.poller.worker import PollWorker


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stored_ref(
    order_id: int = 900,
    name: str = "REF-SO100",
    write_date: str = "2024-01-01 10:00:00",
) -> SentOrder:
    return SentOrder(
        connection_id=1,
        odoo_order_id=order_id,
        odoo_order_name=name,
        odoo_write_date=write_date,
        last_state="sale",
        odoo_create_date="2024-01-01 08:00:00",
    )


def _ref_order(
    order_id: int = 900,
    name: str = "REF-SO100",
    write_date: str = "2024-01-02 10:00:00",
    origin: str = "SO100",
) -> dict:
    return {
        "id": order_id,
        "name": name,
        "state": "sale",
        "write_date": write_date,
        "date_order": "2024-01-01 10:00:00",
        "create_date": "2024-01-01 08:00:00",
        "origin": origin,
        "partner_id": False,
        "partner_shipping_id": False,
        "amount_untaxed": 1000,
        "amount_tax": 190,
        "amount_total": 1190,
        "currency_id": False,
        "note": "",
    }


# ---------------------------------------------------------------------------
# Classifier tests (Unit 4.4 — classifier branch)
# ---------------------------------------------------------------------------


def test_classify_ref_order_with_history_is_refunded():
    """REF-* order seen again with different write_date -> 'refunded', not 'updated'."""
    order = {"name": "REF-SO100", "state": "sale", "write_date": "2024-01-02 12:00:00"}
    stored = _make_stored_ref(write_date="2024-01-01 10:00:00")
    result = classify_event(order, stored)
    assert result == "refunded", (
        f"Expected 'refunded' for REF-* order with history, got '{result}'"
    )


def test_classify_ref_order_with_history_same_write_date_is_skip():
    """REF-* order with same write_date as stored -> 'skip' (idempotent)."""
    order = {"name": "REF-SO100", "state": "sale", "write_date": "2024-01-01 10:00:00"}
    stored = _make_stored_ref(write_date="2024-01-01 10:00:00")
    result = classify_event(order, stored)
    assert result == "skip", (
        f"Expected 'skip' for REF-* order with same write_date, got '{result}'"
    )


# ---------------------------------------------------------------------------
# Mapper tests (Unit 4.4 — payload fields)
# ---------------------------------------------------------------------------


def test_mapper_refunded_payload_includes_refund_ids():
    """Refunded payload includes original_order_id and refund_mirror_odoo_id."""
    order = _ref_order(order_id=900, origin="SO100")
    batch = BatchOdooData(partners={}, products={}, variants={})
    batch._lines_by_order = {}

    payload = map_order_to_webhook_payload(
        order,
        batch,
        odoo_db="testdb",
        connection_id="ext-1",
        event_type="refunded",
        original_order_id=100,
    )

    assert payload["event_type"] == "refunded"
    assert payload["refund_mirror_odoo_id"] == 900, (
        "refund_mirror_odoo_id must be the REF- order's own Odoo id"
    )
    assert payload["original_order_id"] == 100, (
        "original_order_id must be the resolved numeric id of the original order"
    )
    assert payload["original_order_name"] == "SO100", (
        "original_order_name must be preserved from origin field"
    )


def test_mapper_refunded_original_order_id_none_when_no_origin():
    """When original_order_id is not resolved, payload has original_order_id=None."""
    order = _ref_order(order_id=900, origin="")
    batch = BatchOdooData(partners={}, products={}, variants={})
    batch._lines_by_order = {}

    payload = map_order_to_webhook_payload(
        order,
        batch,
        odoo_db="testdb",
        connection_id="ext-1",
        event_type="refunded",
        original_order_id=None,
    )

    assert payload["original_order_id"] is None
    assert payload["refund_mirror_odoo_id"] == 900


def test_mapper_non_refunded_does_not_include_refund_fields():
    """Normal 'confirmed' event does not include refund-specific fields."""
    order = {
        "id": 50,
        "name": "SO050",
        "state": "sale",
        "write_date": "",
        "date_order": "",
        "create_date": "",
        "origin": False,
        "partner_id": False,
        "partner_shipping_id": False,
        "amount_untaxed": 0,
        "amount_tax": 0,
        "amount_total": 0,
        "currency_id": False,
        "note": "",
    }
    batch = BatchOdooData(partners={}, products={}, variants={})
    batch._lines_by_order = {}

    payload = map_order_to_webhook_payload(
        order, batch, odoo_db="testdb", connection_id="ext-1", event_type="confirmed"
    )

    assert payload["event_type"] == "confirmed"
    assert "refund_mirror_odoo_id" not in payload
    assert "original_order_id" not in payload
    assert "original_order_name" not in payload


# ---------------------------------------------------------------------------
# E2E worker tests (Unit 4.4 — full poller flow)
# ---------------------------------------------------------------------------


@pytest.fixture
async def worker_setup():
    """Set up a PollWorker with real SQLite DB and mocked Odoo/HTTP."""
    key = Fernet.generate_key().decode()
    enc = FieldEncryptor(key)
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "refund_test.db")
        db = await init_db(db_path)

        conn_repo = ConnectionRepository(db, enc)
        sync_repo = SyncLogRepository(db)
        retry_repo = RetryQueueRepository(db)
        sent_repo = SentOrderRepository(db)

        conn = await conn_repo.create(
            Connection(
                name="RefundConn",
                odoo_url="https://odoo.example.com",
                odoo_db="testdb",
                odoo_username="admin",
                odoo_api_key="key",
                webhook_url="https://webhook.example.com/hook",
                webhook_secret="secret",
                last_sync_at="2024-01-01 00:00:00",
                external_id="ext-refund",
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


@pytest.mark.asyncio
async def test_e2e_poller_emits_refunded_for_ref_order(worker_setup):
    """Worker sees REF-SO100 (origin=SO100) → emits event_type='refunded'.

    original_order_id is resolved by searching Odoo for name='SO100'.
    refund_mirror_odoo_id is the id of the REF-* order itself (900).
    """
    worker, odoo_client, sender, sent_repo, conn, _ = worker_setup

    ref_order = _ref_order(order_id=900, name="REF-SO100", origin="SO100")

    # First call: main poll returns the REF- order
    # Second call: resolve original by name search → returns [{id:100, name:'SO100'}]
    odoo_client.search_read = AsyncMock(
        side_effect=[
            [ref_order],                                          # main poll
            [{"id": 100, "name": "SO100"}],                      # resolve original
        ]
    )

    empty_batch = BatchOdooData(partners={}, products={}, variants={})
    empty_batch._lines_by_order = {}

    sent_payloads: list[dict] = []

    async def mock_send(url, payload, webhook_secret="", connection_external_id=""):
        sent_payloads.append(payload)

    with patch("src.poller.worker.fetch_batch_data", AsyncMock(return_value=empty_batch)):
        sender.send = AsyncMock(side_effect=mock_send)
        await worker.execute()

    assert len(sent_payloads) == 1, (
        f"Expected 1 webhook, got {len(sent_payloads)}"
    )
    payload = sent_payloads[0]
    assert payload["event_type"] == "refunded", (
        f"Expected 'refunded', got '{payload['event_type']}'"
    )
    assert payload["original_order_id"] == 100, (
        f"Expected original_order_id=100, got {payload.get('original_order_id')}"
    )
    assert payload["refund_mirror_odoo_id"] == 900, (
        f"Expected refund_mirror_odoo_id=900, got {payload.get('refund_mirror_odoo_id')}"
    )
    assert payload["original_order_name"] == "SO100"


@pytest.mark.asyncio
async def test_e2e_poller_does_not_emit_confirmed_for_ref_order(worker_setup):
    """Worker sees REF-* order → event_type is never 'confirmed'."""
    worker, odoo_client, sender, sent_repo, conn, _ = worker_setup

    ref_order = _ref_order(order_id=901, name="REF-SO200", origin="SO200")

    odoo_client.search_read = AsyncMock(
        side_effect=[
            [ref_order],
            [{"id": 200, "name": "SO200"}],
        ]
    )

    empty_batch = BatchOdooData(partners={}, products={}, variants={})
    empty_batch._lines_by_order = {}

    sent_payloads: list[dict] = []

    async def mock_send(url, payload, webhook_secret="", connection_external_id=""):
        sent_payloads.append(payload)

    with patch("src.poller.worker.fetch_batch_data", AsyncMock(return_value=empty_batch)):
        sender.send = AsyncMock(side_effect=mock_send)
        await worker.execute()

    assert len(sent_payloads) == 1
    assert sent_payloads[0]["event_type"] != "confirmed", (
        "REF-* orders must never emit event_type='confirmed'"
    )


@pytest.mark.asyncio
async def test_e2e_poller_refunded_original_order_id_none_when_origin_not_found(
    worker_setup,
):
    """Worker sees REF-* but Odoo search for original returns empty → original_order_id=None.

    Event is still emitted with event_type='refunded' and a warning is logged.
    """
    worker, odoo_client, sender, sent_repo, conn, _ = worker_setup

    ref_order = _ref_order(order_id=902, name="REF-SO999", origin="SO999")

    odoo_client.search_read = AsyncMock(
        side_effect=[
            [ref_order],  # main poll
            [],            # original not found in Odoo
        ]
    )

    empty_batch = BatchOdooData(partners={}, products={}, variants={})
    empty_batch._lines_by_order = {}

    sent_payloads: list[dict] = []

    async def mock_send(url, payload, webhook_secret="", connection_external_id=""):
        sent_payloads.append(payload)

    with patch("src.poller.worker.fetch_batch_data", AsyncMock(return_value=empty_batch)):
        sender.send = AsyncMock(side_effect=mock_send)
        await worker.execute()

    assert len(sent_payloads) == 1, (
        "Event should still be emitted even when original_order_id cannot be resolved"
    )
    payload = sent_payloads[0]
    assert payload["event_type"] == "refunded"
    assert payload["original_order_id"] is None, (
        "original_order_id must be None when original order is not found in Odoo"
    )
    assert payload["refund_mirror_odoo_id"] == 902
