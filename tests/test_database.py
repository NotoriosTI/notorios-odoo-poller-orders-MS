import os
import tempfile

import pytest
from cryptography.fernet import Fernet

from src.db.database import init_db
from src.db.models import Connection, CircuitState
from src.db.repositories import ConnectionRepository, RetryQueueRepository, SyncLogRepository, SentOrderRepository
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
        retry_repo = RetryQueueRepository(db)
        yield db, conn_repo, sync_repo, sent_repo, enc, retry_repo
        await db.close()


@pytest.mark.asyncio
async def test_create_and_read_connection(db_and_repos):
    db, conn_repo, _, _, enc, _ = db_and_repos

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
    _, conn_repo, _, _, _, _ = db_and_repos

    await conn_repo.create(Connection(name="A", odoo_url="u", odoo_db="d", odoo_username="u", odoo_api_key="k", webhook_url="w", enabled=True))
    await conn_repo.create(Connection(name="B", odoo_url="u", odoo_db="d", odoo_username="u", odoo_api_key="k", webhook_url="w", enabled=False))

    enabled = await conn_repo.list_enabled()
    assert len(enabled) == 1
    assert enabled[0].name == "A"


@pytest.mark.asyncio
async def test_update_circuit_state(db_and_repos):
    _, conn_repo, _, _, _, _ = db_and_repos

    conn = await conn_repo.create(Connection(name="X", odoo_url="u", odoo_db="d", odoo_username="u", odoo_api_key="k", webhook_url="w"))
    await conn_repo.update_circuit_state(conn.id, CircuitState.OPEN, 5)

    fetched = await conn_repo.get(conn.id)
    assert fetched.circuit_state == CircuitState.OPEN
    assert fetched.circuit_failure_count == 5


@pytest.mark.asyncio
async def test_sent_order_idempotency(db_and_repos):
    _, conn_repo, _, sent_repo, _, _ = db_and_repos

    conn = await conn_repo.create(Connection(name="Y", odoo_url="u", odoo_db="d", odoo_username="u", odoo_api_key="k", webhook_url="w"))

    from src.db.models import SentOrder
    order = SentOrder(connection_id=conn.id, odoo_order_id=42, odoo_order_name="SO042", odoo_write_date="2024-01-01 00:00:00")
    await sent_repo.mark_sent(order)

    assert await sent_repo.is_sent(conn.id, 42, "2024-01-01 00:00:00")
    assert not await sent_repo.is_sent(conn.id, 42, "2024-01-02 00:00:00")
    assert not await sent_repo.is_sent(conn.id, 99, "2024-01-01 00:00:00")


@pytest.mark.asyncio
async def test_sync_log_trim_to_limit(db_and_repos):
    _, conn_repo, sync_repo, _, _, _ = db_and_repos

    from src.db.models import SyncLog

    conn = await conn_repo.create(Connection(name="T", odoo_url="u", odoo_db="d", odoo_username="u", odoo_api_key="k", webhook_url="w"))

    for i in range(5):
        await sync_repo.create(SyncLog(
            connection_id=conn.id,
            started_at=f"2024-01-01 00:0{i}:00",
            finished_at=f"2024-01-01 00:0{i}:01",
            orders_found=i,
        ))

    await sync_repo.trim_to_limit(conn.id, limit=3)

    logs = await sync_repo.list_by_connection(conn.id, limit=100)
    assert len(logs) == 3
    # Los 3 más recientes (por id DESC)
    assert [l.orders_found for l in logs] == [4, 3, 2]


@pytest.mark.asyncio
async def test_retry_queue_cleanup_finished(db_and_repos):
    _, conn_repo, _, _, _, retry_repo = db_and_repos

    from src.db.models import RetryItem, RetryStatus

    conn = await conn_repo.create(Connection(name="R", odoo_url="u", odoo_db="d", odoo_username="u", odoo_api_key="k", webhook_url="w"))

    # Crear items con distintos estados
    await retry_repo.enqueue(RetryItem(connection_id=conn.id, odoo_order_id=1, odoo_order_name="SO1", payload="{}"))
    item_sent = await retry_repo.enqueue(RetryItem(connection_id=conn.id, odoo_order_id=2, odoo_order_name="SO2", payload="{}"))
    item_disc = await retry_repo.enqueue(RetryItem(connection_id=conn.id, odoo_order_id=3, odoo_order_name="SO3", payload="{}"))

    await retry_repo.update_status(item_sent.id, RetryStatus.SENT)
    await retry_repo.update_status(item_disc.id, RetryStatus.DISCARDED)

    await retry_repo.cleanup_finished(conn.id)

    remaining = await retry_repo.list_by_connection(conn.id)
    assert len(remaining) == 1
    assert remaining[0].odoo_order_name == "SO1"
    assert remaining[0].status == RetryStatus.PENDING
