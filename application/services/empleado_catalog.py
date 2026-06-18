# application/services/empleado_catalog.py
"""Cachea el maestro de empleados de Sigrid (emp) con TTL.

Lo usa la CONCILIACION de trabajadores sin casar: genera candidatos por
similitud de nombre y resuelve el empleado elegido (por ide) al confirmar.
"""
from __future__ import annotations

import logging
import threading
import time

from infrastructure.sigrid.sigrid_lookup_client import (
    EmpleadoOption,
    SigridLookupClient,
)

logger = logging.getLogger(__name__)


class EmpleadoCatalog:
    def __init__(
        self,
        *,
        client: SigridLookupClient | None,
        ttl_seconds: int = 600,
    ) -> None:
        self._client = client
        self._ttl = int(ttl_seconds)
        self._lock = threading.RLock()
        self._items: list[EmpleadoOption] = []
        self._by_ide: dict[int, EmpleadoOption] = {}
        self._loaded_at: float = 0.0
        self._ever_loaded = False

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def list(self) -> list[EmpleadoOption]:
        self._ensure_fresh()
        return list(self._items)

    def get_by_ide(self, ide: int | None) -> EmpleadoOption | None:
        if ide is None:
            return None
        self._ensure_fresh()
        return self._by_ide.get(int(ide))

    def _ensure_fresh(self) -> None:
        if self._client is None:
            return
        with self._lock:
            now = time.time()
            if self._ever_loaded and (now - self._loaded_at) < self._ttl:
                return
            try:
                items = self._client.fetch_empleados()
                self._items = items
                self._by_ide = {e.ide: e for e in items}
                self._loaded_at = now
                self._ever_loaded = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("[empleado-catalog] refresco fallo: %r", exc)
