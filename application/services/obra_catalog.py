# application/services/obra_catalog.py
"""Cachea la lista de obras de Sigrid (codigo + nombre + ide) con TTL.

La usa el endpoint que puebla el desplegable con autocompletar de obra del
detalle del parte, y la resolucion de la obra elegida (por codigo) al
guardar, para fijar ide/nombre desde el maestro.
"""
from __future__ import annotations

import logging
import threading
import time

from infrastructure.sigrid.sigrid_lookup_client import (
    ObraOption,
    SigridLookupClient,
)

logger = logging.getLogger(__name__)


class ObraCatalog:
    def __init__(
        self,
        *,
        client: SigridLookupClient | None,
        ttl_seconds: int = 600,
    ) -> None:
        self._client = client
        self._ttl = int(ttl_seconds)
        self._lock = threading.RLock()
        self._items: list[ObraOption] = []
        self._by_codigo: dict[str, ObraOption] = {}
        self._loaded_at: float = 0.0
        self._ever_loaded = False

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def list(self) -> list[ObraOption]:
        self._ensure_fresh()
        return list(self._items)

    def get_by_codigo(self, codigo: str | None) -> ObraOption | None:
        if not codigo:
            return None
        self._ensure_fresh()
        return self._by_codigo.get(str(codigo).strip().upper())

    def _ensure_fresh(self) -> None:
        if self._client is None:
            return
        with self._lock:
            now = time.time()
            if self._ever_loaded and (now - self._loaded_at) < self._ttl:
                return
            try:
                items = self._client.fetch_obras()
                self._items = items
                self._by_codigo = {
                    str(o.codigo).strip().upper(): o
                    for o in items
                    if o.codigo
                }
                self._loaded_at = now
                self._ever_loaded = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("[obra-catalog] refresco fallo: %r", exc)
