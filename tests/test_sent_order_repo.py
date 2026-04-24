from __future__ import annotations

import os
import tempfile

import pytest
from cryptography.fernet import Fernet

from src.db.database import init_db
from src.db.models import Connection, SentOrder
from src.db.repositories import ConnectionRepository, SentOrderRepository
from src.encryption import FieldEncryptor


@pytest.fixture
async def db_and_repo():
    key = Fernet.generate_key().decode()
    enc = FieldEncryptor(key)
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_repo.db")
        db = await init_db(db_path)
        conn_repo = ConnectionRepository(db, enc)
        sent_repo = SentOrderRepository(db)
        conn = await conn_repo.create(
            Connection(
                name="Test",
                odoo_url="https://test.odoo.com",
                odoo_db="testdb",
                odoo_username="admin",
                odoo_api_key="key",
                webhook_url="https://webhook.example.com",
            )
        )
        yield db, sent_repo, conn
        await db.close()


@pytest.mark.asyncio
async def test_get_latest_returns_none_for_unknown(db_and_repo):
    """get_latest returns None when order has never been sent."""
    _, sent_repo, conn = db_and_repo
    result = await sent_repo.get_latest(conn.id, 99999)
    assert result is None


@pytest.mark.asyncio
async def test_get_latest_returns_stored_row(db_and_repo):
    """get_latest returns the stored SentOrder with all fields including last_state."""
    _, sent_repo, conn = db_and_repo

    order = SentOrder(
        connection_id=conn.id,
        odoo_order_id=42,
        odoo_order_name="SO042",
        odoo_write_date="2024-01-01 10:00:00",
        last_state="sale",
        odoo_create_date="2024-01-01 08:00:00",
        sent_at="2024-01-01 10:05:00",
    )
    await sent_repo.mark_sent(order)

    result = await sent_repo.get_latest(conn.id, 42)
    assert result is not None
    assert result.odoo_order_id == 42
    assert result.odoo_order_name == "SO042"
    assert result.last_state == "sale"
    assert result.odoo_create_date == "2024-01-01 08:00:00"
    assert result.odoo_write_date == "2024-01-01 10:00:00"


@pytest.mark.asyncio
async def test_mark_sent_upsert_updates_existing(db_and_repo):
    """Multiple mark_sent calls for same order result in 1 row with latest values."""
    _, sent_repo, conn = db_and_repo

    order_v1 = SentOrder(
        connection_id=conn.id,
        odoo_order_id=10,
        odoo_order_name="SO010",
        odoo_write_date="2024-01-01 10:00:00",
        last_state="sale",
        odoo_create_date="2024-01-01 08:00:00",
    )
    await sent_repo.mark_sent(order_v1)

    order_v2 = SentOrder(
        connection_id=conn.id,
        odoo_order_id=10,
        odoo_order_name="SO010",
        odoo_write_date="2024-01-02 12:00:00",
        last_state="cancel",
        odoo_create_date="2024-01-01 08:00:00",
    )
    await sent_repo.mark_sent(order_v2)

    # Only 1 row should exist
    rows = await sent_repo.list_by_connection(conn.id)
    assert len(rows) == 1

    result = await sent_repo.get_latest(conn.id, 10)
    assert result is not None
    assert result.last_state == "cancel"
    assert result.odoo_write_date == "2024-01-02 12:00:00"


@pytest.mark.asyncio
async def test_mark_sent_upsert_multiple_orders_independent(db_and_repo):
    """Different order IDs each get their own row — they don't conflict."""
    _, sent_repo, conn = db_and_repo

    for oid in [1, 2, 3]:
        await sent_repo.mark_sent(
            SentOrder(
                connection_id=conn.id,
                odoo_order_id=oid,
                odoo_order_name=f"SO00{oid}",
                odoo_write_date="2024-01-01 10:00:00",
                last_state="sale",
                odoo_create_date="",
            )
        )

    rows = await sent_repo.list_by_connection(conn.id)
    assert len(rows) == 3
