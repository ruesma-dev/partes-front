# interface_adapters/web/app.py
"""Portal de revision de partes de trabajo (sv4).

Paginas:
  GET /                       -> redirige a /trabajadores
  GET /trabajadores           -> una linea por TRABAJADOR (agregado)
  GET /trabajadores/{key}     -> detalle: una linea por REGISTRO horario
  GET /partes                 -> una linea por PARTE DIARIO
  GET /partes/{document_id}   -> detalle del parte (empleados + firma + aprobar)

APIs / acciones:
  GET   /api/sigrid/tipos-hora            -> opciones del desplegable auxhor
  PATCH /api/registros/{id}/hora          -> fija el codigo de hora (auxhor)
  PATCH /api/registros/{id}               -> edita tipo_hora / horas
  POST  /documents/{id}/approve|unapprove|delete  (form -> redirect 'back')
"""
from __future__ import annotations

import html
import logging
import re
import time
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from fastapi import Body, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator

from application.services.tipo_hora_catalog import TipoHoraCatalog
from application.services.calendar_builder import (
    build_calendar,
    build_period_options,
    normalize_mode,
    parse_period_key,
    DayObra,
)
from application.services.holiday_provider import HolidayProvider
from config.settings import Settings
from infrastructure.database.parte_repository import (
    ParteReviewRepository,
    extras_por_jornada,
)
from infrastructure.database.session_factory import SessionFactory
from infrastructure.sigrid.sigrid_lookup_client import SigridLookupClient
from infrastructure.graph.token_provider import GraphTokenProvider
from application.services.obra_catalog import ObraCatalog
from application.services.empleado_catalog import EmpleadoCatalog
from application.services import empleado_reconciler as recon

logger = logging.getLogger(__name__)

# Leyenda de incidencias (para mostrar el nombre largo del codigo).
_INCIDENCIAS = {
    "V": "Vacaciones",
    "B": "Baja enf. comun",
    "AT": "Accidente trabajo",
    "FJ": "Falta justificada",
    "F": "Falta no justif.",
    "H": "Huelga",
    "M": "Maternidad/Pat.",
}


def _fmt_horas(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "—"
    if n == int(n):
        return f"{int(n)} h"
    return f"{n:.2f}".replace(".", ",") + " h"


def _fmt_fecha(value: Any) -> str:
    """ISO 'YYYY-MM-DD' -> 'DD/MM/YYYY'."""
    if not value:
        return "—"
    s = str(value)
    parts = s.split("-")
    if len(parts) == 3:
        return f"{parts[2]}/{parts[1]}/{parts[0]}"
    return s


def _incidencia_label(codigo: Any) -> str:
    if not codigo:
        return ""
    return _INCIDENCIAS.get(str(codigo).upper(), str(codigo))


def _preview_error_html(*, title: str, message: str, external_url: str | None) -> str:
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    link = ""
    if external_url:
        safe_url = html.escape(external_url)
        link = (
            f'<p><a href="{safe_url}" target="_blank" rel="noopener">'
            "Abrir el parte en SharePoint ↗</a></p>"
        )
    return f"""<!DOCTYPE html>
<html lang="es"><head><meta charset="utf-8"><title>Vista previa no disponible</title>
<style>
 body{{font-family:Arial,Helvetica,sans-serif;background:#f4f5f6;color:#1d2024;
  margin:0;padding:24px;display:flex;align-items:center;justify-content:center;height:100vh;box-sizing:border-box}}
 .card{{max-width:560px;background:#fff;border:1px solid #dfe2e4;border-radius:12px;
  padding:24px;box-shadow:0 8px 24px rgba(29,32,36,.07)}}
 h1{{margin-top:0;font-size:18px}} p{{line-height:1.5;color:#4a4f55}}
 a{{color:#9f2842;text-decoration:none;font-weight:600}}
</style></head>
<body><div class="card"><h1>{safe_title}</h1><p>{safe_message}</p>{link}</div></body></html>"""


def _content_disposition_inline(filename: str | None) -> str:
    """Content-Disposition 'inline' SEGURO para proxies estrictos.

    El sidecar de Easy Auth (proxy .NET delante del Container App) rechaza
    cabeceras HTTP con bytes no-ASCII y responde un 500 seco AUNQUE la app
    haya devuelto 200. Los adjuntos reales traen nombres con tildes/enye/
    grado ("PARTE Nº4 JOSÉ.pdf"), asi que: filename= con version ASCII
    saneada + filename*=UTF-8'' con el nombre real percent-encoded
    (RFC 5987), que es puro ASCII y todos los navegadores modernos leen.
    """
    raw = (filename or "").strip() or "parte.pdf"
    ascii_name = (
        unicodedata.normalize("NFKD", raw)
        .encode("ascii", "ignore")
        .decode("ascii")
    )
    ascii_name = re.sub(r'[^A-Za-z0-9._ ()-]+', "_", ascii_name).strip()
    if not ascii_name or ascii_name in {".", ".."}:
        ascii_name = "parte.pdf"
    utf8_quoted = quote(raw, safe="")
    return (
        f'inline; filename="{ascii_name}"; '
        f"filename*=UTF-8''{utf8_quoted}"
    )


def _guess_pdf_media_type(name: str | None) -> str:
    n = (name or "").lower()
    if n.endswith(".pdf"):
        return "application/pdf"
    if n.endswith(".png"):
        return "image/png"
    if n.endswith(".jpg") or n.endswith(".jpeg"):
        return "image/jpeg"
    return "application/pdf"


class HoraPayload(BaseModel):
    hora_ide: int


class PartidaPayload(BaseModel):
    partida_ide: int | None = None
    partida_cod: str | None = None
    partida_res: str | None = None
    partida_capitulo: str | None = None


class FechaPayload(BaseModel):
    fecha: str  # ISO 'YYYY-MM-DD' (input date) o 'DD/MM/YYYY'


class ObraPayload(BaseModel):
    codigo: str
    ide: int | None = None
    nombre: str | None = None

    @field_validator("codigo", mode="before")
    @classmethod
    def _codigo_str(cls, v: object) -> str:
        return str(v or "").strip()


def _parse_fecha_to_iso_int(value: str) -> tuple[str, int] | None:
    """Acepta 'YYYY-MM-DD' (input date) o 'DD/MM/YYYY' -> (iso, YYYYMMDD)."""
    s = (value or "").strip()
    if not s:
        return None
    y = m = d = None
    if "-" in s and len(s.split("-")[0]) == 4:
        parts = s.split("-")
        if len(parts) == 3:
            y, m, d = parts
    elif "/" in s:
        parts = s.split("/")
        if len(parts) == 3:
            d, m, y = parts
    if y is None or m is None or d is None:
        return None
    try:
        yi, mi, di = int(y), int(m), int(d)
        from datetime import date
        date(yi, mi, di)  # valida
    except (TypeError, ValueError):
        return None
    return f"{yi:04d}-{mi:02d}-{di:02d}", yi * 10000 + mi * 100 + di


class RegistroEditPayload(BaseModel):
    tipo_hora: str | None = None
    horas: float | None = None

    @field_validator("horas", mode="before")
    @classmethod
    def _num_or_none(cls, v: object) -> float | None:
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        s = str(v).strip()
        if not s or s == "\u2014":
            return None
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        try:
            return float(s)
        except ValueError:
            return None

    @field_validator("tipo_hora", mode="before")
    @classmethod
    def _str_or_none(cls, v: object) -> str | None:
        if v is None:
            return None
        s = str(v).strip()
        return s or None


def _as_int(value: Any) -> int | None:
    """Coacciona a int tolerando str/float; None si no es convertible."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build_app(settings: Settings) -> FastAPI:
    session_factory = SessionFactory(
        database_url=settings.database_url,
        admin_database_url=settings.admin_database_url,
        target_database_name=settings.pg_db,
        auto_create_database=settings.auto_create_database,
    )
    repository = ParteReviewRepository(session_factory)
    tables_ready = repository.initialize()

    sigrid_client: SigridLookupClient | None = None
    if settings.sigrid_lookup_enabled:
        sigrid_client = SigridLookupClient(
            base_url=settings.sigrid_api_base_url,          # type: ignore[arg-type]
            function_key=settings.sigrid_api_function_key,  # type: ignore[arg-type]
            database=settings.sigrid_api_database,          # type: ignore[arg-type]
            timeout_s=settings.sigrid_api_timeout_s,
        )
        logger.info(
            "[sigrid-lookup][wiring] CABLEADO base_url=%s database=%s",
            settings.sigrid_api_base_url, settings.sigrid_api_database,
        )
    else:
        logger.info(
            "[sigrid-lookup][wiring] DESACTIVADO (faltan SIGRID_API_*); "
            "el desplegable de codigo de hora quedara vacio."
        )
    catalog = TipoHoraCatalog(client=sigrid_client)
    obra_catalog = ObraCatalog(client=sigrid_client)
    empleado_catalog = EmpleadoCatalog(client=sigrid_client)

    # Token provider de Graph para el visor de PDF (descarga desde SharePoint).
    graph_token_provider: GraphTokenProvider | None = None
    if settings.preview_enabled:
        graph_token_provider = GraphTokenProvider(
            settings.graph_key, settings.graph_timeout_s  # type: ignore[arg-type]
        )
        logger.info("[preview][wiring] Visor de PDF CABLEADO (Graph).")
    else:
        logger.info(
            "[preview][wiring] Visor de PDF DESACTIVADO (falta GRAPH_KEY)."
        )

    holiday_provider = HolidayProvider(
        enabled=settings.holidays_enabled,
        subdiv=settings.holidays_subdiv,
        extra_iso=settings.holidays_extra_list,
    )

    app = FastAPI(title=settings.app_title, version=settings.service_version)
    app.state.settings = settings
    app.state.repository = repository
    app.state.catalog = catalog
    app.state.obra_catalog = obra_catalog
    app.state.empleado_catalog = empleado_catalog
    app.state.graph_token_provider = graph_token_provider
    app.state.tables_ready = tables_ready

    templates = Jinja2Templates(
        directory=str(Path(__file__).resolve().parents[2] / "templates")
    )
    templates.env.filters["horas"] = _fmt_horas
    templates.env.filters["fecha"] = _fmt_fecha
    templates.env.filters["incidencia"] = _incidencia_label
    templates.env.globals["asset_version"] = str(int(time.time()))
    app.mount(
        "/static",
        StaticFiles(
            directory=str(Path(__file__).resolve().parents[2] / "static")
        ),
        name="static",
    )

    # ----------------------------------------------------------------- #
    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "partes-portal",
            "version": settings.service_version,
            "tables_ready": app.state.tables_ready,
            "database": settings.pg_db,
            "sigrid_lookup_enabled": settings.sigrid_lookup_enabled,
        }

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/trabajadores", status_code=302)

    # ---------------- Trabajadores ----------------------------------- #
    @app.get("/trabajadores", response_class=HTMLResponse)
    def trabajadores_list(
        request: Request,
        search: str | None = Query(default=None),
        message: str | None = Query(default=None),
    ) -> HTMLResponse:
        workers = repository.list_workers(search=search)
        context = {
            "request": request,
            "title": settings.app_title,
            "workers": workers,
            "search": search or "",
            "total_normales": round(sum(w.horas_normales for w in workers), 2),
            "total_extra": round(sum(w.horas_extra for w in workers), 2),
            "total_incidencias": sum(w.num_incidencias for w in workers),
            "sin_casar": sum(1 for w in workers if not w.matched),
            "sigrid_enabled": settings.sigrid_lookup_enabled,
            "message": message,
        }
        return templates.TemplateResponse(
            request=request, name="trabajadores_list.html", context=context
        )

    @app.get("/trabajadores/{worker_key}", response_class=HTMLResponse)
    def trabajador_detail(
        request: Request,
        worker_key: str,
        period: str | None = Query(default=None),
        modo: str | None = Query(default=None),
        message: str | None = Query(default=None),
    ) -> HTMLResponse:
        mode = normalize_mode(modo)
        detail = repository.get_worker(worker_key)
        if detail is None:
            raise HTTPException(status_code=404, detail="Trabajador no encontrado")

        # Agregado por dia y OBRA (para la tarjeta del calendario: el codigo
        # de obra encima de las horas; si hay 2 obras, 2 columnas).
        per_day: dict[str, dict] = {}
        for r in detail.registros:
            if not r.fecha:
                continue
            slot = per_day.setdefault(
                r.fecha,
                {"normal": 0.0, "extra": 0.0, "incidencias": 0, "_obras": {}},
            )
            okey = r.obra_codigo or r.obra_nombre or "—"
            oslot = slot["_obras"].setdefault(
                okey,
                {"codigo": r.obra_codigo, "nombre": r.obra_nombre,
                 "normal": 0.0, "extra": 0.0, "inc": 0},
            )
            if r.es_incidencia:
                slot["incidencias"] += 1
                oslot["inc"] += 1
            elif (r.tipo_hora or "") == "extra" or r.hora_ext == 1:
                slot["extra"] += r.horas or 0.0
                oslot["extra"] += r.horas or 0.0
            else:
                slot["normal"] += r.horas or 0.0
                oslot["normal"] += r.horas or 0.0

        for slot in per_day.values():
            obras = [
                DayObra(
                    obra_codigo=o["codigo"], obra_nombre=o["nombre"],
                    normal_h=round(o["normal"], 2), extra_h=round(o["extra"], 2),
                    incidencias=o["inc"],
                )
                for o in slot.pop("_obras").values()
            ]
            obras.sort(key=lambda x: (x.obra_codigo or "~"))
            slot["obras"] = obras

        period_options = build_period_options(
            [r.fecha for r in detail.registros], mode
        )
        selected = parse_period_key(period)
        if selected is None and period_options:
            selected = parse_period_key(period_options[0].key)

        calendar = None
        if selected is not None:
            y, m = selected
            calendar = build_calendar(
                year=y,
                month=m,
                per_day=per_day,
                holiday_name=holiday_provider.name,
                mode=mode,
            )

        # Cantidad por defecto (CanDefecto) del recurso, para diagnostico.
        # Se distingue 0 de None (un 0 significa que Sigrid no tiene la
        # jornada informada; el calculo de sv3 usa 8 en ese caso).
        candef_recurso = sorted({
            float(r.hora_candef)
            for r in detail.registros
            if r.hora_candef is not None
        })
        # Presentacion del CanDefecto: si Sigrid no lo informa o es <= el
        # minimo, se muestra la jornada por defecto (no la de Sigrid) y se
        # marca como valor "asignado".
        _cd_real = min(candef_recurso) if candef_recurso else None
        if _cd_real is None or _cd_real <= settings.candef_minimo_valido:
            candef_kpi = {
                "valor": settings.jornada_por_defecto,
                "asignado": True,
                "sigrid": _cd_real,
            }
        else:
            candef_kpi = {
                "valor": _cd_real, "asignado": False, "sigrid": _cd_real,
            }

        # Dias LABORABLES con jornada ordinaria incompleta: horas
        # ordinarias del dia por debajo del CanDefecto efectivo (el de
        # Sigrid, u 8 si era <= minimo). Se excluyen findes/festivos y
        # los dias sin horas ordinarias (0).
        candef_efectivo = candef_kpi["valor"]
        dias_incompletos: set[str] = set()
        if calendar is not None:
            for _week in calendar.weeks:
                for _day in _week:
                    if (_day.in_period and not _day.is_weekend
                            and not _day.is_holiday
                            and 0.0 < (_day.normal_h or 0.0)
                            < candef_efectivo - 1e-9):
                        dias_incompletos.add(_day.date_iso)

        context = {
            "request": request,
            "title": settings.app_title,
            "detail": detail,
            "calendar": calendar,
            "candef_recurso": candef_recurso,
            "candef_kpi": candef_kpi,
            "dias_incompletos": dias_incompletos,
            "extras": extras_por_jornada(detail.registros),
            "period_options": period_options,
            "selected_period": calendar.period_key if calendar else None,
            "period_mode": mode,
            "sigrid_enabled": settings.sigrid_lookup_enabled,
            "preview_enabled": settings.preview_enabled,
            "back": f"/trabajadores/{worker_key}",
            "worker_key": worker_key,
            "message": message,
        }
        return templates.TemplateResponse(
            request=request, name="trabajador_detail.html", context=context
        )

    # ---------------- Partes diarios --------------------------------- #
    # ----------------------------- OBRAS ----------------------------- #
    @app.get("/obras", response_class=HTMLResponse)
    def obras_list(
        request: Request,
        search: str | None = Query(default=None),
        message: str | None = Query(default=None),
    ) -> HTMLResponse:
        obras = repository.list_obras(search=search)
        context = {
            "request": request,
            "title": settings.app_title,
            "obras": obras,
            "search": search or "",
            "total_normales": round(sum(o.horas_normales for o in obras), 2),
            "total_extra": round(sum(o.horas_extra for o in obras), 2),
            "total_incidencias": sum(o.num_incidencias for o in obras),
            "num_obras": len(obras),
            "message": message,
        }
        return templates.TemplateResponse(
            request=request, name="obras_list.html", context=context
        )

    @app.get("/obras/{obra_key}", response_class=HTMLResponse)
    def obra_detail(
        request: Request,
        obra_key: str,
        period: str | None = Query(default=None),
        modo: str | None = Query(default=None),
        message: str | None = Query(default=None),
    ) -> HTMLResponse:
        mode = normalize_mode(modo)
        detail = repository.get_obra(
            obra_key, period_key=period, mode=mode,
            holiday_name=holiday_provider.name,
        )
        if detail is None:
            raise HTTPException(status_code=404, detail="Obra no encontrada")

        # Avisos de jornada incompleta por (trabajador, dia): horas
        # ordinarias EN ESTA OBRA por debajo del CanDefecto efectivo del
        # recurso (u 8 si era <= minimo). Se excluyen findes/festivos.
        _cd_min = settings.candef_minimo_valido
        _cd_jor = settings.jornada_por_defecto
        _candef_real: dict[str, float] = {}
        for _r in detail.registros:
            if _r.hora_candef is None:
                continue
            _nom = _r.trabajador_nombre or ""
            _v = float(_r.hora_candef)
            if _nom not in _candef_real or _v < _candef_real[_nom]:
                _candef_real[_nom] = _v
        incompletos: set[str] = set()
        for _row in detail.rows:
            _real = _candef_real.get(_row.nombre or "")
            _eff = _cd_jor if (_real is None or _real <= _cd_min) else _real
            for _c in _row.cells:
                if _c.is_weekend or _c.is_holiday:
                    continue
                if 0.0 < (_c.normal or 0.0) < _eff - 1e-9:
                    incompletos.add((_row.nombre or "") + "|"
                                    + (_c.date_iso or ""))

        context = {
            "request": request,
            "title": settings.app_title,
            "detail": detail,
            "period_options": detail.period_options,
            "selected_period": detail.period_key,
            "extras": extras_por_jornada(detail.registros),
            "candef_minimo": settings.candef_minimo_valido,
            "jornada_defecto": settings.jornada_por_defecto,
            "incompletos": incompletos,
            "period_mode": mode,
            "sigrid_enabled": settings.sigrid_lookup_enabled,
            "preview_enabled": settings.preview_enabled,
            "back": f"/obras/{obra_key}",
            "obra_key": obra_key,
            "message": message,
        }
        return templates.TemplateResponse(
            request=request, name="obra_detail.html", context=context
        )

    # -------------------- CONCILIACION de trabajadores ---------------- #
    @app.get("/conciliacion", response_class=HTMLResponse)
    def conciliacion(request: Request) -> HTMLResponse:
        pendientes = repository.list_unmatched_workers()
        empleados = empleado_catalog.list() if empleado_catalog.enabled else []

        filas: list[dict] = []
        n_auto = n_rev = n_sin = n_cat = 0
        for p in pendientes:
            bucket, cands = recon.classify(p["nombre_leido"], empleados, top_n=5)
            if bucket == "auto":
                n_auto += 1
            elif bucket == "revisar":
                n_rev += 1
            elif bucket == "categoria":
                n_cat += 1
            else:
                n_sin += 1
            filas.append({
                "nombre_leido": p["nombre_leido"],
                "num_registros": p["num_registros"],
                "obras": p["obras"],
                "categorias": p["categorias"],
                "partes": p.get("partes", []),
                "bucket": bucket,
                "candidates": [
                    {"ide": c.ide, "codigo": c.codigo, "nombre": c.nombre,
                     "dni": c.dni, "score": round(c.score * 100)}
                    for c in cands
                ],
            })

        context = {
            "request": request,
            "title": settings.app_title,
            "sigrid_enabled": settings.sigrid_lookup_enabled,
            "preview_enabled": settings.preview_enabled,
            "empleados_total": len(empleados),
            "filas": filas,
            "n_total": len(filas),
            "n_auto": n_auto,
            "n_revisar": n_rev,
            "n_sin": n_sin,
            "n_categoria": n_cat,
        }
        return templates.TemplateResponse(
            request=request, name="conciliacion.html", context=context
        )

    @app.post("/api/conciliacion/confirmar")
    async def conciliacion_confirmar(request: Request) -> JSONResponse:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            data = None
        if not isinstance(data, dict):
            return JSONResponse(
                {"ok": False, "error": "Body no es JSON válido."},
                status_code=400,
            )
        nombre_leido = data.get("nombre_leido")
        ide = _as_int(data.get("ide"))
        if not nombre_leido or ide is None:
            return JSONResponse(
                {"ok": False, "error": "Faltan datos: "
                 f"nombre_leido={nombre_leido!r}, ide={data.get('ide')!r}."},
                status_code=400,
            )
        emp = empleado_catalog.get_by_ide(ide)
        if emp is None:
            return JSONResponse(
                {"ok": False, "error": "Empleado no encontrado en el maestro "
                 f"(ide={ide}). ¿Sigrid configurado en sv4?"},
                status_code=404,
            )
        try:
            updated = repository.backfill_empleado(
                nombre_leido=nombre_leido, ide=emp.ide,
                codigo=emp.codigo, nombre=emp.nombre, dni=emp.dni,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[conciliacion] backfill fallo")
            return JSONResponse(
                {"ok": False, "error": f"Error guardando casado: "
                 f"{type(exc).__name__}: {exc}"},
                status_code=500,
            )
        # El alias es una optimizacion (auto-casado futuro): best-effort.
        alias_ok = True
        try:
            repository.upsert_empleado_alias(
                nombre_leido=nombre_leido, ide=emp.ide,
                codigo=emp.codigo, nombre=emp.nombre, dni=emp.dni,
                created_by="conciliacion",
            )
        except Exception as exc:  # noqa: BLE001
            alias_ok = False
            logger.warning("[conciliacion] alias no guardado: %r", exc)
        return JSONResponse({
            "ok": True, "updated": updated, "alias_ok": alias_ok,
            "empleado": {"ide": emp.ide, "codigo": emp.codigo,
                         "nombre": emp.nombre},
        })

    @app.get("/api/conciliacion/buscar")
    def conciliacion_buscar(
        q: str = Query(default=""),
        nombre_leido: str | None = Query(default=None),
    ) -> JSONResponse:
        empleados = empleado_catalog.list() if empleado_catalog.enabled else []
        query = (q or nombre_leido or "").strip()
        if not query:
            return JSONResponse({"ok": True, "items": []})
        from application.services import text_match as _tm
        scored = []
        qn = _tm.normalize(query)
        for e in empleados:
            s = _tm.name_similarity(query, e.nombre)
            # tambien por subcadena de codigo o nombre (busqueda manual libre)
            sub = qn and (qn in _tm.normalize(e.nombre) or qn in _tm.normalize(e.codigo))
            if s >= 0.30 or sub:
                scored.append((max(s, 0.31 if sub else 0.0), e))
        scored.sort(key=lambda t: t[0], reverse=True)
        items = [
            {"ide": e.ide, "codigo": e.codigo, "nombre": e.nombre,
             "dni": e.dni, "score": round(sc * 100)}
            for sc, e in scored[:15]
        ]
        return JSONResponse({"ok": True, "items": items})

    @app.post("/api/empleado/reasignar")
    async def empleado_reasignar(request: Request) -> JSONResponse:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            data = None
        if not isinstance(data, dict):
            return JSONResponse(
                {"ok": False, "error": "Body no es JSON válido."},
                status_code=400,
            )
        ide = _as_int(data.get("ide"))
        if ide is None:
            return JSONResponse(
                {"ok": False, "error": f"Falta o es inválido 'ide' "
                 f"({data.get('ide')!r})."},
                status_code=400,
            )
        emp = empleado_catalog.get_by_ide(ide)
        if emp is None:
            return JSONResponse(
                {"ok": False, "error": "Empleado no encontrado en el maestro "
                 f"(ide={ide}). ¿Sigrid configurado en sv4?"},
                status_code=404,
            )
        registro_id = _as_int(data.get("registro_id"))
        registro_ids_in = data.get("registro_ids")
        worker_key = data.get("worker_key")
        nombre_leido_in = data.get("nombre_leido")
        updated = 0
        leidos: list[str] = []
        try:
            if isinstance(registro_ids_in, list) and registro_ids_in:
                ids = [_as_int(x) for x in registro_ids_in]
                ids = [x for x in ids if x is not None]
                updated = repository.reassign_empleado_by_registro_ids(
                    registro_ids=ids, ide=emp.ide, codigo=emp.codigo,
                    nombre=emp.nombre, dni=emp.dni,
                )
                # Acotado a lineas concretas: no se crea alias de mapeo.
                leidos = []
            elif registro_id is not None:
                leido = repository.get_registro_leido(registro_id)
                if not leido:
                    return JSONResponse(
                        {"ok": False, "error": "Registro sin nombre leido"},
                        status_code=400,
                    )
                updated = repository.reassign_empleado_by_leido(
                    nombre_leido=leido, ide=emp.ide, codigo=emp.codigo,
                    nombre=emp.nombre, dni=emp.dni,
                )
                leidos = [leido]
            elif worker_key:
                updated, leidos = repository.reassign_empleado_by_worker_key(
                    worker_key=worker_key, ide=emp.ide,
                    codigo=emp.codigo, nombre=emp.nombre, dni=emp.dni,
                )
            elif nombre_leido_in:
                updated = repository.reassign_empleado_by_leido(
                    nombre_leido=nombre_leido_in, ide=emp.ide,
                    codigo=emp.codigo, nombre=emp.nombre, dni=emp.dni,
                )
                leidos = [nombre_leido_in]
            else:
                return JSONResponse(
                    {"ok": False,
                     "error": "Falta registro_id / worker_key / nombre_leido"},
                    status_code=400,
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[reasignar] fallo guardando reasignacion")
            return JSONResponse(
                {"ok": False, "error": f"Error guardando reasignacion: "
                 f"{type(exc).__name__}: {exc}"},
                status_code=500,
            )

        # Alias (auto-casado futuro): best-effort, no debe tumbar la reasignacion.
        alias_ok = True
        for leido in leidos:
            try:
                repository.upsert_empleado_alias(
                    nombre_leido=leido, ide=emp.ide, codigo=emp.codigo,
                    nombre=emp.nombre, dni=emp.dni, created_by="reasignacion",
                )
            except Exception as exc:  # noqa: BLE001
                alias_ok = False
                logger.warning("[reasignar] alias no guardado: %r", exc)
        return JSONResponse({
            "ok": True, "updated": updated, "alias_ok": alias_ok,
            "empleado": {"ide": emp.ide, "codigo": emp.codigo,
                         "nombre": emp.nombre},
        })

    # ----------------------------- DESHACER --------------------------- #
    @app.get("/api/undo/list", include_in_schema=False)
    def undo_list() -> JSONResponse:
        try:
            items = repository.list_undo(limit=15)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[undo] list fallo: %r", exc)
            return JSONResponse({"ok": False, "items": [], "count": 0})
        return JSONResponse({"ok": True, "items": items, "count": len(items)})

    @app.post("/api/undo", include_in_schema=False)
    def undo_apply() -> JSONResponse:
        try:
            res = repository.undo_last()
        except Exception as exc:  # noqa: BLE001
            logger.exception("[undo] fallo al deshacer")
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status_code=500,
            )
        return JSONResponse(res, status_code=200 if res.get("ok") else 400)

    @app.get("/partes", response_class=HTMLResponse)
    def partes_list(
        request: Request,
        search: str | None = Query(default=None),
        pendientes: str = Query(default="0"),
        message: str | None = Query(default=None),
    ) -> HTMLResponse:
        only_pending = pendientes in {"1", "true", "on"}
        partes = repository.list_partes(
            search=search, only_pending=only_pending
        )
        context = {
            "request": request,
            "title": settings.app_title,
            "partes": partes,
            "search": search or "",
            "only_pending": only_pending,
            "total_pendientes": sum(1 for p in partes if not p.approved),
            "total_sin_firmar": sum(1 for p in partes if not p.firmado),
            "message": message,
        }
        return templates.TemplateResponse(
            request=request, name="partes_list.html", context=context
        )

    @app.get("/partes/{document_id}", response_class=HTMLResponse)
    def parte_detail(
        request: Request,
        document_id: str,
        message: str | None = Query(default=None),
    ) -> HTMLResponse:
        detail = repository.get_parte(document_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="Parte no encontrado")
        context = {
            "request": request,
            "title": settings.app_title,
            "parte": detail,
            "sigrid_enabled": settings.sigrid_lookup_enabled,
            "preview_enabled": settings.preview_enabled and bool(detail.sharepoint_url),
            "preview_url": f"/partes/{document_id}/preview",
            "back": f"/partes/{document_id}",
            "message": message,
        }
        return templates.TemplateResponse(
            request=request, name="parte_detail.html", context=context
        )

    # ---------------- Lookup Sigrid (auxhor) ------------------------- #
    @app.get("/api/sigrid/tipos-hora", include_in_schema=False)
    def sigrid_tipos_hora() -> JSONResponse:
        if not catalog.enabled:
            return JSONResponse(
                {"ok": False, "error": "Sigrid no configurado en el sv4.",
                 "items": []}
            )
        try:
            items = catalog.list()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sigrid-lookup] tipos_hora fallo: %r", exc)
            return JSONResponse(
                {"ok": False, "error": f"Error consultando Sigrid: {exc}",
                 "items": []}
            )
        return JSONResponse(
            {
                "ok": True,
                "items": [
                    {
                        "ide": t.ide,
                        "codigo": t.codigo,
                        "descripcion": t.descripcion,
                        "ext": t.ext,
                    }
                    for t in items
                ],
            }
        )

    # ---------------- Lookup Sigrid (obras) -------------------------- #
    @app.get("/api/sigrid/obras", include_in_schema=False)
    def sigrid_obras() -> JSONResponse:
        if not obra_catalog.enabled:
            return JSONResponse(
                {"ok": False, "error": "Sigrid no configurado en el sv4.",
                 "items": []}
            )
        try:
            items = obra_catalog.list()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sigrid-lookup] obras fallo: %r", exc)
            return JSONResponse(
                {"ok": False, "error": f"Error consultando Sigrid: {exc}",
                 "items": []}
            )
        return JSONResponse(
            {
                "ok": True,
                "items": [
                    {"ide": o.ide, "codigo": o.codigo, "nombre": o.nombre}
                    for o in items
                ],
            }
        )

    @app.get("/api/sigrid/empleados", include_in_schema=False)
    def sigrid_empleados() -> JSONResponse:
        if not empleado_catalog.enabled:
            return JSONResponse(
                {"ok": False, "error": "Sigrid no configurado en el sv4.",
                 "items": []}
            )
        try:
            items = empleado_catalog.list()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sigrid-lookup] empleados fallo: %r", exc)
            return JSONResponse(
                {"ok": False, "error": f"Error consultando Sigrid: {exc}",
                 "items": []}
            )
        def _sugerida(cd: float | None) -> float:
            # CanDefecto no valido (vacio o <= minimo) -> jornada por defecto
            # (mismo umbral que sv3 al reclasificar extras).
            if cd is None or float(cd) <= settings.candef_minimo_valido:
                return settings.jornada_por_defecto
            return float(cd)

        return JSONResponse({
            "ok": True,
            "items": [
                {"ide": e.ide, "codigo": e.codigo, "nombre": e.nombre,
                 "dni": e.dni, "categoria": e.categoria, "candef": e.candef,
                 "jornada_sugerida": _sugerida(e.candef)}
                for e in items
            ],
        })

    @app.get("/api/sigrid/partidas", include_in_schema=False)
    def sigrid_partidas(obra_ide: int = Query(...)) -> JSONResponse:
        """Partidas HOJA (sin hijos) del presupuesto de la obra, para imputar.
        Devuelve CD/CI/CP/OTRO sin restriccion (solo se exige que sean hoja)."""
        if sigrid_client is None:
            return JSONResponse(
                {"ok": False, "error": "Sigrid no configurado en el sv4.",
                 "items": []}
            )
        from application.services.partida_catalog import (
            build_arbol_partidas,
            partidas_hoja,
        )
        try:
            filas = sigrid_client.fetch_partidas_obra(obra_ide)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[sigrid-lookup] partidas fallo: %r", exc)
            return JSONResponse(
                {"ok": False, "error": f"Error consultando Sigrid: {exc}",
                 "items": []}
            )
        nodos = build_arbol_partidas(filas)
        items = [
            {"ide": n.ide, "cod": n.cod, "res": n.res, "capitulo": n.categoria}
            for n in partidas_hoja(nodos)
        ]
        return JSONResponse({"ok": True, "items": items})

    # ---------------- Visor del PDF del parte ------------------------ #
    @app.get("/partes/{document_id}/preview", response_class=Response)
    def parte_preview(document_id: str) -> Response:
        try:
            ref = repository.get_sharepoint_ref(document_id)
        except Exception as exc:  # noqa: BLE001
            # Un fallo al LEER la referencia en la BD (p.ej. desajuste de
            # esquema) devolvia un HTTP 500 mudo. Se captura para mostrar el
            # detalle y dejar rastro en el log en lugar de romper el visor.
            logger.exception(
                "[preview] fallo leyendo la referencia SharePoint "
                "document_id=%s", document_id,
            )
            return HTMLResponse(
                _preview_error_html(
                    title="Error accediendo al parte",
                    message=(
                        "No se pudo leer la referencia del parte en la base "
                        f"de datos: {type(exc).__name__}: {exc}"
                    ),
                    external_url=None,
                ),
                status_code=500,
            )
        if ref is None:
            raise HTTPException(status_code=404, detail="Parte no encontrado")

        external = ref.get("url")
        if not settings.preview_enabled:
            return HTMLResponse(_preview_error_html(
                title="Visor no configurado",
                message="Falta GRAPH_KEY en el .env del portal (sv4).",
                external_url=external,
            ))
        item_id = (ref.get("item_id") or "").strip()
        drive_id = (ref.get("drive_id") or "").strip() or (
            settings.sharepoint_drive_id or ""
        ).strip()
        if not item_id or not drive_id:
            return HTMLResponse(_preview_error_html(
                title="Parte sin archivo en SharePoint",
                message=(
                    "Este parte no tiene referencia de SharePoint guardada "
                    "(se proceso sin archivar o antes de activar SharePoint). "
                    "Reprocesa el parte para poder visualizarlo."
                ),
                external_url=external,
            ))

        token_provider: GraphTokenProvider | None = app.state.graph_token_provider
        if token_provider is None:
            return HTMLResponse(_preview_error_html(
                title="Graph no disponible",
                message="No se pudo inicializar el acceso a Microsoft Graph.",
                external_url=external,
            ))

        content_url = (
            f"https://graph.microsoft.com/v1.0/drives/{drive_id}"
            f"/items/{item_id}/content"
        )
        try:
            headers = {"Authorization": f"Bearer {token_provider.get_token()}"}
            timeout = httpx.Timeout(
                settings.graph_timeout_s, connect=min(20, settings.graph_timeout_s)
            )
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                resp = client.get(content_url, headers=headers)
            if resp.status_code >= 300:
                logger.warning(
                    "[preview] Graph content %s para item=%s: %s",
                    resp.status_code, item_id, resp.text[:300],
                )
                return HTMLResponse(_preview_error_html(
                    title="No se pudo descargar el archivo",
                    message=f"Graph devolvio {resp.status_code} al descargar el PDF.",
                    external_url=external,
                ))
            media = _guess_pdf_media_type(ref.get("filename"))
            fname = ref.get("filename") or "parte.pdf"
            return Response(
                content=resp.content,
                media_type=media,
                headers={
                    "Content-Disposition": _content_disposition_inline(fname),
                    "Cache-Control": "no-store",
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("[preview] error document_id=%s", document_id)
            return HTMLResponse(_preview_error_html(
                title="Error obteniendo la vista previa",
                message=str(exc),
                external_url=external,
            ))

    # ---------------- Edicion de cabecera del parte ------------------ #
    @app.patch("/api/partes/{document_id}/fecha")
    def patch_parte_fecha(
        document_id: str,
        payload: FechaPayload = Body(...),
    ) -> dict[str, Any]:
        parsed = _parse_fecha_to_iso_int(payload.fecha)
        if parsed is None:
            raise HTTPException(status_code=400, detail="Fecha invalida.")
        fecha_iso, fecha_int = parsed
        ok = repository.update_parte_fecha(
            document_id=document_id, fecha_iso=fecha_iso, fecha_int=fecha_int
        )
        if not ok:
            raise HTTPException(status_code=404, detail="Parte no encontrado")
        return {"ok": True, "fecha": fecha_iso, "fecha_int": fecha_int}

    @app.patch("/api/partes/{document_id}/obra")
    def patch_parte_obra(
        document_id: str,
        payload: ObraPayload = Body(...),
    ) -> dict[str, Any]:
        if not payload.codigo:
            raise HTTPException(status_code=400, detail="Falta el codigo de obra.")
        # Resuelve ide/nombre desde el catalogo (fuente de verdad); si no esta
        # cableado o no se encuentra, usa lo que envie el front.
        ide = payload.ide
        nombre = payload.nombre
        if obra_catalog.enabled:
            opt = obra_catalog.get_by_codigo(payload.codigo)
            if opt is not None:
                ide = opt.ide
                nombre = opt.nombre
        ok = repository.update_parte_obra(
            document_id=document_id,
            obra_ide=ide,
            obra_codigo=payload.codigo,
            obra_nombre=nombre,
        )
        if not ok:
            raise HTTPException(status_code=404, detail="Parte no encontrado")
        return {
            "ok": True,
            "obra_ide": ide,
            "obra_codigo": payload.codigo,
            "obra_nombre": nombre,
        }

    # ---------------- Edicion de registros --------------------------- #
    @app.patch("/api/registros/{registro_id}/hora")
    def set_registro_hora(
        registro_id: int,
        payload: HoraPayload = Body(...),
    ) -> dict[str, Any]:
        if not catalog.enabled:
            raise HTTPException(
                status_code=503,
                detail="Sigrid no configurado: no se puede resolver el "
                       "codigo de hora.",
            )
        option = catalog.get_by_ide(payload.hora_ide)
        if option is None:
            raise HTTPException(
                status_code=400,
                detail="El tipo de hora seleccionado no existe en Sigrid.",
            )
        ok = repository.set_registro_hora(
            registro_id=registro_id,
            hora_ide=option.ide,
            hora_codigo=option.codigo,
            hora_descripcion=option.descripcion,
            hora_ext=option.ext,
            hora_precio_coste=option.pre,
            hora_precio_nomina=option.prenom,
        )
        if not ok:
            raise HTTPException(status_code=404, detail="Registro no encontrado")
        return {
            "ok": True,
            "registro_id": registro_id,
            "hora_ide": option.ide,
            "hora_codigo": option.codigo,
            "hora_descripcion": option.descripcion,
            "hora_ext": option.ext,
            "tipo_hora": "extra" if option.ext == 1 else "normal",
        }

    @app.patch("/api/registros/{registro_id}/partida")
    def api_set_registro_partida(
        registro_id: int,
        payload: PartidaPayload = Body(...),
    ) -> dict[str, Any]:
        """Reimputa manualmente la partida de un registro. El front envia los
        4 campos (ide/cod/res/capitulo) de la partida-hoja elegida en el combo
        cargado con /api/sigrid/partidas (partidas del presupuesto de la obra).
        """
        ok = repository.set_registro_partida(
            registro_id=registro_id,
            partida_ide=payload.partida_ide,
            partida_cod=payload.partida_cod,
            partida_res=payload.partida_res,
            partida_capitulo=payload.partida_capitulo,
        )
        if not ok:
            raise HTTPException(status_code=404, detail="Registro no encontrado")
        return {
            "ok": True,
            "registro_id": registro_id,
            "partida_ide": payload.partida_ide,
            "partida_cod": payload.partida_cod,
            "partida_res": payload.partida_res,
            "partida_capitulo": payload.partida_capitulo,
        }

    @app.patch("/api/registros/{registro_id}")
    def update_registro(
        registro_id: int,
        payload: RegistroEditPayload = Body(...),
    ) -> dict[str, Any]:
        ok = repository.update_registro(
            registro_id=registro_id,
            tipo_hora=payload.tipo_hora,
            horas=payload.horas,
        )
        if not ok:
            raise HTTPException(status_code=404, detail="Registro no encontrado")
        return {"ok": True, "registro_id": registro_id}

    # ---------------- Estado del parte (documento) ------------------- #
    @app.post("/documents/{document_id}/approve", include_in_schema=False)
    def approve_document(
        document_id: str,
        back: str = Form(default="/partes"),
    ) -> RedirectResponse:
        repository.approve_document(
            document_id=document_id,
            approved_by=settings.default_reviewer,
        )
        return _redirect(back, "Parte aprobado")

    @app.post("/documents/{document_id}/unapprove", include_in_schema=False)
    def unapprove_document(
        document_id: str,
        back: str = Form(default="/partes"),
    ) -> RedirectResponse:
        repository.unapprove_document(document_id=document_id)
        return _redirect(back, "Parte marcado como pendiente")

    @app.post("/documents/{document_id}/delete", include_in_schema=False)
    def delete_document(
        document_id: str,
        back: str = Form(default="/partes"),
    ) -> RedirectResponse:
        repository.delete_document(
            document_id=document_id,
            deleted_by=settings.default_reviewer,
        )
        # Si borramos desde el detalle del propio parte, volver al listado.
        target = "/partes" if back.startswith(f"/partes/{document_id}") else back
        return _redirect(target, "Parte movido a la papelera")

    # ----------------------------------------------------------- #
    # BORRADO: linea / obra / persona  (soft -> papelera -> hard).
    # ----------------------------------------------------------- #
    @app.post("/api/registro/{registro_id}/delete", include_in_schema=False)
    def api_registro_delete(registro_id: int) -> JSONResponse:
        ok = repository.soft_delete_registro(
            registro_id=registro_id, by=settings.default_reviewer
        )
        return JSONResponse({"ok": ok})

    @app.post("/api/registro/{registro_id}/restore", include_in_schema=False)
    def api_registro_restore(registro_id: int) -> JSONResponse:
        return JSONResponse(
            {"ok": repository.restore_registro(registro_id=registro_id)}
        )

    @app.post("/api/registro/{registro_id}/hard-delete", include_in_schema=False)
    def api_registro_hard(registro_id: int) -> JSONResponse:
        return JSONResponse(
            {"ok": repository.hard_delete_registro(registro_id=registro_id)}
        )

    @app.post("/api/obra/{obra_key}/delete", include_in_schema=False)
    def api_obra_delete(obra_key: str) -> JSONResponse:
        n = repository.soft_delete_obra(
            obra_key=obra_key, by=settings.default_reviewer
        )
        return JSONResponse({"ok": n > 0, "partes": n})

    @app.post("/api/trabajador/{worker_key}/delete", include_in_schema=False)
    def api_trabajador_delete(worker_key: str) -> JSONResponse:
        n = repository.soft_delete_worker(
            worker_key=worker_key, by=settings.default_reviewer
        )
        return JSONResponse({"ok": n > 0, "lineas": n})

    @app.post("/api/documento/{document_id}/restore", include_in_schema=False)
    def api_documento_restore(document_id: str) -> JSONResponse:
        return JSONResponse(
            {"ok": repository.restore_document(document_id=document_id)}
        )

    @app.post("/api/documento/{document_id}/hard-delete", include_in_schema=False)
    def api_documento_hard(document_id: str) -> JSONResponse:
        return JSONResponse(
            {"ok": repository.hard_delete_document(document_id=document_id)}
        )

    @app.post("/api/papelera/vaciar", include_in_schema=False)
    def api_papelera_vaciar() -> JSONResponse:
        res = repository.vaciar_papelera()
        return JSONResponse({"ok": True, **res})

    @app.get("/papelera", response_class=HTMLResponse)
    def papelera(
        request: Request, message: str | None = Query(default=None)
    ) -> HTMLResponse:
        pap = repository.list_papelera()
        context = {
            "request": request,
            "title": settings.app_title,
            "documentos": pap["documentos"],
            "registros": pap["registros"],
            "message": message,
        }
        return templates.TemplateResponse(
            request=request, name="papelera.html", context=context
        )

    # ----------------------------------------------------------- #
    # CREAR parte manualmente (un dia o un periodo, con calendario).
    # ----------------------------------------------------------- #
    @app.get("/nuevo", response_class=HTMLResponse)
    def nuevo_parte(request: Request) -> HTMLResponse:
        context = {
            "request": request,
            "title": settings.app_title,
            "sigrid_enabled": getattr(obra_catalog, "enabled", False),
            "hoy": date.today().isoformat(),
        }
        return templates.TemplateResponse(
            request=request, name="nuevo_parte.html", context=context
        )

    @app.post("/api/partes/nuevo", include_in_schema=False)
    async def api_partes_nuevo(request: Request) -> JSONResponse:
        try:
            data = await request.json()
        except Exception:  # noqa: BLE001
            data = None
        if not isinstance(data, dict):
            return JSONResponse(
                {"ok": False, "error": "Body no es JSON válido."}, status_code=400
            )
        dias = data.get("dias") or []
        if not isinstance(dias, list) or not dias:
            return JSONResponse(
                {"ok": False, "error": "Selecciona al menos un día."},
                status_code=400,
            )
        nombre = (data.get("empleado_nombre") or "").strip()
        if not nombre:
            return JSONResponse(
                {"ok": False, "error": "Falta el trabajador."}, status_code=400
            )
        if data.get("obra_ide") is None and not (data.get("obra_codigo") or "").strip():
            return JSONResponse(
                {"ok": False, "error": "Falta la obra."}, status_code=400
            )

        def _f(v: Any) -> float:
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0

        # Casar el CODIGO DE HORA por categoria+tipo (igual que sv3 en la
        # ingesta), para que las lineas manuales no salgan "sin asignar".
        hora_normal = None
        hora_extra = None
        categoria_txt = (data.get("categoria") or "").strip()
        if categoria_txt and sigrid_client is not None:
            try:
                from application.services.tipo_hora_matcher import (
                    TipoHoraMatcher,
                )
                matcher = TipoHoraMatcher(sigrid_client.fetch_tipos_hora())
                hora_normal = matcher.match(categoria_txt, extra=False)
                hora_extra = matcher.match(categoria_txt, extra=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[partes-nuevo] match codigo hora fallo: %r", exc)

        # Casar la PARTIDA en el momento de crear (mismo motor que sv3):
        # primero por NOMBRE del trabajador en la descripcion, luego por
        # CATEGORIA, y SOLO en el capitulo CI. Antes esto quedaba pendiente
        # de la siguiente conciliacion de sv3 (al persistir un parte por
        # email), de ahi que registros iguales salieran unos casados y otros
        # en blanco. Si el usuario eligio partida a mano, se respeta.
        partida_ide = _as_int(data.get("partida_ide"))
        partida_cod = data.get("partida_cod") or None
        partida_res_txt = data.get("partida_res") or None
        partida_capitulo = data.get("partida_capitulo") or None
        partida_metodo = "manual" if partida_ide else None
        partida_score = 1.0 if partida_ide else None
        obra_ide_int = _as_int(data.get("obra_ide"))
        if partida_ide is None and sigrid_client is not None and obra_ide_int:
            try:
                from application.services.partida_catalog import (
                    build_arbol_partidas,
                    partidas_hoja,
                )
                from application.services.partida_matcher import match_partida
                filas = sigrid_client.fetch_partidas_obra(obra_ide_int)
                nodos = build_arbol_partidas(filas)
                candidatas = partidas_hoja(nodos, categoria="CI")
                m = match_partida(categoria_txt or None, nombre, candidatas)
                if m is not None:
                    partida_ide = m.partida.ide
                    partida_cod = m.partida.cod
                    partida_res_txt = m.partida.res
                    partida_capitulo = m.partida.categoria
                    partida_metodo = m.metodo
                    partida_score = m.score
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[partes-nuevo] casado de partida fallo: %r", exc
                )

        res = repository.crear_parte_manual(
            obra_ide=_as_int(data.get("obra_ide")),
            obra_codigo=(data.get("obra_codigo") or None),
            obra_nombre=(data.get("obra_nombre") or None),
            empleado_ide=_as_int(data.get("empleado_ide")),
            empleado_codigo=(data.get("empleado_codigo") or None),
            empleado_nombre=nombre,
            empleado_dni=(data.get("empleado_dni") or None),
            empleado_reside=_as_int(data.get("empleado_reside")),
            categoria=(data.get("categoria") or None),
            dias=[str(d) for d in dias],
            horas_ordinaria=_f(data.get("horas_ordinaria")),
            horas_extra=_f(data.get("horas_extra")),
            partida_ide=partida_ide,
            partida_cod=partida_cod,
            partida_res=partida_res_txt,
            partida_capitulo=partida_capitulo,
            partida_match_method=partida_metodo,
            partida_match_score=partida_score,
            hora_normal=hora_normal,
            hora_extra=hora_extra,
            by=settings.default_reviewer,
        )
        ok = not res.get("error") and (res.get("lineas") or 0) > 0
        return JSONResponse(
            {"ok": ok, **res}, status_code=200 if ok else 400
        )

    return app


def _redirect(path: str, message: str) -> RedirectResponse:
    sep = "&" if "?" in path else "?"
    qs = urlencode({"message": message})
    return RedirectResponse(url=f"{path}{sep}{qs}", status_code=303)
