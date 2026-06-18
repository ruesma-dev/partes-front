# infrastructure/sigrid/sigrid_lookup_client.py
"""Cliente HTTP de SOLO LECTURA contra ``sigrid-api`` para el portal.

Unico lookup necesario en sv4: la lista de tipos de hora (``auxhor``)
para el desplegable de codigo de hora del detalle del trabajador. El
casado inicial lo hace el sv3; aqui solo poblamos el selector y, al
guardar, resolvemos el ``auxhor`` elegido por ``ide`` para no confiar
en el cliente con los precios.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_LOG_PREFIX = "[sigrid-lookup]"

_SQL_TIPOS_HORA = """\
SELECT
    auxhor.ide    AS ide,
    auxhor.cod    AS codigo,
    auxhor.res    AS descripcion,
    auxhor.ext    AS ext,
    auxhor.pre    AS pre,
    auxhor.prenom AS prenom
FROM auxhor
WHERE (auxhor.fecbaj IS NULL OR auxhor.fecbaj = 0)
ORDER BY auxhor.ext, auxhor.cod
"""

# Obras: obr extiende con (obr.ide = con.ide). Codigo en con.cod, nombre en
# obr.res. Para el desplegable de obra del detalle del parte.
_SQL_OBRAS = """\
SELECT
    con.ide AS ide,
    con.cod AS codigo,
    obr.res AS nombre
FROM obr
JOIN con ON obr.ide = con.ide
WHERE con.cod IS NOT NULL
ORDER BY con.cod
"""


# Empleados: emp extiende con (emp.ide = con.ide). Codigo en con.cod,
# nombre completo en emp.res, DNI en emp.dni. Para la conciliacion de
# trabajadores sin casar contra el maestro de Sigrid.
_SQL_EMPLEADOS = """\
SELECT
    con.ide AS ide,
    con.cod AS codigo,
    emp.res AS nombre,
    emp.dni AS dni
FROM emp
JOIN con ON emp.ide = con.ide
"""


@dataclass(frozen=True)
class TipoHoraOption:
    ide: int
    codigo: str | None
    descripcion: str | None
    ext: int
    pre: float | None
    prenom: float | None


@dataclass(frozen=True)
class ObraOption:
    ide: int | None
    codigo: str | None
    nombre: str | None


@dataclass(frozen=True)
class EmpleadoOption:
    ide: int
    codigo: str | None
    nombre: str | None
    dni: str | None


class SigridLookupClient:
    def __init__(
        self,
        *,
        base_url: str,
        function_key: str,
        database: str,
        timeout_s: float = 30.0,
        max_rows: int = 10000,
    ) -> None:
        if not base_url:
            raise ValueError("SigridLookupClient requiere base_url no vacio")
        if not function_key:
            raise ValueError("SigridLookupClient requiere function_key no vacio")
        if not database:
            raise ValueError("SigridLookupClient requiere database no vacio")
        self._base_url = base_url.rstrip("/")
        self._function_key = function_key
        self._database = database
        self._timeout_s = float(timeout_s)
        self._max_rows = int(max_rows)
        logger.info(
            "%s Instanciado. base_url=%s database=%s",
            _LOG_PREFIX, self._base_url, self._database,
        )

    def fetch_tipos_hora(self) -> list[TipoHoraOption]:
        columns, rows = self._post_sql_read(
            sql=_SQL_TIPOS_HORA, parameters=[], label="tipos_hora"
        )
        out: list[TipoHoraOption] = []
        for row in rows:
            rm = dict(zip(columns, row))
            ide = _opt_int(rm.get("ide"))
            if ide is None:
                continue
            out.append(
                TipoHoraOption(
                    ide=ide,
                    codigo=_opt_str(rm.get("codigo")),
                    descripcion=_opt_str(rm.get("descripcion")),
                    ext=_opt_int(rm.get("ext")) or 0,
                    pre=_opt_float(rm.get("pre")),
                    prenom=_opt_float(rm.get("prenom")),
                )
            )
        logger.info("%s tipos_hora -> %s filas", _LOG_PREFIX, len(out))
        return out

    def fetch_obras(self) -> list[ObraOption]:
        columns, rows = self._post_sql_read(
            sql=_SQL_OBRAS, parameters=[], label="obras"
        )
        seen: set[str] = set()
        out: list[ObraOption] = []
        for row in rows:
            rm = dict(zip(columns, row))
            cod = _opt_str(rm.get("codigo"))
            if not cod or cod in seen:
                continue
            seen.add(cod)
            out.append(
                ObraOption(
                    ide=_opt_int(rm.get("ide")),
                    codigo=cod,
                    nombre=_opt_str(rm.get("nombre")),
                )
            )
        logger.info("%s obras -> %s obras", _LOG_PREFIX, len(out))
        return out

    def fetch_empleados(self) -> list[EmpleadoOption]:
        columns, rows = self._post_sql_read(
            sql=_SQL_EMPLEADOS, parameters=[], label="empleados"
        )
        out: list[EmpleadoOption] = []
        for row in rows:
            rm = dict(zip(columns, row))
            ide = _opt_int(rm.get("ide"))
            if ide is None:
                continue
            out.append(
                EmpleadoOption(
                    ide=ide,
                    codigo=_opt_str(rm.get("codigo")),
                    nombre=_opt_str(rm.get("nombre")),
                    dni=_opt_str(rm.get("dni")),
                )
            )
        logger.info("%s empleados -> %s filas", _LOG_PREFIX, len(out))
        return out

    def _post_sql_read(
        self, *, sql: str, parameters: list[Any], label: str
    ) -> tuple[list[str], list[list[Any]]]:
        url = f"{self._base_url}/api/sql/read"
        payload = {
            "database": self._database,
            "sql": sql,
            "parameters": parameters,
            "timeout_seconds": int(self._timeout_s),
            "max_rows": self._max_rows,
        }
        headers = {
            "x-functions-key": self._function_key,
            "Content-Type": "application/json",
        }
        transport = httpx.HTTPTransport(retries=1)
        with httpx.Client(timeout=self._timeout_s, transport=transport) as client:
            response = client.post(url, json=payload, headers=headers)
        status = response.status_code
        body_text = response.text or ""
        if status >= 400:
            raise RuntimeError(f"sigrid-api respondio {status}: {body_text[:300]}")
        try:
            body: dict[str, Any] = response.json()
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"sigrid-api respuesta no JSON: {body_text[:300]}"
            ) from exc
        if not body.get("ok", False):
            raise RuntimeError(f"sigrid-api devolvio ok=false: {body!r}")
        columns: list[str] = list(body.get("columns") or [])
        rows: list[list[Any]] = list(body.get("rows") or [])
        return columns, rows


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        s = value.strip()
        return s or None
    return str(value)


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
