import os
import tempfile

import pytest
from cryptography.fernet import Fernet

from src.db.database import init_db
from src.db.models import Connection, CircuitState
from src.db.repositories import ConnectionRepository, SyncLogRepository, SentOrderRepository
from src.encryption import FieldEncryptor


@pytest.fixture
async def db_and_repos():
    key = Fernet.generate_key().decode()
    enc = FieldEncryptor(key)
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        db = await init_db(db_path)
        conn_repo = ConnectionRepository(db, enc)
        sync_repo = SyncLogRepository(db)
        sent_repo = SentOrderRepository(db)
        yield db, conn_repo, sync_repo, sent_repo, enc
        await db.close()


@pytest.mark.asyncio
async def test_create_and_read_connection(db_and_repos):
    db, conn_repo, _, _, enc = db_and_repos

    conn = Connection(
        name="Test",
        odoo_url="https://test.odoo.com",
        odoo_db="testdb",
        odoo_username="admin",
        odoo_api_key="secret-key-123",
        webhook_url="https://webhook.example.com",
        webhook_secret="webhook-secret",
        poll_interval_seconds=30,
    )
    created = await conn_repo.create(conn)
    assert created.id is not None

    fetched = await conn_repo.get(created.id)
    assert fetched is not None
    assert fetched.name == "Test"
    assert fetched.odoo_api_key == "secret-key-123"
    assert fetched.webhook_secret == "webhook-secret"
    assert fetched.poll_interval_seconds == 30

    # Verificar que en la DB los campos están encriptados
    cursor = await db.execute("SELECT odoo_api_key, webhook_secret FROM connections WHERE id = ?", (created.id,))
    row = await cursor.fetchone()
    assert row["odoo_api_key"] != "secret-key-123"
    assert row["webhook_secret"] != "webhook-secret"
    assert enc.decrypt(row["odoo_api_key"]) == "secret-key-123"


@pytest.mark.asyncio
async def test_list_enabled_connections(db_and_repos):
    _, conn_repo, _, _, _ = db_and_repos

    await conn_repo.create(Connection(name="A", odoo_url="u", odoo_db="d", odoo_username="u", odoo_api_key="k", webhook_url="w", enabled=True))
    await conn_repo.create(Connection(name="B", odoo_url="u", odoo_db="d", odoo_username="u", odoo_api_key="k", webhook_url="w", enabled=False))

    enabled = await conn_repo.list_enabled()
    assert len(enabled) == 1
    assert enabled[0].name == "A"


@pytest.mark.asyncio
async def test_update_circuit_state(db_and_repos):
    _, conn_repo, _, _, _ = db_and_repos

    conn = await conn_repo.create(Connection(name="X", odoo_url="u", odoo_db="d", odoo_username="u", odoo_api_key="k", webhook_url="w"))
    await conn_repo.update_circuit_state(conn.id, CircuitState.OPEN, 5)

    fetched = await conn_repo.get(conn.id)
    assert fetched.circuit_state == CircuitState.OPEN
    assert fetched.circuit_failure_count == 5


@pytest.mark.asyncio
async def test_sent_order_idempotency(db_and_repos):
    _, conn_repo, _, sent_repo, _ = db_and_repos

    conn = await conn_repo.create(Connection(name="Y", odoo_url="u", odoo_db="d", odoo_username="u", odoo_api_key="k", webhook_url="w"))

    from src.db.models import SentOrder
    order = SentOrder(connection_id=conn.id, odoo_order_id=42, odoo_order_name="SO042", odoo_write_date="2024-01-01 00:00:00")
    await sent_repo.mark_sent(order)

    assert await sent_repo.is_sent(conn.id, 42, "2024-01-01 00:00:00")
    assert not await sent_repo.is_sent(conn.id, 42, "2024-01-02 00:00:00")
    assert not await sent_repo.is_sent(conn.id, 99, "2024-01-01 00:00:00")
