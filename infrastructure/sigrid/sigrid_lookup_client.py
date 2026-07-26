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
from datetime import datetime
import logging
from dataclasses import dataclass, replace as dc_replace
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
    con.ide       AS ide,
    con.cod       AS codigo,
    emp.res       AS nombre,
    emp.dni       AS dni,
    res.ide       AS reside,
    auxrestip.res AS categoria,
    reshor.candef AS candef
FROM emp
JOIN con ON emp.ide = con.ide
LEFT JOIN res ON res.conide = emp.ide
LEFT JOIN con rescon ON rescon.ide = res.ide
LEFT JOIN auxrestip ON auxrestip.ide = res.restipide
LEFT JOIN reshor ON reshor.reside = res.ide AND reshor.horide = res.horide
WHERE (con.fecbaj IS NULL OR con.fecbaj = 0 OR con.fecbaj > ?)
  AND (res.ide IS NULL
       OR rescon.fecbaj IS NULL OR rescon.fecbaj = 0 OR rescon.fecbaj > ?)
"""


_SQL_PARTIDAS = """\
SELECT
    obrparpar.ide       AS ide,
    obrparpar.padide    AS padide,
    obrparpar.cod       AS cod,
    obrparpar.res       AS res,
    obrparpar.tex       AS tex,
    obrparpar.tipdes    AS tipdes,
    obrparpar.cosindide AS cosindide,
    obrparpar.unimed    AS unimed
FROM obrparpar
WHERE obrparpar.obride = ?
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
    categoria: str | None = None
    candef: float | None = None
    reside: int | None = None   # recurso (res.ide) del empleado


@dataclass
class PartidaRowLite:
    """Fila cruda de ``obrparpar`` para construir el arbol de partidas."""
    ide: int
    padide: int | None
    cod: str | None
    res: str | None
    tex: str | None
    tipdes: int
    cosindide: int | None
    unimed: str | None


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
        """Empleados ACTIVOS: excluye los dados de baja en Sigrid.

        'Dar de baja concepto' escribe ``fecbaj`` (entero YYYYMMDD) en el
        concepto del EMPLEADO o del RECURSO asociado; 0/NULL = activo. Se
        excluye si CUALQUIERA de los dos tiene baja efectiva a dia de hoy
        (una baja con fecha futura sigue apareciendo hasta ese dia).
        """
        hoy = int(datetime.now().strftime("%Y%m%d"))
        columns, rows = self._post_sql_read(
            sql=_SQL_EMPLEADOS, parameters=[hoy, hoy], label="empleados"
        )
        out: list[EmpleadoOption] = []
        por_ide: dict[int, EmpleadoOption] = {}
        for row in rows:
            rm = dict(zip(columns, row))
            ide = _opt_int(rm.get("ide"))
            if ide is None:
                continue
            categoria = _opt_str(rm.get("categoria"))
            candef = _opt_float(rm.get("candef"))
            reside = _opt_int(rm.get("reside"))
            previo = por_ide.get(ide)
            if previo is not None:
                # El JOIN con res/reshor puede duplicar filas (varios
                # recursos por empleado): completamos categoria/candef si
                # la fila nueva los aporta (dataclass frozen -> replace).
                cambios = {}
                if previo.categoria is None and categoria:
                    cambios["categoria"] = categoria
                if previo.candef is None and candef is not None:
                    cambios["candef"] = candef
                if previo.reside is None and reside is not None:
                    cambios["reside"] = reside
                if cambios:
                    nuevo = dc_replace(previo, **cambios)
                    por_ide[ide] = nuevo
                    out[out.index(previo)] = nuevo
                continue
            emp = EmpleadoOption(
                ide=ide,
                codigo=_opt_str(rm.get("codigo")),
                nombre=_opt_str(rm.get("nombre")),
                dni=_opt_str(rm.get("dni")),
                categoria=categoria,
                candef=candef,
                reside=reside,
            )
            por_ide[ide] = emp
            out.append(emp)
        logger.info("%s empleados -> %s filas", _LOG_PREFIX, len(out))
        return out

    def fetch_partidas_obra(self, obra_ide: int) -> list[PartidaRowLite]:
        columns, rows = self._post_sql_read(
            sql=_SQL_PARTIDAS, parameters=[int(obra_ide)], label="partidas",
        )
        out: list[PartidaRowLite] = []
        for row in rows:
            rm = dict(zip(columns, row))
            ide = _opt_int(rm.get("ide"))
            if ide is None:
                continue
            out.append(PartidaRowLite(
                ide=ide,
                padide=_opt_int(rm.get("padide")),
                cod=_opt_str(rm.get("cod")),
                res=_opt_str(rm.get("res")),
                tex=_opt_str(rm.get("tex")),
                tipdes=_opt_int(rm.get("tipdes")) or 0,
                cosindide=_opt_int(rm.get("cosindide")),
                unimed=_opt_str(rm.get("unimed")),
            ))
        logger.info(
            "%s partidas obra=%s -> %s filas", _LOG_PREFIX, obra_ide, len(out)
        )
        return out

    def fetch_dnis_sin_extra(self, dnis: set[str]) -> set[str]:
        """DNIs cuyos recursos NO tienen NINGUNA hora EXTRA en ``reshor``.

        Ancla por DNI (fiable), no por recurso_ide (que puede venir mal
        persistido). Camino: DNI -> res (por emp.dni o res.cif) -> reshor.
        Un DNI cuenta como CON extra si CUALQUIERA de sus recursos tiene
        una hora ext=1. Devuelve el conjunto de DNIs (normalizados) SIN
        extra, que son los que se excluyen de los totales.
        """
        norm = {self._norm_dni(d) for d in dnis if d}
        norm.discard("")
        if not norm:
            return set()
        placeholders = ",".join("?" for _ in norm)
        # "Tiene extra" = el recurso tiene alguna hora en reshor cuyo
        # CODIGO empieza por 'HE' (Hora Extra ...). El flag auxhor.ext
        # NO es fiable aqui: en los datos reales viene 0 incluso para las
        # HE*, asi que se usa el prefijo del codigo, que es inequivoco.
        # es_he = 1 si cod LIKE 'HE%', 0 en caso contrario; MAX por DNI.
        sql = (
            "SELECT dnin AS dni, MAX(es_he) AS max_he FROM ("
            "  SELECT REPLACE(REPLACE(UPPER(ISNULL(emp.dni,'')),'-',''),' ','')"
            "         AS dnin,"
            "         CASE WHEN auxhor.cod LIKE 'HE%' THEN 1 ELSE 0 END AS es_he"
            "  FROM res"
            "  JOIN emp ON emp.ide = res.conide"
            "  LEFT JOIN reshor ON reshor.reside = res.ide"
            "  LEFT JOIN auxhor ON auxhor.ide = reshor.horide"
            f"  WHERE REPLACE(REPLACE(UPPER(ISNULL(emp.dni,'')),'-',''),' ','')"
            f"        IN ({placeholders})"
            "  UNION ALL"
            "  SELECT REPLACE(REPLACE(UPPER(ISNULL(res.cif,'')),'-',''),' ','')"
            "         AS dnin,"
            "         CASE WHEN auxhor.cod LIKE 'HE%' THEN 1 ELSE 0 END AS es_he"
            "  FROM res"
            "  LEFT JOIN reshor ON reshor.reside = res.ide"
            "  LEFT JOIN auxhor ON auxhor.ide = reshor.horide"
            f"  WHERE REPLACE(REPLACE(UPPER(ISNULL(res.cif,'')),'-',''),' ','')"
            f"        IN ({placeholders})"
            ") q GROUP BY dnin"
        )
        params = list(norm) + list(norm)
        columns, rows = self._post_sql_read(
            sql=sql, parameters=params, label="dnis_sin_extra"
        )
        idx = {c.lower(): i for i, c in enumerate(columns)}
        con_extra: set[str] = set()
        vistos: set[str] = set()
        for row in rows:
            d = _opt_str(row[idx["dni"]])
            mx = _opt_int(row[idx["max_he"]])
            if not d:
                continue
            vistos.add(d)
            if mx and mx >= 1:
                con_extra.add(d)
        # SIN extra = pedidos que NO resultaron con extra (incluye los que
        # no aparecieron: sin recurso o sin reshor -> sin extra).
        sin = {d for d in norm if d not in con_extra}
        logger.info(
            "[sigrid-lookup] dnis_sin_extra: pedidos=%s con_extra=%s "
            "sin_extra=%s no_localizados=%s",
            len(norm), len(con_extra), len(sin),
            sorted(norm - vistos),
        )
        return sin

    @staticmethod
    def _norm_dni(dni: str | None) -> str:
        import re
        return re.sub(r"[^0-9A-Za-z]", "", dni or "").upper()

    def fetch_hora_extra_recurso(self, reside: int) -> dict | None:
        """Primera hora EXTRA (ext=1) del recurso en ``reshor``: para
        proponer el codigo al crear una linea extra desde la matriz."""
        sql = (
            "SELECT TOP 1 reshor.horide AS ide, auxhor.cod AS cod, "
            "auxhor.res AS res, reshor.pre AS pre "
            "FROM reshor JOIN auxhor ON auxhor.ide = reshor.horide "
            "WHERE auxhor.cod LIKE 'HE%' AND reshor.reside = ? "
            "ORDER BY auxhor.cod"
        )
        columns, rows = self._post_sql_read(
            sql=sql, parameters=[int(reside)], label="hora_extra_recurso"
        )
        if not rows:
            return None
        rm = dict(zip([c.lower() for c in columns], rows[0]))
        return {
            "ide": _opt_int(rm.get("ide")),
            "cod": _opt_str(rm.get("cod")),
            "res": _opt_str(rm.get("res")),
            "pre": _opt_float(rm.get("pre")),
        }

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
