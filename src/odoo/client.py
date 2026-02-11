from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

TIMEOUT = 30.0


class OdooAuthError(Exception):
    pass


class OdooRPCError(Exception):
    pass


class OdooRateLimitError(Exception):
    pass


class OdooClient:
    def __init__(
        self,
        url: str,
        db: str,
        username: str,
        api_key: str,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._db = db
        self._username = username
        self._api_key = api_key
        self._uid: int | None = None
        self._http = http_client or httpx.AsyncClient(timeout=TIMEOUT)
        self._owns_http = http_client is None

    @property
    def uid(self) -> int | None:
        return self._uid

    async def authenticate(self) -> int:
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "service": "common",
                "method": "authenticate",
                "args": [self._db, self._username, self._api_key, {}],
            },
        }
        result = await self._rpc_call(payload)
        if not result or not isinstance(result, int):
            raise OdooAuthError(f"Autenticación fallida para {self._username}@{self._db}")
        self._uid = result
        logger.info("Autenticado en Odoo: uid=%d, db=%s", self._uid, self._db)
        return self._uid

    async def search_read(
        self,
        model: str,
        domain: list,
        fields: list[str],
        limit: int = 0,
        order: str = "",
    ) -> list[dict[str, Any]]:
        kwargs: dict[str, Any] = {"fields": fields}
        if limit:
            kwargs["limit"] = limit
        if order:
            kwargs["order"] = order
        return await self._object_call(
            model,
            "search_read",
            [domain],
            kwargs,
        )

    async def read(
        self, model: str, ids: list[int], fields: list[str]
    ) -> list[dict[str, Any]]:
        if not ids:
            return []
        return await self._object_call(model, "read", [ids], {"fields": fields})

    async def _object_call(
        self, model: str, method: str, args: list, kwargs: dict
    ) -> Any:
        if self._uid is None:
            await self.authenticate()

        try:
            return await self._execute(model, method, args, kwargs)
        except OdooAuthError:
            logger.warning("Sesión expirada, re-autenticando...")
            await self.authenticate()
            return await self._execute(model, method, args, kwargs)

    async def _execute(
        self, model: str, method: str, args: list, kwargs: dict
    ) -> Any:
        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "service": "object",
                "method": "execute_kw",
                "args": [self._db, self._uid, self._api_key, model, method, args, kwargs],
            },
        }
        return await self._rpc_call(payload)

    async def _rpc_call(self, payload: dict) -> Any:
        url = f"{self._url}/jsonrpc"
        response = await self._http.post(url, json=payload)

        if response.status_code == 429:
            raise OdooRateLimitError("HTTP 429: Rate limit alcanzado")
        response.raise_for_status()

        data = response.json()
        if "error" in data:
            error = data["error"]
            msg = error.get("data", {}).get("message", error.get("message", str(error)))
            if "Session" in msg or "Access Denied" in msg or "authenticate" in msg.lower():
                raise OdooAuthError(msg)
            raise OdooRPCError(msg)

        return data.get("result")

    async def close(self) -> None:
        if self._owns_http:
            await self._http.aclose()
