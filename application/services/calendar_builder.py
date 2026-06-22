# application/services/calendar_builder.py
"""Construye el CALENDARIO mensual del trabajador para el detalle.

Convencion de "mes" de Ruesma: el periodo va del DIA 16 del mes anterior
al DIA 15 del mes presente. El periodo se ETIQUETA por el mes del dia 15
(el de cierre). Ej.: periodo "Abril 2026" = 16/03/2026 .. 15/04/2026.

Funciones puras (sin IO) para poder testearlas:
  - period_of(date)            -> (year, month) del periodo al que pertenece
  - period_bounds(year,month)  -> (inicio, fin) del periodo
  - build_period_options(isos) -> opciones de periodo (desc) de unas fechas
  - build_calendar(...)        -> rejilla de semanas con horas por dia
"""
from __future__ import annotations

import calendar as _calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Iterable

_MESES = [
    "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre",
]

# Modos de periodo soportados.
MODE_NOMINA = "nomina"     # del 16 del mes anterior al 15 del mes presente
MODE_NATURAL = "natural"   # mes natural (1 al ultimo dia)


def normalize_mode(mode: str | None) -> str:
    return MODE_NATURAL if str(mode or "").strip().lower() == MODE_NATURAL \
        else MODE_NOMINA


def _iso_to_date(iso: str | None) -> date | None:
    if not iso:
        return None
    try:
        parts = str(iso)[:10].split("-")
        return date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        return None


def period_of(d: date, mode: str = MODE_NOMINA) -> tuple[int, int]:
    """Periodo (year, month) al que pertenece la fecha segun el modo.

    - nomina: etiquetado por el mes del dia 15 (cierre); dia>=16 -> mes sig.
    - natural: el propio mes de la fecha.
    """
    if normalize_mode(mode) == MODE_NATURAL:
        return (d.year, d.month)
    if d.day >= 16:
        m = d.month + 1
        y = d.year
        if m > 12:
            m = 1
            y += 1
        return (y, m)
    return (d.year, d.month)


def period_bounds(
    year: int, month: int, mode: str = MODE_NOMINA
) -> tuple[date, date]:
    """Inicio y fin del periodo.

    - nomina: 16 del mes anterior .. 15 del mes de cierre.
    - natural: 1 .. ultimo dia del mes.
    """
    if normalize_mode(mode) == MODE_NATURAL:
        last = _calendar.monthrange(year, month)[1]
        return date(year, month, 1), date(year, month, last)
    end = date(year, month, 15)
    pm = month - 1
    py = year
    if pm < 1:
        pm = 12
        py -= 1
    start = date(py, pm, 16)
    return start, end


def future_threshold(today: date | None = None) -> date:
    """Fecha limite a partir de la cual un parte se considera FUTURO: el fin
    del mes en curso tomando la convencion (natural o nomina) que termine MAS
    TARDE. Hoy 19/06 -> max(30/06 natural, 15/07 nomina) = 15/07."""
    d = today or date.today()
    yn, mn = period_of(d, MODE_NATURAL)
    _, end_nat = period_bounds(yn, mn, MODE_NATURAL)
    yp, mp = period_of(d, MODE_NOMINA)
    _, end_nom = period_bounds(yp, mp, MODE_NOMINA)
    return max(end_nat, end_nom)


def is_future_fecha(fecha_iso: str | None, today: date | None = None) -> bool:
    """True si la fecha (YYYY-MM-DD) es posterior al umbral de mes en curso."""
    if not fecha_iso:
        return False
    try:
        parts = str(fecha_iso)[:10].split("-")
        fd = date(int(parts[0]), int(parts[1]), int(parts[2]))
    except (ValueError, IndexError):
        return False
    return fd > future_threshold(today)


@dataclass
class PeriodOption:
    key: str    # "2026-04"
    label: str  # "Abril 2026"


@dataclass
class DayObra:
    """Desglose de horas de UNA obra dentro de un dia (para la tarjeta)."""
    obra_codigo: str | None
    obra_nombre: str | None
    normal_h: float
    extra_h: float
    incidencias: int


@dataclass
class DayCell:
    date_iso: str | None
    day: int | None
    in_period: bool
    is_weekend: bool
    is_holiday: bool
    holiday_name: str | None
    normal_h: float
    extra_h: float
    incidencias: int
    obras: list[DayObra] = field(default_factory=list)


@dataclass
class CalendarMonth:
    period_key: str
    label: str
    range_label: str
    weeks: list[list[DayCell]] = field(default_factory=list)
    total_normal: float = 0.0
    total_extra: float = 0.0
    total_incidencias: int = 0


def build_period_options(
    fechas_iso: Iterable[str | None], mode: str = MODE_NOMINA
) -> list[PeriodOption]:
    periods: set[tuple[int, int]] = set()
    for iso in fechas_iso:
        d = _iso_to_date(iso)
        if d is not None:
            periods.add(period_of(d, mode))
    return [
        PeriodOption(key=f"{y:04d}-{m:02d}", label=f"{_MESES[m]} {y}")
        for (y, m) in sorted(periods, reverse=True)
    ]


def parse_period_key(key: str | None) -> tuple[int, int] | None:
    if not key:
        return None
    try:
        y_s, m_s = str(key).split("-")
        y, m = int(y_s), int(m_s)
        if 1 <= m <= 12:
            return (y, m)
    except (ValueError, AttributeError):
        return None
    return None


def build_calendar(
    *,
    year: int,
    month: int,
    per_day: dict[str, dict[str, float]],
    holiday_name: Callable[[date], str | None] | None = None,
    mode: str = MODE_NOMINA,
) -> CalendarMonth:
    start, end = period_bounds(year, month, mode)

    # Rejilla alineada a lunes: rellena la primera y ultima semana.
    grid_start = start - timedelta(days=start.weekday())          # lunes
    grid_end = end + timedelta(days=(6 - end.weekday()))          # domingo

    weeks: list[list[DayCell]] = []
    week: list[DayCell] = []
    total_n = total_e = 0.0
    total_inc = 0
    d = grid_start
    while d <= grid_end:
        in_period = start <= d <= end
        iso = d.isoformat()
        agg = per_day.get(iso, {}) if in_period else {}
        n = float(agg.get("normal", 0.0))
        e = float(agg.get("extra", 0.0))
        inc = int(agg.get("incidencias", 0))
        obras = agg.get("obras", []) if in_period else []
        name = holiday_name(d) if (in_period and holiday_name) else None
        week.append(
            DayCell(
                date_iso=iso,
                day=d.day,
                in_period=in_period,
                is_weekend=d.weekday() >= 5,
                is_holiday=bool(name),
                holiday_name=name,
                normal_h=n,
                extra_h=e,
                incidencias=inc,
                obras=obras,
            )
        )
        if in_period:
            total_n += n
            total_e += e
            total_inc += inc
        if len(week) == 7:
            weeks.append(week)
            week = []
        d += timedelta(days=1)
    if week:
        weeks.append(week)

    return CalendarMonth(
        period_key=f"{year:04d}-{month:02d}",
        label=f"{_MESES[month]} {year}",
        range_label=f"{start.strftime('%d/%m/%Y')} – {end.strftime('%d/%m/%Y')}",
        weeks=weeks,
        total_normal=round(total_n, 2),
        total_extra=round(total_e, 2),
        total_incidencias=total_inc,
    )
