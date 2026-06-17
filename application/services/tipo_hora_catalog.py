# application/services/tipo_hora_catalog.py
"""Cachea el catalogo de tipos de hora (auxhor) de Sigrid con TTL.

Lo usan: (a) el endpoint que puebla el desplegable y (b) el guardado del
codigo de hora, que resuelve el ``auxhor`` elegido por ``ide`` para
persistir descripcion/ext/precios desde el maestro (no desde el cliente).
"""
from __future__ import annotations

import logging
import threading
import time

from infrastructure.sigrid.sigrid_lookup_client import (
    SigridLookupClient,
    TipoHoraOption,
)

logger = logging.getLogger(__name__)


class TipoHoraCatalog:
    def __init__(
        self,
        *,
        client: SigridLookupClient | None,
        ttl_seconds: int = 600,
    ) -> None:
        self._client = client
        self._ttl = int(ttl_seconds)
        self._lock = threading.RLock()
        self._items: list[TipoHoraOption] = []
        self._by_ide: dict[int, TipoHoraOption] = {}
        self._loaded_at: float = 0.0
        self._ever_loaded = False

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def list(self) -> list[TipoHoraOption]:
        self._ensure_fresh()
        return list(self._items)

    def get_by_ide(self, ide: int) -> TipoHoraOption | None:
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
                items = self._client.fetch_tipos_hora()
                self._items = items
                self._by_ide = {t.ide: t for t in items}
                self._loaded_at = now
                self._ever_loaded = True
            except Exception:
                logger.exception(
                    "[tipo-hora-catalog] fallo cargando auxhor de Sigrid."
                )
                # Conserva lo anterior si lo hubiera.
