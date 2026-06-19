# infrastructure/database/parte_repository.py
"""Repositorio del portal (sv4) sobre la BBDD 'partes'.

Lee y escribe las mismas tablas que crea sv3 (parte_documents = parte
diario; parte_registros = empleado x tipo de hora). create_all
idempotente defensivo por si el portal arranca antes que sv3.

Expone:
  - list_workers(...)      -> una fila agregada por TRABAJADOR (desde
                              parte_registros, sumando horas de todos los
                              partes diarios).
  - get_worker(key)        -> registros del trabajador agrupados por parte
                              (dia), con la firma/obra del parte.
  - list_partes(...)       -> una fila por PARTE DIARIO (para revision/firma).
  - get_parte(document_id) -> cabecera + empleados del parte diario.
  - update_registro / set_registro_hora
  - approve_document / unapprove_document / delete_document
"""
from __future__ import annotations

import logging
import json
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from infrastructure.database.orm_models import (
    Base,
    EmpleadoAliasOrm,
    ParteDocumentOrm,
    ParteRegistroOrm,
    UndoLogOrm,
)
from infrastructure.database.session_factory import SessionFactory
from application.services import text_match as tm
from application.services.calendar_builder import (
    build_period_options,
    parse_period_key,
    period_bounds,
)

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------ #
# DTOs de presentacion (los consume Jinja).
# ------------------------------------------------------------------ #
@dataclass
class WorkerRow:
    worker_key: str
    nombre: str
    codigo: Optional[str]
    dni: Optional[str]
    categoria: Optional[str]
    empleado_ide: Optional[int]
    matched: bool
    num_partes: int
    num_registros: int
    horas_normales: float
    horas_extra: float
    num_incidencias: int


@dataclass
class RegistroView:
    id: int
    document_id: str
    fecha: Optional[str]
    obra_codigo: Optional[str]
    obra_nombre: Optional[str]
    categoria: Optional[str]
    tipo_hora: Optional[str]
    es_incidencia: bool
    incidencia_codigo: Optional[str]
    incidencia_texto: Optional[str]
    horas: Optional[float]
    hora_ide: Optional[int]
    hora_codigo: Optional[str]
    hora_descripcion: Optional[str]
    hora_ext: Optional[int]
    hora_match_method: Optional[str]
    confianza_pct: Optional[float]
    # Datos del parte al que pertenece (para mostrar contexto/firma).
    parte_firmado: bool
    parte_firmante_rol: Optional[str]
    parte_aprobado: bool
    # Nombre del trabajador (para la vista por obra; opcional).
    trabajador_nombre: Optional[str] = None
    # True si el parte tiene PDF accesible en SharePoint (drive+item).
    tiene_pdf: bool = False


@dataclass
class WorkerDetail:
    worker_key: str
    nombre: str
    codigo: Optional[str]
    dni: Optional[str]
    empleado_ide: Optional[int]
    matched: bool
    horas_normales: float
    horas_extra: float
    num_incidencias: int
    registros: list[RegistroView] = field(default_factory=list)


@dataclass
class ParteRow:
    document_id: str
    fecha: Optional[str]
    obra_codigo: Optional[str]
    obra_nombre: Optional[str]
    encargado_nombre: Optional[str]
    num_empleados: int
    num_registros: int
    horas_normales: float
    horas_extra: float
    firmado: bool
    firmante_rol: Optional[str]
    review_required: Optional[bool]
    approved: bool


@dataclass
class ParteEmpleadoView:
    nombre_leido: Optional[str]
    categoria: Optional[str]
    empleado_ide: Optional[int]
    empleado_nombre: Optional[str]
    matched: bool
    registros: list[RegistroView] = field(default_factory=list)


@dataclass
class ParteDetail:
    document_id: str
    fecha: Optional[str]
    fecha_int: Optional[int]
    obra_ide: Optional[int]
    obra_codigo: Optional[str]
    obra_nombre: Optional[str]
    obra_numero_leido: Optional[str]
    encargado_nombre: Optional[str]
    jefe_obra_nombre: Optional[str]
    firmado: bool
    firma_encargado: bool
    firma_jefe_obra: bool
    firma_administracion: bool
    firmante_rol: Optional[str]
    firmante_nombre: Optional[str]
    review_required: Optional[bool]
    approved: bool
    approved_by: Optional[str]
    source_filename: Optional[str]
    sharepoint_url: Optional[str]
    empleados: list[ParteEmpleadoView] = field(default_factory=list)


# ------------------------------------------------------------------ #
# DTOs de la vista por OBRA.
# ------------------------------------------------------------------ #
@dataclass
class ObraRow:
    obra_key: str
    obra_codigo: Optional[str]
    obra_nombre: Optional[str]
    obra_ide: Optional[int]
    num_trabajadores: int
    num_partes: int
    num_registros: int
    horas_normales: float
    horas_extra: float
    num_incidencias: int


@dataclass
class ObraDayCol:
    date_iso: str
    day: int
    is_weekend: bool
    is_holiday: bool
    holiday_name: Optional[str]


@dataclass
class ObraMatrixCell:
    date_iso: str
    label: str          # "8+2", "8", "+2", "V", ""
    normal: float
    extra: float
    has_inc: bool
    is_weekend: bool
    is_holiday: bool
    document_id: Optional[str] = None
    tiene_pdf: bool = False


@dataclass
class ObraMatrixRow:
    worker_key: str
    nombre: str
    matched: bool
    cells: list[ObraMatrixCell]
    total_normal: float
    total_extra: float


@dataclass
class ObraColTotal:
    date_iso: str
    normal: float
    extra: float


@dataclass
class ObraDetail:
    obra_key: str
    obra_codigo: Optional[str]
    obra_nombre: Optional[str]
    obra_ide: Optional[int]
    mode: str
    period_key: Optional[str]
    period_label: Optional[str]
    range_label: Optional[str]
    period_options: list[Any]
    days: list[ObraDayCol]
    rows: list[ObraMatrixRow]
    col_totals: list[ObraColTotal]
    total_normal: float
    total_extra: float
    total_incidencias: int
    registros: list[RegistroView] = field(default_factory=list)


# ------------------------------------------------------------------ #
# Helpers.
# ------------------------------------------------------------------ #
def _strip_accents(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _norm(text: str | None) -> str:
    if not text:
        return ""
    return " ".join(_strip_accents(str(text)).upper().split())


def worker_key_for_registro(reg: ParteRegistroOrm) -> str:
    """Clave estable y URL-safe para agrupar registros por trabajador."""
    if reg.empleado_ide is not None:
        return f"emp-{reg.empleado_ide}"
    nombre = _norm(reg.trabajador_nombre_leido)
    if nombre:
        return "nom-" + nombre.replace(" ", "_")
    return "sin-trabajador"


# ------------------------------------------------------------------ #
# DESHACER: snapshots de filas (estado ANTERIOR) y su restauracion.
# Un snapshot de registro captura TODOS los campos que cualquier accion
# puede tocar, asi la restauracion es uniforme para cualquier tipo de cambio.
# ------------------------------------------------------------------ #
_REG_UNDO_FIELDS = (
    "empleado_ide", "empleado_codigo", "empleado_nombre", "empleado_dni",
    "tipo_hora", "horas", "es_incidencia",
    "hora_ide", "hora_codigo", "hora_descripcion", "hora_ext",
    "hora_precio_coste", "hora_precio_nomina", "hora_match_method",
    "fecha", "fecha_int", "obra_ide", "obra_codigo", "obra_nombre",
)
_DOC_UNDO_FIELDS = (
    "fecha", "fecha_int", "obra_ide", "obra_codigo", "obra_nombre",
    "obra_match_method", "obra_match_score",
)


def _reg_snapshot(reg: ParteRegistroOrm) -> dict:
    snap = {"id": reg.id}
    for f in _REG_UNDO_FIELDS:
        snap[f] = getattr(reg, f, None)
    return snap


def _apply_reg_snapshot(reg: ParteRegistroOrm, snap: dict) -> None:
    for f in _REG_UNDO_FIELDS:
        if f in snap:
            setattr(reg, f, snap[f])


def _doc_snapshot(doc: ParteDocumentOrm) -> dict:
    snap = {"id": doc.id}
    for f in _DOC_UNDO_FIELDS:
        snap[f] = getattr(doc, f, None)
    return snap


def _apply_doc_snapshot(doc: ParteDocumentOrm, snap: dict) -> None:
    for f in _DOC_UNDO_FIELDS:
        if f in snap:
            setattr(doc, f, snap[f])


def _reg_label(reg: ParteRegistroOrm) -> str:
    """Etiqueta legible de un registro para el historial."""
    nom = reg.empleado_nombre or reg.trabajador_nombre_leido or "trabajador"
    return f"{nom} ({reg.fecha or '?'})"


def _fmt_h(x: float) -> str:
    """Formatea horas sin decimales superfluos: 8.0->'8', 8.5->'8.5'."""
    if x is None:
        return ""
    if float(x).is_integer():
        return str(int(x))
    return ("%g" % round(float(x), 2))


def _cell_label(normal: float, extra: float, inc_codes: list[str]) -> str:
    """Etiqueta de celda de la matriz: '8+2', '8', '+2', 'V', o combinada."""
    parts: list[str] = []
    if normal and extra:
        parts.append(f"{_fmt_h(normal)}+{_fmt_h(extra)}")
    elif normal:
        parts.append(_fmt_h(normal))
    elif extra:
        parts.append(f"+{_fmt_h(extra)}")
    if inc_codes:
        uniq = sorted(set(c for c in inc_codes if c))
        if uniq:
            parts.append("/".join(uniq))
    return " ".join(parts)


def obra_key_for_registro(reg: ParteRegistroOrm) -> str:
    """Clave estable y URL-safe para agrupar registros por obra."""
    if reg.obra_ide is not None:
        return f"obr-{reg.obra_ide}"
    if reg.obra_codigo:
        return "cod-" + _norm(reg.obra_codigo).replace(" ", "_")
    nombre = _norm(reg.obra_nombre)
    if nombre:
        return "nom-" + nombre.replace(" ", "_")
    return "sin-obra"


def _is_extra(reg: ParteRegistroOrm) -> bool:
    # Mismo criterio que el badge de la lista: es extra si el tipo es 'extra'
    # o el auxhor resuelto es extra (ext=1). NO depende de que hora_ext sea
    # exactamente 1 (puede quedar 0/None y el tipo seguir siendo 'extra').
    return (reg.tipo_hora or "") == "extra" or reg.hora_ext == 1


class ParteReviewRepository:
    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def initialize(self) -> bool:
        try:
            Base.metadata.create_all(self._session_factory.engine)
            with self._session_factory.engine.begin() as conn:
                from sqlalchemy import text
                for col in (
                    "firma_encargado", "firma_jefe_obra", "firma_administracion",
                ):
                    conn.execute(text(
                        f"ALTER TABLE parte_documents ADD COLUMN IF NOT EXISTS "
                        f"{col} BOOLEAN NOT NULL DEFAULT false"
                    ))
                conn.execute(text(
                    "ALTER TABLE parte_documents ADD COLUMN IF NOT EXISTS "
                    "sharepoint_url TEXT"
                ))
                conn.execute(text(
                    "ALTER TABLE parte_documents ADD COLUMN IF NOT EXISTS "
                    "sharepoint_item_id VARCHAR(255)"
                ))
                conn.execute(text(
                    "ALTER TABLE parte_documents ADD COLUMN IF NOT EXISTS "
                    "sharepoint_drive_id VARCHAR(255)"
                ))
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS empleado_alias ("
                    "  nombre_norm VARCHAR(300) PRIMARY KEY,"
                    "  empleado_ide INTEGER NOT NULL,"
                    "  empleado_codigo VARCHAR(60),"
                    "  empleado_nombre TEXT,"
                    "  empleado_dni VARCHAR(40),"
                    "  created_at_utc VARCHAR(40) NOT NULL,"
                    "  created_by VARCHAR(120)"
                    ")"
                ))
                conn.execute(text(
                    "CREATE TABLE IF NOT EXISTS undo_log ("
                    "  id SERIAL PRIMARY KEY,"
                    "  created_at_utc VARCHAR(40) NOT NULL,"
                    "  action VARCHAR(40) NOT NULL,"
                    "  description TEXT NOT NULL,"
                    "  payload TEXT NOT NULL,"
                    "  undone BOOLEAN NOT NULL DEFAULT false,"
                    "  actor VARCHAR(120)"
                    ")"
                ))
            return True
        except Exception:
            logger.exception("[parte-repo-sv4] create_all fallo (continua).")
            return False

    # ----------------------------------------------------------------- #
    # Listado por TRABAJADOR.
    # ----------------------------------------------------------------- #
    def list_workers(
        self,
        *,
        search: str | None = None,
        include_deleted: bool = False,
    ) -> list[WorkerRow]:
        with self._session_factory.create_session() as session:
            stmt = (
                select(ParteRegistroOrm)
                .join(ParteDocumentOrm)
                .options(selectinload(ParteRegistroOrm.document))
            )
            if not include_deleted:
                stmt = stmt.where(ParteDocumentOrm.is_active.is_(True))
            regs = list(session.execute(stmt).scalars().all())

        groups: dict[str, dict[str, Any]] = {}
        for reg in regs:
            key = worker_key_for_registro(reg)
            g = groups.get(key)
            if g is None:
                g = {
                    "worker_key": key,
                    "nombre": reg.empleado_nombre
                    or reg.trabajador_nombre_leido
                    or "(sin identificar)",
                    "codigo": reg.empleado_codigo,
                    "dni": reg.empleado_dni,
                    "categoria": reg.categoria,
                    "empleado_ide": reg.empleado_ide,
                    "matched": reg.empleado_ide is not None,
                    "docs": set(),
                    "num_registros": 0,
                    "horas_normales": 0.0,
                    "horas_extra": 0.0,
                    "num_incidencias": 0,
                }
                groups[key] = g
            g["docs"].add(reg.document_id)
            g["num_registros"] += 1
            if reg.es_incidencia:
                g["num_incidencias"] += 1
            elif _is_extra(reg):
                g["horas_extra"] += reg.horas or 0.0
            else:
                g["horas_normales"] += reg.horas or 0.0
            if not g["categoria"] and reg.categoria:
                g["categoria"] = reg.categoria

        rows = [
            WorkerRow(
                worker_key=g["worker_key"],
                nombre=g["nombre"],
                codigo=g["codigo"],
                dni=g["dni"],
                categoria=g["categoria"],
                empleado_ide=g["empleado_ide"],
                matched=g["matched"],
                num_partes=len(g["docs"]),
                num_registros=g["num_registros"],
                horas_normales=round(g["horas_normales"], 2),
                horas_extra=round(g["horas_extra"], 2),
                num_incidencias=g["num_incidencias"],
            )
            for g in groups.values()
        ]

        if search:
            needle = _norm(search)
            rows = [
                r for r in rows
                if needle in _norm(r.nombre)
                or needle in _norm(r.dni)
                or needle in _norm(r.codigo)
            ]
        rows.sort(key=lambda r: (0 if r.matched else 1, _norm(r.nombre)))
        return rows

    def get_worker(self, worker_key: str) -> WorkerDetail | None:
        with self._session_factory.create_session() as session:
            stmt = (
                select(ParteRegistroOrm)
                .join(ParteDocumentOrm)
                .options(selectinload(ParteRegistroOrm.document))
                .where(ParteDocumentOrm.is_active.is_(True))
            )
            regs = [
                r for r in session.execute(stmt).scalars().all()
                if worker_key_for_registro(r) == worker_key
            ]

        if not regs:
            return None

        head = regs[0]
        detail = WorkerDetail(
            worker_key=worker_key,
            nombre=head.empleado_nombre or head.trabajador_nombre_leido
            or "(sin identificar)",
            codigo=head.empleado_codigo,
            dni=head.empleado_dni,
            empleado_ide=head.empleado_ide,
            matched=head.empleado_ide is not None,
            horas_normales=0.0,
            horas_extra=0.0,
            num_incidencias=0,
        )
        regs.sort(key=lambda r: (r.fecha_int or 0, r.line_index))
        for reg in regs:
            if reg.es_incidencia:
                detail.num_incidencias += 1
            elif _is_extra(reg):
                detail.horas_extra += reg.horas or 0.0
            else:
                detail.horas_normales += reg.horas or 0.0
            detail.registros.append(_registro_view(reg))
        detail.horas_normales = round(detail.horas_normales, 2)
        detail.horas_extra = round(detail.horas_extra, 2)
        return detail

    # ----------------------------------------------------------------- #
    # Vista por OBRA.
    # ----------------------------------------------------------------- #
    def list_obras(self, *, search: str | None = None) -> list[ObraRow]:
        with self._session_factory.create_session() as session:
            stmt = (
                select(ParteRegistroOrm)
                .join(ParteDocumentOrm)
                .options(selectinload(ParteRegistroOrm.document))
                .where(ParteDocumentOrm.is_active.is_(True))
            )
            regs = list(session.execute(stmt).scalars().all())

        groups: dict[str, dict[str, Any]] = {}
        for reg in regs:
            key = obra_key_for_registro(reg)
            g = groups.get(key)
            if g is None:
                g = {
                    "obra_key": key,
                    "obra_codigo": reg.obra_codigo,
                    "obra_nombre": reg.obra_nombre,
                    "obra_ide": reg.obra_ide,
                    "trabajadores": set(),
                    "docs": set(),
                    "num_registros": 0,
                    "horas_normales": 0.0,
                    "horas_extra": 0.0,
                    "num_incidencias": 0,
                }
                groups[key] = g
            g["trabajadores"].add(worker_key_for_registro(reg))
            g["docs"].add(reg.document_id)
            g["num_registros"] += 1
            if reg.es_incidencia:
                g["num_incidencias"] += 1
            elif _is_extra(reg):
                g["horas_extra"] += reg.horas or 0.0
            else:
                g["horas_normales"] += reg.horas or 0.0
            if not g["obra_codigo"] and reg.obra_codigo:
                g["obra_codigo"] = reg.obra_codigo
            if not g["obra_nombre"] and reg.obra_nombre:
                g["obra_nombre"] = reg.obra_nombre

        rows = [
            ObraRow(
                obra_key=g["obra_key"],
                obra_codigo=g["obra_codigo"],
                obra_nombre=g["obra_nombre"],
                obra_ide=g["obra_ide"],
                num_trabajadores=len(g["trabajadores"]),
                num_partes=len(g["docs"]),
                num_registros=g["num_registros"],
                horas_normales=round(g["horas_normales"], 2),
                horas_extra=round(g["horas_extra"], 2),
                num_incidencias=g["num_incidencias"],
            )
            for g in groups.values()
        ]
        if search:
            needle = _norm(search)
            rows = [
                r for r in rows
                if needle in _norm(r.obra_codigo)
                or needle in _norm(r.obra_nombre)
            ]
        rows.sort(key=lambda r: (_norm(r.obra_codigo) or "~", _norm(r.obra_nombre)))
        return rows

    def get_obra(
        self,
        obra_key: str,
        *,
        period_key: str | None = None,
        mode: str = "nomina",
        holiday_name: Callable[[date], str | None] | None = None,
    ) -> ObraDetail | None:
        with self._session_factory.create_session() as session:
            stmt = (
                select(ParteRegistroOrm)
                .join(ParteDocumentOrm)
                .options(selectinload(ParteRegistroOrm.document))
                .where(ParteDocumentOrm.is_active.is_(True))
            )
            regs = [
                r for r in session.execute(stmt).scalars().all()
                if obra_key_for_registro(r) == obra_key
            ]
            if not regs:
                return None

            head = next(
                (r for r in regs if r.obra_codigo or r.obra_nombre), regs[0]
            )
            obra_codigo = head.obra_codigo
            obra_nombre = head.obra_nombre
            obra_ide = head.obra_ide

            period_options = build_period_options([r.fecha for r in regs], mode)
            selected = parse_period_key(period_key)
            if selected is None and period_options:
                selected = parse_period_key(period_options[0].key)

            views = [_registro_view(r) for r in regs]

        if selected is None:
            # Sin fechas validas: sin matriz, registros sueltos.
            return ObraDetail(
                obra_key=obra_key, obra_codigo=obra_codigo,
                obra_nombre=obra_nombre, obra_ide=obra_ide, mode=mode,
                period_key=None, period_label=None, range_label=None,
                period_options=period_options, days=[], rows=[], col_totals=[],
                total_normal=0.0, total_extra=0.0, total_incidencias=0,
                registros=sorted(
                    views, key=lambda v: ((v.fecha or ""), v.trabajador_nombre or "")
                ),
            )

        y, m = selected
        start, end = period_bounds(y, m, mode)

        # Columnas = dias del periodo (16->15).
        days: list[ObraDayCol] = []
        d = start
        while d <= end:
            hn = holiday_name(d) if holiday_name else None
            days.append(ObraDayCol(
                date_iso=d.isoformat(), day=d.day,
                is_weekend=d.weekday() >= 5,
                is_holiday=bool(hn), holiday_name=hn,
            ))
            d += timedelta(days=1)
        day_index = {dc.date_iso: i for i, dc in enumerate(days)}

        def in_period(iso: str | None) -> bool:
            return bool(iso) and iso in day_index

        # Filtra registros del periodo y agrega por (trabajador, dia).
        period_views = [v for v in views if in_period(v.fecha)]
        # agg[worker_key] = {nombre, matched, days: {iso: {normal,extra,inc:set}}}
        agg: dict[str, dict[str, Any]] = {}
        for reg in regs:
            if not in_period(reg.fecha):
                continue
            wk = worker_key_for_registro(reg)
            w = agg.get(wk)
            if w is None:
                w = {
                    "nombre": reg.empleado_nombre or reg.trabajador_nombre_leido
                    or "(sin identificar)",
                    "matched": reg.empleado_ide is not None,
                    "days": {},
                }
                agg[wk] = w
            slot = w["days"].setdefault(
                reg.fecha, {"normal": 0.0, "extra": 0.0, "inc": [],
                           "doc": None, "pdf": False}
            )
            if slot["doc"] is None:
                slot["doc"] = reg.document_id
                slot["pdf"] = bool(
                    reg.document is not None
                    and reg.document.sharepoint_drive_id
                    and reg.document.sharepoint_item_id
                )
            if reg.es_incidencia:
                if reg.incidencia_codigo:
                    slot["inc"].append(reg.incidencia_codigo)
            elif _is_extra(reg):
                slot["extra"] += reg.horas or 0.0
            else:
                slot["normal"] += reg.horas or 0.0

        col_n = [0.0] * len(days)
        col_e = [0.0] * len(days)
        total_n = total_e = 0.0
        total_inc = 0
        rows: list[ObraMatrixRow] = []
        for wk, w in agg.items():
            cells: list[ObraMatrixCell] = []
            row_n = row_e = 0.0
            for dc in days:
                slot = w["days"].get(dc.date_iso)
                if slot is None:
                    cells.append(ObraMatrixCell(
                        date_iso=dc.date_iso, label="", normal=0.0, extra=0.0,
                        has_inc=False, is_weekend=dc.is_weekend,
                        is_holiday=dc.is_holiday,
                    ))
                    continue
                n = float(slot["normal"]); e = float(slot["extra"])
                inc = slot["inc"]
                cells.append(ObraMatrixCell(
                    date_iso=dc.date_iso,
                    label=_cell_label(n, e, inc),
                    normal=n, extra=e, has_inc=bool(inc),
                    is_weekend=dc.is_weekend, is_holiday=dc.is_holiday,
                    document_id=slot.get("doc"), tiene_pdf=bool(slot.get("pdf")),
                ))
                idx = day_index[dc.date_iso]
                col_n[idx] += n; col_e[idx] += e
                row_n += n; row_e += e
                total_n += n; total_e += e
                total_inc += len(inc)
            rows.append(ObraMatrixRow(
                worker_key=wk, nombre=w["nombre"], matched=w["matched"],
                cells=cells, total_normal=round(row_n, 2),
                total_extra=round(row_e, 2),
            ))
        rows.sort(key=lambda r: (0 if r.matched else 1, _norm(r.nombre)))

        col_totals = [
            ObraColTotal(date_iso=days[i].date_iso,
                         normal=round(col_n[i], 2), extra=round(col_e[i], 2))
            for i in range(len(days))
        ]

        opt = next((o for o in period_options if o.key == f"{y:04d}-{m:02d}"), None)
        period_label = opt.label if opt else f"{y:04d}-{m:02d}"
        range_label = (
            f"{start.day:02d}/{start.month:02d} – {end.day:02d}/{end.month:02d}"
        )

        return ObraDetail(
            obra_key=obra_key, obra_codigo=obra_codigo, obra_nombre=obra_nombre,
            obra_ide=obra_ide, mode=mode, period_key=f"{y:04d}-{m:02d}",
            period_label=period_label, range_label=range_label,
            period_options=period_options, days=days, rows=rows,
            col_totals=col_totals, total_normal=round(total_n, 2),
            total_extra=round(total_e, 2), total_incidencias=total_inc,
            registros=sorted(
                period_views,
                key=lambda v: ((v.fecha or ""), v.trabajador_nombre or "")
            ),
        )

    # ----------------------------------------------------------------- #
    # Listado por PARTE DIARIO.
    # ----------------------------------------------------------------- #
    def list_partes(
        self,
        *,
        search: str | None = None,
        only_pending: bool = False,
        include_deleted: bool = False,
    ) -> list[ParteRow]:
        with self._session_factory.create_session() as session:
            stmt = select(ParteDocumentOrm).options(
                selectinload(ParteDocumentOrm.registros)
            )
            if not include_deleted:
                stmt = stmt.where(ParteDocumentOrm.is_active.is_(True))
            docs = list(session.execute(stmt).scalars().all())

        rows: list[ParteRow] = []
        for doc in docs:
            empleados = {
                r.empleado_ide or r.trabajador_nombre_leido
                for r in doc.registros
            }
            horas_n = sum(
                (r.horas or 0.0) for r in doc.registros
                if not r.es_incidencia and not _is_extra(r)
            )
            horas_e = sum(
                (r.horas or 0.0) for r in doc.registros
                if not r.es_incidencia and _is_extra(r)
            )
            rows.append(
                ParteRow(
                    document_id=doc.id,
                    fecha=doc.fecha,
                    obra_codigo=doc.obra_codigo or doc.obra_numero_leido,
                    obra_nombre=doc.obra_nombre or doc.obra_nombre_leido,
                    encargado_nombre=doc.encargado_nombre,
                    num_empleados=len(empleados),
                    num_registros=len(doc.registros),
                    horas_normales=round(horas_n, 2),
                    horas_extra=round(horas_e, 2),
                    firmado=doc.firmado,
                    firmante_rol=doc.firmante_rol,
                    review_required=doc.review_required,
                    approved=doc.approved,
                )
            )

        if only_pending:
            rows = [r for r in rows if not r.approved]
        if search:
            needle = _norm(search)
            rows = [
                r for r in rows
                if needle in _norm(r.obra_codigo)
                or needle in _norm(r.obra_nombre)
                or needle in _norm(r.encargado_nombre)
                or needle in _norm(r.fecha)
            ]
        rows.sort(key=lambda r: (r.fecha or ""), reverse=True)
        return rows

    def get_parte(self, document_id: str) -> ParteDetail | None:
        with self._session_factory.create_session() as session:
            stmt = (
                select(ParteDocumentOrm)
                .options(selectinload(ParteDocumentOrm.registros))
                .where(ParteDocumentOrm.id == document_id)
            )
            doc = session.execute(stmt).scalar_one_or_none()
            if doc is None or not doc.is_active:
                return None

            detail = ParteDetail(
                document_id=doc.id,
                fecha=doc.fecha,
                fecha_int=doc.fecha_int,
                obra_ide=doc.obra_ide,
                obra_codigo=doc.obra_codigo,
                obra_nombre=doc.obra_nombre or doc.obra_nombre_leido,
                obra_numero_leido=doc.obra_numero_leido,
                encargado_nombre=doc.encargado_nombre,
                jefe_obra_nombre=doc.jefe_obra_nombre,
                firmado=doc.firmado,
                firma_encargado=doc.firma_encargado,
                firma_jefe_obra=doc.firma_jefe_obra,
                firma_administracion=doc.firma_administracion,
                firmante_rol=doc.firmante_rol,
                firmante_nombre=doc.firmante_nombre,
                review_required=doc.review_required,
                approved=doc.approved,
                approved_by=doc.approved_by,
                source_filename=doc.source_filename,
                sharepoint_url=doc.sharepoint_url,
            )

            # Agrupar registros por empleado (linea de la tabla PERSONAL).
            by_emp: dict[Any, ParteEmpleadoView] = {}
            order: list[Any] = []
            for reg in sorted(
                doc.registros, key=lambda r: (r.empleado_line_no or 0, r.line_index)
            ):
                key = reg.empleado_ide or (reg.trabajador_nombre_leido or "") \
                    or reg.empleado_line_no
                if key not in by_emp:
                    by_emp[key] = ParteEmpleadoView(
                        nombre_leido=reg.trabajador_nombre_leido,
                        categoria=reg.categoria,
                        empleado_ide=reg.empleado_ide,
                        empleado_nombre=reg.empleado_nombre,
                        matched=reg.empleado_ide is not None,
                    )
                    order.append(key)
                by_emp[key].registros.append(_registro_view(reg))
            detail.empleados = [by_emp[k] for k in order]
            return detail

    # ----------------------------------------------------------------- #
    # Edicion de registros.
    # ----------------------------------------------------------------- #
    def update_registro(
        self,
        *,
        registro_id: int,
        tipo_hora: str | None = None,
        horas: float | None = None,
    ) -> bool:
        with self._session_factory.create_session() as session:
            reg = session.get(ParteRegistroOrm, registro_id)
            if reg is None:
                return False
            snap = _reg_snapshot(reg)
            label = _reg_label(reg)
            if tipo_hora is not None:
                reg.tipo_hora = tipo_hora or None
            if horas is not None:
                reg.horas = horas
            self._record_undo(
                session, action="registro_edit",
                description=f"Editar horas · {label}", registros=[snap],
            )
            session.commit()
        return True

    def set_registro_hora(
        self,
        *,
        registro_id: int,
        hora_ide: int | None,
        hora_codigo: str | None,
        hora_descripcion: str | None,
        hora_ext: int | None,
        hora_precio_coste: float | None,
        hora_precio_nomina: float | None,
    ) -> bool:
        with self._session_factory.create_session() as session:
            reg = session.get(ParteRegistroOrm, registro_id)
            if reg is None:
                return False
            snap = _reg_snapshot(reg)
            label = _reg_label(reg)
            reg.hora_ide = hora_ide
            reg.hora_codigo = hora_codigo
            reg.hora_descripcion = hora_descripcion
            reg.hora_ext = hora_ext
            reg.hora_precio_coste = hora_precio_coste
            reg.hora_precio_nomina = hora_precio_nomina
            reg.hora_match_method = "manual"
            if hora_ext is not None and not reg.es_incidencia:
                reg.tipo_hora = "extra" if hora_ext == 1 else "normal"
            self._record_undo(
                session, action="registro_hora",
                description=f"Cambiar codigo de hora · {label}",
                registros=[snap],
            )
            session.commit()
        return True

    # ----------------------------------------------------------------- #
    # Conciliacion de trabajadores SIN CASAR.
    # ----------------------------------------------------------------- #
    def list_unmatched_workers(self) -> list[dict]:
        """Nombres LEIDOS sin casar (empleado_ide NULL) en registros activos,
        agrupados por nombre normalizado, con conteo y obras/categorias."""
        with self._session_factory.create_session() as session:
            stmt = (
                select(ParteRegistroOrm)
                .join(ParteDocumentOrm)
                .where(ParteDocumentOrm.is_active.is_(True))
                .where(ParteRegistroOrm.empleado_ide.is_(None))
            )
            regs = list(session.execute(stmt).scalars().all())

        groups: dict[str, dict] = {}
        for r in regs:
            leido = (r.trabajador_nombre_leido or "").strip()
            norm = tm.normalize(leido)
            if not norm:
                continue
            g = groups.get(norm)
            if g is None:
                g = {
                    "nombre_leido": leido or norm,
                    "nombre_norm": norm,
                    "num_registros": 0,
                    "obras": set(),
                    "categorias": set(),
                }
                groups[norm] = g
            g["num_registros"] += 1
            if r.obra_codigo:
                g["obras"].add(r.obra_codigo)
            if r.categoria:
                g["categorias"].add(r.categoria)

        out = []
        for g in groups.values():
            out.append({
                "nombre_leido": g["nombre_leido"],
                "nombre_norm": g["nombre_norm"],
                "num_registros": g["num_registros"],
                "obras": sorted(g["obras"]),
                "categorias": sorted(g["categorias"]),
            })
        out.sort(key=lambda x: (-x["num_registros"], x["nombre_leido"].lower()))
        return out

    def count_unmatched_workers(self) -> int:
        return len(self.list_unmatched_workers())

    def backfill_empleado(
        self, *, nombre_leido: str, ide: int,
        codigo: str | None, nombre: str | None, dni: str | None,
    ) -> int:
        """Asigna el empleado a TODOS los registros activos sin casar cuyo
        nombre leido (normalizado) coincide. Devuelve nº de filas tocadas."""
        target = tm.normalize(nombre_leido)
        if not target:
            return 0
        with self._session_factory.create_session() as session:
            stmt = (
                select(ParteRegistroOrm)
                .join(ParteDocumentOrm)
                .where(ParteDocumentOrm.is_active.is_(True))
                .where(ParteRegistroOrm.empleado_ide.is_(None))
            )
            regs = list(session.execute(stmt).scalars().all())
            affected = [
                r for r in regs
                if tm.normalize(r.trabajador_nombre_leido) == target
            ]
            if not affected:
                return 0
            reg_snaps = [_reg_snapshot(r) for r in affected]
            alias_snaps = [self._alias_snapshot(session, target)]
            for r in affected:
                r.empleado_ide = ide
                r.empleado_codigo = codigo
                r.empleado_nombre = nombre
                r.empleado_dni = dni
            self._record_undo(
                session, action="empleado",
                description=f"Casar '{nombre_leido}' → "
                            f"{nombre or codigo or ide} ({len(affected)} reg.)",
                registros=reg_snaps, aliases=alias_snaps,
            )
            session.commit()
        return len(affected)

    def upsert_empleado_alias(
        self, *, nombre_leido: str, ide: int,
        codigo: str | None, nombre: str | None, dni: str | None,
        created_by: str | None = None,
    ) -> None:
        """Persiste/actualiza el alias (nombre leido -> empleado) para que la
        INGESTA futura case esa variante de forma exacta."""
        norm = tm.normalize(nombre_leido)
        if not norm:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._session_factory.create_session() as session:
            row = session.get(EmpleadoAliasOrm, norm)
            if row is None:
                row = EmpleadoAliasOrm(nombre_norm=norm, created_at_utc=now)
                session.add(row)
            row.empleado_ide = ide
            row.empleado_codigo = codigo
            row.empleado_nombre = nombre
            row.empleado_dni = dni
            row.created_by = created_by
            session.commit()

    # ----------------------------------------------------------------- #
    # DESHACER: registrar, restaurar y listar acciones.
    # ----------------------------------------------------------------- #
    def _alias_snapshot(self, session, norm: str) -> dict:
        row = session.get(EmpleadoAliasOrm, norm)
        if row is None:
            return {"nombre_norm": norm, "existed": False}
        return {
            "nombre_norm": norm, "existed": True,
            "empleado_ide": row.empleado_ide,
            "empleado_codigo": row.empleado_codigo,
            "empleado_nombre": row.empleado_nombre,
            "empleado_dni": row.empleado_dni,
            "created_at_utc": row.created_at_utc,
            "created_by": row.created_by,
        }

    def _apply_alias_snapshot(self, session, snap: dict) -> None:
        norm = snap.get("nombre_norm")
        if not norm:
            return
        row = session.get(EmpleadoAliasOrm, norm)
        if not snap.get("existed"):
            if row is not None:
                session.delete(row)
            return
        if row is None:
            row = EmpleadoAliasOrm(
                nombre_norm=norm,
                created_at_utc=snap.get("created_at_utc")
                or datetime.now(timezone.utc).isoformat(),
            )
            session.add(row)
        row.empleado_ide = snap.get("empleado_ide")
        row.empleado_codigo = snap.get("empleado_codigo")
        row.empleado_nombre = snap.get("empleado_nombre")
        row.empleado_dni = snap.get("empleado_dni")
        row.created_by = snap.get("created_by")

    def _record_undo(
        self, session, *, action: str, description: str,
        registros: list[dict] | None = None,
        documents: list[dict] | None = None,
        aliases: list[dict] | None = None,
    ) -> int:
        """Inserta una entrada de historial DENTRO de la sesion dada (atomico
        con la mutacion). Devuelve el id."""
        payload = {
            "registros": registros or [],
            "documents": documents or [],
            "aliases": aliases or [],
        }
        row = UndoLogOrm(
            created_at_utc=datetime.now(timezone.utc).isoformat(),
            action=action, description=description,
            payload=json.dumps(payload, default=str), undone=False,
        )
        session.add(row)
        session.flush()
        return row.id

    def list_undo(self, *, limit: int = 15) -> list[dict]:
        with self._session_factory.create_session() as session:
            stmt = (
                select(UndoLogOrm)
                .where(UndoLogOrm.undone.is_(False))
                .order_by(UndoLogOrm.id.desc())
                .limit(limit)
            )
            rows = list(session.execute(stmt).scalars().all())
            return [
                {"id": r.id, "action": r.action,
                 "description": r.description, "created_at": r.created_at_utc}
                for r in rows
            ]

    def count_undo(self) -> int:
        with self._session_factory.create_session() as session:
            stmt = select(UndoLogOrm).where(UndoLogOrm.undone.is_(False))
            return len(list(session.execute(stmt).scalars().all()))

    def undo_last(self) -> dict:
        """Deshace la accion no-deshecha mas reciente: restaura el estado
        anterior de registros/documento/alias y la marca como deshecha."""
        with self._session_factory.create_session() as session:
            stmt = (
                select(UndoLogOrm)
                .where(UndoLogOrm.undone.is_(False))
                .order_by(UndoLogOrm.id.desc())
                .limit(1)
            )
            row = session.execute(stmt).scalars().first()
            if row is None:
                return {"ok": False, "error": "No hay nada que deshacer."}
            description = row.description
            try:
                payload = json.loads(row.payload)
            except Exception:  # noqa: BLE001
                payload = {}
            for snap in payload.get("documents", []):
                doc = session.get(ParteDocumentOrm, snap.get("id"))
                if doc is not None:
                    _apply_doc_snapshot(doc, snap)
            for snap in payload.get("registros", []):
                reg = session.get(ParteRegistroOrm, snap.get("id"))
                if reg is not None:
                    _apply_reg_snapshot(reg, snap)
            for snap in payload.get("aliases", []):
                self._apply_alias_snapshot(session, snap)
            row.undone = True
            session.commit()
            remaining = len(list(session.execute(
                select(UndoLogOrm).where(UndoLogOrm.undone.is_(False))
            ).scalars().all()))
        return {"ok": True, "description": description, "remaining": remaining}

    def get_registro_leido(self, registro_id: int) -> str | None:
        with self._session_factory.create_session() as session:
            r = session.get(ParteRegistroOrm, registro_id)
            return r.trabajador_nombre_leido if r is not None else None

    def reassign_empleado_by_leido(
        self, *, nombre_leido: str, ide: int,
        codigo: str | None, nombre: str | None, dni: str | None,
    ) -> int:
        """Reasigna el empleado a TODOS los registros activos cuyo nombre
        leido (normalizado) coincide, ESTEN o no casados (correccion)."""
        target = tm.normalize(nombre_leido)
        if not target:
            return 0
        with self._session_factory.create_session() as session:
            stmt = (
                select(ParteRegistroOrm)
                .join(ParteDocumentOrm)
                .where(ParteDocumentOrm.is_active.is_(True))
            )
            affected = [
                r for r in session.execute(stmt).scalars().all()
                if tm.normalize(r.trabajador_nombre_leido) == target
            ]
            if not affected:
                return 0
            reg_snaps = [_reg_snapshot(r) for r in affected]
            alias_snaps = [self._alias_snapshot(session, target)]
            for r in affected:
                r.empleado_ide = ide
                r.empleado_codigo = codigo
                r.empleado_nombre = nombre
                r.empleado_dni = dni
            self._record_undo(
                session, action="empleado",
                description=f"Reasignar '{nombre_leido}' → "
                            f"{nombre or codigo or ide} ({len(affected)} reg.)",
                registros=reg_snaps, aliases=alias_snaps,
            )
            session.commit()
        return len(affected)

    def reassign_empleado_by_worker_key(
        self, *, worker_key: str, ide: int,
        codigo: str | None, nombre: str | None, dni: str | None,
    ) -> tuple[int, list[str]]:
        """Reasigna todos los registros activos del grupo (worker_key).
        Devuelve (filas, nombres_leidos_distintos) para escribir alias."""
        with self._session_factory.create_session() as session:
            stmt = (
                select(ParteRegistroOrm)
                .join(ParteDocumentOrm)
                .where(ParteDocumentOrm.is_active.is_(True))
            )
            affected = [
                r for r in session.execute(stmt).scalars().all()
                if worker_key_for_registro(r) == worker_key
            ]
            if not affected:
                return 0, []
            reg_snaps = [_reg_snapshot(r) for r in affected]
            leidos = sorted({
                r.trabajador_nombre_leido for r in affected
                if r.trabajador_nombre_leido
            })
            alias_snaps = [
                self._alias_snapshot(session, tm.normalize(l)) for l in leidos
            ]
            for r in affected:
                r.empleado_ide = ide
                r.empleado_codigo = codigo
                r.empleado_nombre = nombre
                r.empleado_dni = dni
            self._record_undo(
                session, action="empleado",
                description=f"Reasignar trabajador → "
                            f"{nombre or codigo or ide} ({len(affected)} reg.)",
                registros=reg_snaps, aliases=alias_snaps,
            )
            session.commit()
        return len(affected), leidos

    def get_sharepoint_ref(self, document_id: str) -> dict | None:
        """Datos para descargar el PDF de SharePoint por Graph."""
        with self._session_factory.create_session() as session:
            doc = session.get(ParteDocumentOrm, document_id)
            if doc is None:
                return None
            return {
                "drive_id": doc.sharepoint_drive_id,
                "item_id": doc.sharepoint_item_id,
                "url": doc.sharepoint_url,
                "filename": doc.source_filename,
            }

    # ----------------------------------------------------------------- #
    # Edicion de cabecera del parte (fecha / obra). Propaga a los
    # registros (desnormalizados) para que la vista de Trabajadores y el
    # calendario queden consistentes.
    # ----------------------------------------------------------------- #
    def update_parte_fecha(
        self, *, document_id: str, fecha_iso: str, fecha_int: int
    ) -> bool:
        with self._session_factory.create_session() as session:
            doc = session.get(ParteDocumentOrm, document_id)
            if doc is None:
                return False
            doc_snap = _doc_snapshot(doc)
            reg_snaps = [_reg_snapshot(r) for r in doc.registros]
            old = doc.fecha
            doc.fecha = fecha_iso
            doc.fecha_int = fecha_int
            for reg in doc.registros:
                reg.fecha = fecha_iso
                reg.fecha_int = fecha_int
            self._record_undo(
                session, action="parte_fecha",
                description=f"Cambiar fecha del parte {doc.obra_codigo or ''} "
                            f"({old or '?'} → {fecha_iso})",
                documents=[doc_snap], registros=reg_snaps,
            )
            session.commit()
        return True

    def update_parte_obra(
        self,
        *,
        document_id: str,
        obra_ide: int | None,
        obra_codigo: str | None,
        obra_nombre: str | None,
    ) -> bool:
        with self._session_factory.create_session() as session:
            doc = session.get(ParteDocumentOrm, document_id)
            if doc is None:
                return False
            doc_snap = _doc_snapshot(doc)
            reg_snaps = [_reg_snapshot(r) for r in doc.registros]
            doc.obra_ide = obra_ide
            doc.obra_codigo = obra_codigo
            doc.obra_nombre = obra_nombre
            doc.obra_match_method = "manual"
            doc.obra_match_score = 1.0
            for reg in doc.registros:
                reg.obra_ide = obra_ide
                reg.obra_codigo = obra_codigo
                reg.obra_nombre = obra_nombre
            self._record_undo(
                session, action="parte_obra",
                description=f"Cambiar obra del parte → {obra_codigo or obra_nombre or '?'}",
                documents=[doc_snap], registros=reg_snaps,
            )
            session.commit()
        return True

    # ----------------------------------------------------------------- #
    # Estado del parte (documento).
    # ----------------------------------------------------------------- #
    def approve_document(self, *, document_id: str, approved_by: str | None) -> bool:
        return self._set_approval(document_id, True, approved_by)

    def unapprove_document(self, *, document_id: str) -> bool:
        return self._set_approval(document_id, False, None)

    def _set_approval(
        self, document_id: str, approved: bool, by: str | None
    ) -> bool:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._session_factory.create_session() as session:
            doc = session.get(ParteDocumentOrm, document_id)
            if doc is None:
                return False
            doc.approved = approved
            doc.approved_by = by if approved else None
            doc.approved_at_utc = now_iso if approved else None
            session.commit()
        return True

    def delete_document(self, *, document_id: str, deleted_by: str | None) -> bool:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._session_factory.create_session() as session:
            doc = session.get(ParteDocumentOrm, document_id)
            if doc is None:
                return False
            doc.is_active = False
            doc.deleted_at_utc = now_iso
            doc.deleted_by = deleted_by
            session.commit()
        return True


def _registro_view(reg: ParteRegistroOrm) -> RegistroView:
    doc = reg.document
    return RegistroView(
        id=reg.id,
        document_id=reg.document_id,
        fecha=reg.fecha,
        obra_codigo=reg.obra_codigo,
        obra_nombre=reg.obra_nombre,
        categoria=reg.categoria,
        tipo_hora=reg.tipo_hora,
        es_incidencia=reg.es_incidencia,
        incidencia_codigo=reg.incidencia_codigo,
        incidencia_texto=reg.incidencia_texto,
        horas=reg.horas,
        hora_ide=reg.hora_ide,
        hora_codigo=reg.hora_codigo,
        hora_descripcion=reg.hora_descripcion,
        hora_ext=reg.hora_ext,
        hora_match_method=reg.hora_match_method,
        confianza_pct=reg.confianza_pct,
        parte_firmado=bool(doc.firmado) if doc is not None else False,
        parte_firmante_rol=doc.firmante_rol if doc is not None else None,
        parte_aprobado=bool(doc.approved) if doc is not None else False,
        trabajador_nombre=reg.empleado_nombre or reg.trabajador_nombre_leido,
        tiene_pdf=bool(
            doc is not None
            and doc.sharepoint_drive_id
            and doc.sharepoint_item_id
        ),
    )
