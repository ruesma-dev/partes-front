# infrastructure/transfer/transfer_client.py
"""Cliente del microservicio partes-transfer (sv5).

sv4 NUNCA escribe en Sigrid: construye las lineas a registrar y delega en
este servicio, que aplica las reglas y escribe (o avisa de conflictos).
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class TransferClient:
    def __init__(self, *, base_url: str, timeout_s: float = 120.0) -> None:
        if not base_url:
            raise ValueError("TransferClient requiere base_url")
        self._base = base_url.rstrip("/")
        self._timeout = float(timeout_s)

    def _post(self, ruta: str, payload: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base}{ruta}"
        try:
            r = httpx.post(url, json=payload, timeout=self._timeout)
        except httpx.ConnectError:
            # El servicio no esta arrancado / puerto cerrado: mensaje util
            # en la pantalla en vez de un 500 con traceback.
            logger.warning("[transfer] sin conexion con %s", url)
            return {"ok": False, "error": (
                f"no se puede conectar con el servicio de registro "
                f"(partes-transfer) en {self._base}. Arrancalo con "
                f"'python main.py' en el proyecto partes-transfer.")}
        except httpx.TimeoutException:
            logger.warning("[transfer] timeout en %s", url)
            return {"ok": False, "error": (
                f"el servicio de registro no respondio en "
                f"{self._timeout:.0f}s ({self._base}).")}
        except httpx.HTTPError as exc:
            logger.warning("[transfer] error de red en %s: %s", url, exc)
            return {"ok": False, "error": f"error de red hacia {self._base}: {exc}"}
        if r.status_code >= 400:
            logger.warning("[transfer] %s HTTP %s: %s", ruta, r.status_code,
                           r.text[:300])
            try:
                body = r.json()
            except Exception:  # noqa: BLE001
                body = {}
            return {"ok": False,
                    "error": body.get("error") or f"HTTP {r.status_code}"}
        return r.json()

    def preflight(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/api/registro/preflight", payload)

    def ejecutar(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._post("/api/registro/ejecutar", payload)
