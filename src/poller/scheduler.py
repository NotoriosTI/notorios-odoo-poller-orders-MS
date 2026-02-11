from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Awaitable

import httpx

from src.db.models import CircuitState, Connection, SyncLog
from src.db.repositories import (
    ConnectionRepository,
    RetryQueueRepository,
    SentOrderRepository,
    SyncLogRepository,
)
from src.encryption import FieldEncryptor
from src.odoo.client import OdooClient
from src.poller.circuit_breaker import CircuitBreaker
from src.poller.sender import WebhookSender
from src.poller.worker import PollWorker

logger = logging.getLogger(__name__)

OnSyncComplete = Callable[[int, SyncLog | None], Awaitable[None]]
OnCircuitStateChange = Callable[[int, CircuitState], Awaitable[None]]


class _ConnectionTask:
    def __init__(self) -> None:
        self.task: asyncio.Task | None = None
        self.http_client: httpx.AsyncClient | None = None
        self.circuit_breaker = CircuitBreaker()


class Scheduler:
    def __init__(
        self,
        conn_repo: ConnectionRepository,
        sync_log_repo: SyncLogRepository,
        retry_repo: RetryQueueRepository,
        sent_repo: SentOrderRepository,
        on_sync_complete: OnSyncComplete | None = None,
        on_circuit_state_change: OnCircuitStateChange | None = None,
    ) -> None:
        self._conn_repo = conn_repo
        self._sync_log_repo = sync_log_repo
        self._retry_repo = retry_repo
        self._sent_repo = sent_repo
        self._on_sync_complete = on_sync_complete
        self._on_circuit_state_change = on_circuit_state_change
        self._tasks: dict[int, _ConnectionTask] = {}
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def get_circuit_state(self, conn_id: int) -> CircuitState | None:
        ct = self._tasks.get(conn_id)
        return ct.circuit_breaker.state if ct else None

    async def start(self) -> None:
        self._running = True
        connections = await self._conn_repo.list_enabled()
        for conn in connections:
            await self.add_connection(conn)
        logger.info("Scheduler iniciado con %d conexiones", len(connections))

    async def stop(self) -> None:
        self._running = False
        conn_ids = list(self._tasks.keys())
        for conn_id in conn_ids:
            await self.remove_connection(conn_id)
        logger.info("Scheduler detenido")

    async def add_connection(self, conn: Connection) -> None:
        if conn.id in self._tasks:
            return

        ct = _ConnectionTask()
        ct.http_client = httpx.AsyncClient(timeout=30.0)
        ct.circuit_breaker.load_state(conn.circuit_state, conn.circuit_failure_count)
        ct.task = asyncio.create_task(
            self._poll_loop(conn, ct), name=f"poll-{conn.id}-{conn.name}"
        )
        self._tasks[conn.id] = ct

    async def remove_connection(self, conn_id: int) -> None:
        ct = self._tasks.pop(conn_id, None)
        if not ct:
            return
        if ct.task and not ct.task.done():
            ct.task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(ct.task), timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
        if ct.http_client:
            await ct.http_client.aclose()

    async def restart_connection(self, conn: Connection) -> None:
        await self.remove_connection(conn.id)
        if conn.enabled:
            await self.add_connection(conn)

    async def reset_circuit_breaker(self, conn_id: int) -> None:
        ct = self._tasks.get(conn_id)
        if ct:
            ct.circuit_breaker.reset()
            await self._conn_repo.update_circuit_state(
                conn_id, CircuitState.CLOSED, 0
            )
            if self._on_circuit_state_change:
                await self._on_circuit_state_change(conn_id, CircuitState.CLOSED)

    async def _poll_loop(self, conn: Connection, ct: _ConnectionTask) -> None:
        odoo_client: OdooClient | None = None
        try:
            while self._running:
                try:
                    # Refresh connection data
                    fresh = await self._conn_repo.get(conn.id)
                    if not fresh or not fresh.enabled:
                        logger.info("Conexión '%s' deshabilitada, deteniendo", conn.name)
                        break
                    conn = fresh

                    if odoo_client is None:
                        odoo_client = OdooClient(
                            conn.odoo_url,
                            conn.odoo_db,
                            conn.odoo_username,
                            conn.odoo_api_key,
                            http_client=ct.http_client,
                        )

                    prev_state = ct.circuit_breaker.state
                    sender = WebhookSender(ct.http_client)
                    worker = PollWorker(
                        connection=conn,
                        odoo_client=odoo_client,
                        sender=sender,
                        circuit_breaker=ct.circuit_breaker,
                        conn_repo=self._conn_repo,
                        sync_log_repo=self._sync_log_repo,
                        retry_repo=self._retry_repo,
                        sent_repo=self._sent_repo,
                    )
                    sync_log = await worker.execute()

                    if self._on_sync_complete:
                        await self._on_sync_complete(conn.id, sync_log)

                    new_state = ct.circuit_breaker.state
                    if new_state != prev_state and self._on_circuit_state_change:
                        await self._on_circuit_state_change(conn.id, new_state)

                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(
                        "Error inesperado en poll loop '%s': %s",
                        conn.name,
                        e,
                        exc_info=True,
                    )

                await asyncio.sleep(conn.poll_interval_seconds)

        except asyncio.CancelledError:
            logger.info("Poll loop cancelado para '%s'", conn.name)
        finally:
            if odoo_client and odoo_client._owns_http is False:
                pass  # No cerrar, el scheduler cierra el http_client
