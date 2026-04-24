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
from src.poller.circuit_breaker import CircuitBreaker
from src.poller.sender import WebhookSender
from src.poller.worker import PollWorker


@pytest.fixture
async def worker_setup():
    """Set up a PollWorker with real DB and mocked Odoo/HTTP."""
    key = Fernet.generate_key().decode()
    enc = FieldEncryptor(key)
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "worker_test.db")
        db = await init_db(db_path)

        conn_repo = ConnectionRepository(db, enc)
        sync_repo = SyncLogRepository(db)
        retry_repo = RetryQueueRepository(db)
        sent_repo = SentOrderRepository(db)

        conn = await conn_repo.create(
            Connection(
                name="TestConn",
                odoo_url="https://odoo.example.com",
                odoo_db="testdb",
                odoo_username="admin",
                odoo_api_key="key",
                webhook_url="https://webhook.example.com/hook",
                webhook_secret="secret",
                last_sync_at="2024-01-01 00:00:00",
                external_id="ext-123",
            )
        )

        # Mock Odoo client
        odoo_client = MagicMock(spec=OdooClient)
        odoo_client.uid = 1  # already authenticated

        # Mock HTTP for webhook sender
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
async def test_worker_skip_unchanged_order(worker_setup):
    """Order with same write_date as stored -> skip (no webhook sent)."""
    worker, odoo_client, sender, sent_repo, conn, _ = worker_setup

    # Pre-store the order as already sent with write_date
    await sent_repo.mark_sent(
        SentOrder(
            connection_id=conn.id,
            odoo_order_id=100,
            odoo_order_name="SO100",
            odoo_write_date="2024-01-02 10:00:00",
            last_state="sale",
            odoo_create_date="2024-01-01 08:00:00",
        )
    )

    # Odoo returns the same order, unchanged write_date
    odoo_client.search_read = AsyncMock(
        return_value=[
            {
                "id": 100,
                "name": "SO100",
                "state": "sale",
                "write_date": "2024-01-02 10:00:00",  # same as stored
                "date_order": "2024-01-01 10:00:00",
                "create_date": "2024-01-01 08:00:00",
                "origin": False,
                "partner_id": False,
                "partner_shipping_id": False,
                "amount_untaxed": 0,
                "amount_tax": 0,
                "amount_total": 0,
                "currency_id": False,
                "note": "",
            }
        ]
    )

    # Mock fetch_batch_data
    empty_batch = BatchOdooData(partners={}, products={}, variants={})
    empty_batch._lines_by_order = {}

    send_calls = []

    async def mock_send(url, payload, webhook_secret="", connection_external_id=""):
        send_calls.append(payload)

    with patch("src.poller.worker.fetch_batch_data", AsyncMock(return_value=empty_batch)):
        sender.send = AsyncMock(side_effect=mock_send)
        await worker.execute()

    assert len(send_calls) == 0, "No webhook should be sent for unchanged order"


@pytest.mark.asyncio
async def test_worker_cancel_emits_cancelled_event(worker_setup):
    """Order transitioning to cancel -> emits webhook with event_type=cancelled."""
    worker, odoo_client, sender, sent_repo, conn, _ = worker_setup

    # Pre-store order as 'sale'
    await sent_repo.mark_sent(
        SentOrder(
            connection_id=conn.id,
            odoo_order_id=200,
            odoo_order_name="SO200",
            odoo_write_date="2024-01-02 10:00:00",
            last_state="sale",
            odoo_create_date="2024-01-01 08:00:00",
        )
    )

    # Odoo returns the order now as cancelled with new write_date
    odoo_client.search_read = AsyncMock(
        return_value=[
            {
                "id": 200,
                "name": "SO200",
                "state": "cancel",
                "write_date": "2024-01-03 12:00:00",  # newer
                "date_order": "2024-01-01 10:00:00",
                "create_date": "2024-01-01 08:00:00",
                "origin": False,
                "partner_id": False,
                "partner_shipping_id": False,
                "amount_untaxed": 0,
                "amount_tax": 0,
                "amount_total": 0,
                "currency_id": False,
                "note": "",
            }
        ]
    )

    empty_batch = BatchOdooData(partners={}, products={}, variants={})
    empty_batch._lines_by_order = {}

    sent_payloads = []

    async def mock_send(url, payload, webhook_secret="", connection_external_id=""):
        sent_payloads.append(payload)

    with patch("src.poller.worker.fetch_batch_data", AsyncMock(return_value=empty_batch)):
        sender.send = AsyncMock(side_effect=mock_send)
        await worker.execute()

    assert len(sent_payloads) == 1, "One webhook should be sent for the cancelled order"
    assert sent_payloads[0]["event_type"] == "cancelled"
