from __future__ import annotations

import json
import logging
from datetime import datetime, timezone, timedelta

from src.db.models import (
    CircuitState,
    Connection,
    RetryItem,
    RetryStatus,
    SentOrder,
    SyncLog,
)
from src.db.repositories import (
    ConnectionRepository,
    RetryQueueRepository,
    SentOrderRepository,
    SyncLogRepository,
)
from src.odoo.client import OdooClient, OdooRateLimitError
from src.odoo.mapper import fetch_batch_data, map_order_to_webhook_payload
from src.odoo.payload_hash import compute_relevant_hash
from src.poller.circuit_breaker import CircuitBreaker
from src.poller.classifier import classify_event
from src.poller.sender import WebhookSender, WebhookSendError

logger = logging.getLogger(__name__)

MAX_SENT_ORDERS = 30
MAX_SYNC_LOGS = 100

ORDER_FIELDS = [
    "name",
    "state",
    "date_order",
    "write_date",
    "create_date",
    "origin",
    "partner_id",
    "partner_shipping_id",
    "amount_untaxed",
    "amount_tax",
    "amount_total",
    "currency_id",
    "note",
]


def _now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class PollWorker:
    def __init__(
        self,
        connection: Connection,
        odoo_client: OdooClient,
        sender: WebhookSender,
        circuit_breaker: CircuitBreaker,
        conn_repo: ConnectionRepository,
        sync_log_repo: SyncLogRepository,
        retry_repo: RetryQueueRepository,
        sent_repo: SentOrderRepository,
    ) -> None:
        self._conn = connection
        self._odoo = odoo_client
        self._sender = sender
        self._cb = circuit_breaker
        self._conn_repo = conn_repo
        self._sync_repo = sync_log_repo
        self._retry_repo = retry_repo
        self._sent_repo = sent_repo

    async def execute(self) -> SyncLog | None:
        started_at = _now_str()
        orders_found = 0
        orders_sent = 0
        orders_failed = 0
        orders_skipped = 0
        error_message: str | None = None
        rate_limited = False

        try:
            if not self._cb.check_allowed():
                logger.info(
                    "Circuit breaker OPEN para '%s', saltando ciclo",
                    self._conn.name,
                )
                return None

            # Autenticar (reusa sesión si ya está autenticado)
            if self._odoo.uid is None:
                await self._odoo.authenticate()

            # ── Seed: primera sincronización ──
            if not self._conn.last_sync_at:
                return await self._execute_seed(started_at)

            # Buscar órdenes confirmadas y canceladas
            domain: list = [
                ["state", "in", ["sale", "done", "cancel"]],
                ["write_date", ">", self._conn.last_sync_at],
            ]

            orders = await self._odoo.search_read(
                "sale.order", domain, ORDER_FIELDS, order="write_date asc"
            )
            orders_found = len(orders)

            if not orders:
                self._cb.record_success()
                await self._conn_repo.update_circuit_state(
                    self._conn.id, self._cb.state, self._cb.failure_count
                )
                await self._sync_repo.trim_to_limit(self._conn.id, MAX_SYNC_LOGS)
                await self._retry_repo.cleanup_finished(self._conn.id)
                sync_log = await self._log(
                    started_at, orders_found, orders_sent, orders_failed, orders_skipped, None
                )
                return sync_log

            # Clasificar órdenes: skip duplicados, emitir eventos correctos
            actionable_orders = []
            event_types = {}
            order_hashes: dict[int, str] = {}
            for order in orders:
                stored = await self._sent_repo.get_latest(self._conn.id, order["id"])
                # Pre-compute hash only for potential 'updated' events (stored exists, write_date changed)
                # The full payload isn't available yet, but we compute a preliminary hash from the order
                # fields. The real hash (from the mapped payload) is computed after batch fetch.
                # For classification, we pass new_hash="" here and re-classify per-order after batch.
                event_type = classify_event(order, stored)
                if event_type == "skip":
                    orders_skipped += 1
                    continue
                actionable_orders.append(order)
                event_types[order["id"]] = event_type
            orders_found_non_skip = len(actionable_orders)

            if actionable_orders:
                batch = await fetch_batch_data(self._odoo, actionable_orders)

                last_write_date = self._conn.last_sync_at
                for order in actionable_orders:
                    event_type = event_types[order["id"]]

                    # Resolve original_order_id for refunded events before mapping
                    original_order_id: int | None = None
                    if event_type == "refunded":
                        original_order_id = await self._resolve_original_order_id(order)

                    payload = map_order_to_webhook_payload(
                        order, batch, self._conn.odoo_db, self._conn.external_id,
                        event_type=event_type,
                        original_order_id=original_order_id,
                    )
                    new_hash = compute_relevant_hash(payload)

                    # Re-apply noise filter for 'updated' events now that we have the full payload
                    # REF-* orders (refunded) bypass this path — they have no hash noise filter
                    if event_type == "updated":
                        stored = await self._sent_repo.get_latest(self._conn.id, order["id"])
                        event_type = classify_event(order, stored, new_hash=new_hash)
                        if event_type == "skip":
                            orders_skipped += 1
                            continue
                        # Rebuild payload with correct (possibly unchanged) event_type
                        payload = map_order_to_webhook_payload(
                            order, batch, self._conn.odoo_db, self._conn.external_id,
                            event_type=event_type,
                        )

                    try:
                        await self._sender.send(
                            self._conn.webhook_url,
                            payload,
                            self._conn.webhook_secret,
                            self._conn.external_id,
                        )
                        await self._sent_repo.mark_sent(
                            SentOrder(
                                connection_id=self._conn.id,
                                odoo_order_id=order["id"],
                                odoo_order_name=order.get("name", ""),
                                odoo_write_date=order.get("write_date", ""),
                                last_state=order.get("state", "sale"),
                                odoo_create_date=order.get("create_date") or "",
                                hash_payload=new_hash,
                                sent_at=_now_str(),
                            )
                        )
                        orders_sent += 1
                    except WebhookSendError as e:
                        orders_failed += 1
                        logger.warning(
                            "Webhook falló para orden %s: %s",
                            order.get("name"),
                            e,
                        )
                        next_retry_seconds = WebhookSender.calculate_next_retry(0)
                        next_retry = (
                            datetime.now(timezone.utc) + timedelta(seconds=next_retry_seconds)
                        ).strftime("%Y-%m-%d %H:%M:%S")
                        await self._retry_repo.enqueue(
                            RetryItem(
                                connection_id=self._conn.id,
                                odoo_order_id=order["id"],
                                odoo_order_name=order.get("name", ""),
                                payload=json.dumps(payload),
                                next_retry_at=next_retry,
                            )
                        )

                    wd = order.get("write_date", "")
                    if wd and (not last_write_date or wd > last_write_date):
                        last_write_date = wd

                if last_write_date:
                    await self._conn_repo.update_last_sync(self._conn.id, last_write_date)
                    self._conn.last_sync_at = last_write_date

            # Procesar retry queue
            await self._process_retries()

            self._cb.record_success()

            await self._sync_repo.trim_to_limit(self._conn.id, MAX_SYNC_LOGS)
            await self._retry_repo.cleanup_finished(self._conn.id)

        except OdooRateLimitError as e:
            logger.warning("Rate limit en '%s': %s", self._conn.name, e)
            error_message = str(e)
            rate_limited = True
        except Exception as e:
            logger.error("Error en polling '%s': %s", self._conn.name, e, exc_info=True)
            error_message = str(e)
            self._cb.record_failure()

        await self._conn_repo.update_circuit_state(
            self._conn.id, self._cb.state, self._cb.failure_count
        )

        return await self._log(
            started_at, orders_found, orders_sent, orders_failed, orders_skipped, error_message
        )

    async def _execute_seed(self, started_at: str) -> SyncLog:
        """Primera sincronización: registra las últimas N órdenes sin enviar webhooks."""
        logger.info("Seed inicial para '%s': registrando últimas %d órdenes", self._conn.name, MAX_SENT_ORDERS)

        domain: list = [["state", "in", ["sale", "done", "cancel"]]]
        orders = await self._odoo.search_read(
            "sale.order", domain, ORDER_FIELDS,
            limit=MAX_SENT_ORDERS, order="write_date desc",
        )
        orders_found = len(orders)

        last_write_date: str | None = None
        for order in orders:
            await self._sent_repo.mark_sent(
                SentOrder(
                    connection_id=self._conn.id,
                    odoo_order_id=order["id"],
                    odoo_order_name=order.get("name", ""),
                    odoo_write_date=order.get("write_date", ""),
                    last_state=order.get("state", "sale"),
                    odoo_create_date=order.get("create_date") or "",
                    sent_at=_now_str(),
                )
            )
            wd = order.get("write_date", "")
            if wd and (not last_write_date or wd > last_write_date):
                last_write_date = wd

        if last_write_date:
            await self._conn_repo.update_last_sync(self._conn.id, last_write_date)
            self._conn.last_sync_at = last_write_date

        self._cb.record_success()
        await self._conn_repo.update_circuit_state(
            self._conn.id, self._cb.state, self._cb.failure_count
        )

        logger.info(
            "Seed completado para '%s': %d órdenes registradas (0 webhooks enviados)",
            self._conn.name, orders_found,
        )
        return await self._log(started_at, orders_found, 0, 0, orders_found, None)

    async def _resolve_original_order_id(self, ref_order: dict) -> int | None:
        """Resolve the numeric Odoo id of the original order for a REF-* counter-order.

        Uses the ``origin`` field of the counter-order (which stores the ``name`` of
        the original order, e.g. "SO100") to look up the original order in Odoo.

        Returns the integer id if found, or ``None`` with a warning when the original
        order no longer exists in Odoo (e.g. was deleted or renamed after refund).
        """
        origin_name: str = ref_order.get("origin") or ""
        if not origin_name:
            logger.warning(
                "Orden contraria '%s' (id=%s) no tiene campo 'origin'. "
                "original_order_id será None.",
                ref_order.get("name"),
                ref_order.get("id"),
            )
            return None

        try:
            results = await self._odoo.search_read(
                "sale.order",
                [["name", "=", origin_name]],
                ["id", "name"],
                limit=1,
            )
        except Exception as exc:
            logger.warning(
                "Error al resolver original_order_id para '%s' (origin='%s'): %s",
                ref_order.get("name"),
                origin_name,
                exc,
            )
            return None

        if not results:
            logger.warning(
                "Orden original '%s' no encontrada en Odoo para la orden contraria '%s'. "
                "original_order_id será None.",
                origin_name,
                ref_order.get("name"),
            )
            return None

        return results[0]["id"]

    async def _process_retries(self) -> None:
        pending = await self._retry_repo.get_pending(self._conn.id)
        for item in pending:
            if item.attempts >= item.max_attempts:
                await self._retry_repo.update_status(
                    item.id, RetryStatus.DISCARDED, last_error="Max attempts reached"
                )
                continue

            try:
                payload = json.loads(item.payload)
                await self._sender.send(
                    self._conn.webhook_url,
                    payload,
                    self._conn.webhook_secret,
                    self._conn.external_id,
                )
                await self._retry_repo.update_status(item.id, RetryStatus.SENT)
                await self._sent_repo.mark_sent(
                    SentOrder(
                        connection_id=self._conn.id,
                        odoo_order_id=item.odoo_order_id,
                        odoo_order_name=item.odoo_order_name,
                        odoo_write_date=payload.get("order", {}).get("write_date", ""),
                        last_state=payload.get("order", {}).get("state", "sale"),
                        odoo_create_date=payload.get("order", {}).get("create_date", ""),
                        sent_at=_now_str(),
                    )
                )
            except WebhookSendError as e:
                new_attempt = item.attempts + 1
                next_seconds = WebhookSender.calculate_next_retry(new_attempt)
                next_at = (
                    datetime.now(timezone.utc) + timedelta(seconds=next_seconds)
                ).strftime("%Y-%m-%d %H:%M:%S")
                await self._retry_repo.update_status(
                    item.id,
                    RetryStatus.PENDING,
                    attempts=new_attempt,
                    next_retry_at=next_at,
                    last_error=str(e),
                )

    async def _log(
        self,
        started_at: str,
        found: int,
        sent: int,
        failed: int,
        skipped: int,
        error: str | None,
    ) -> SyncLog:
        return await self._sync_repo.create(
            SyncLog(
                connection_id=self._conn.id,
                started_at=started_at,
                finished_at=_now_str(),
                orders_found=found,
                orders_sent=sent,
                orders_failed=failed,
                orders_skipped=skipped,
                error_message=error,
            )
        )
