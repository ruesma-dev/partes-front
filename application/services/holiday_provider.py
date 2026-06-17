# application/services/holiday_provider.py
"""Marca festivos para el calendario del trabajador.

Usa la libreria ``holidays`` (Espana + subdivision, p.ej. Madrid 'MD') si
esta instalada; incluye fiestas moviles (Semana Santa). Permite anadir
festivos LOCALES extra por configuracion (CSV de 'YYYY-MM-DD'). Si la
libreria no esta disponible, solo cuentan los festivos extra y los fines
de semana (estos los marca el calendario, no este proveedor).
"""
from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger(__name__)


class HolidayProvider:
    def __init__(
        self,
        *,
        enabled: bool = True,
        subdiv: str | None = "MD",
        extra_iso: tuple[str, ...] = (),
    ) -> None:
        self._enabled = enabled
        self._subdiv = (subdiv or "").strip() or None
        self._extra = set(extra_iso or ())
        self._mod = None
        self._cache: dict[int, object] = {}
        if enabled:
            try:
                import holidays  # type: ignore
                self._mod = holidays
            except Exception:  # noqa: BLE001
                logger.info(
                    "[holidays] libreria 'holidays' no instalada; solo "
                    "festivos extra + fines de semana."
                )

    def name(self, d: date) -> str | None:
        iso = d.isoformat()
        if iso in self._extra:
            return "Festivo local"
        if self._mod is None:
            return None
        obj = self._cache.get(d.year)
        if obj is None:
            try:
                obj = self._mod.Spain(years=d.year, subdiv=self._subdiv)
            except Exception:  # noqa: BLE001
                obj = {}
            self._cache[d.year] = obj
        try:
            return obj.get(d)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            return None
