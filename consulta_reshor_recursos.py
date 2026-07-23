# consulta_reshor_recursos.py
"""Diagnostico standalone: recursos y su hora extra, PARTIENDO DEL DNI.

Objetivo: entender por que la exclusion de totales (trabajadores sin codigo
de hora extra) marca a quien no debe. El DNI es el ancla fiable; a partir de
el se localiza el/los recurso(s) 'res' del empleado y se listan sus horas de
'reshor' (con el flag ext), replicando la consulta EXACTA de sv4.

Relacion en Sigrid:
  - emp.dni : DNI del empleado (tabla emp, extiende con)
  - res.conide -> emp.ide : el recurso apunta a su empleado
  - res.cif : CIF/NIF del propio recurso (a veces trae el DNI directamente)
Se prueban AMBOS caminos (por emp.dni y por res.cif).

Uso: rellena DNIS y ejecuta. Lee SIGRID_API_* del .env de partes-front.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import httpx

# ----------------------------- CONFIG ----------------------------- #
DNIS = [
    "28669109T",   # Jose Gomez Garcia (encargado; ESPERADO sin extra)
    "14320168T",   # Fco Javier Roldan (gruista)
    "28790165F",   # Jose Luis Colmenar (oficial 1a albanil)
    "75435658M",   # Rafael Serrano (capataz)
]
ENV_PATH = Path(r"C:\Users\pgris\PycharmProjects\partes-front\.env")
# ------------------------------------------------------------------ #


def _leer_dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.exists():
        raise SystemExit(f"No existe {path}")
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _database(cfg: dict[str, str]) -> str:
    return (cfg.get("SIGRID_API_DATABASE")
            or cfg.get("SIGRID_DATABASE")
            or "ruesma")


def _norm_dni(dni: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", dni or "").upper()


def sql_read(cfg: dict[str, str], sql: str, parameters: list) -> list[dict]:
    url = cfg["SIGRID_API_BASE_URL"].rstrip("/") + "/api/sql/read"
    payload = {
        "database": _database(cfg),
        "sql": sql,
        "parameters": parameters,
        "timeout_seconds": 60,
        "max_rows": 10000,
    }
    headers = {
        "x-functions-key": cfg["SIGRID_API_FUNCTION_KEY"],
        "Content-Type": "application/json",
    }
    r = httpx.post(url, json=payload, headers=headers, timeout=90)
    if r.status_code >= 400:
        raise SystemExit(
            f"sigrid-api HTTP {r.status_code} (database={payload['database']})"
            f":\n{r.text[:800]}"
        )
    body = r.json()
    if not body.get("ok"):
        raise SystemExit(f"sigrid-api ok=false: {json.dumps(body)[:400]}")
    cols = [c.lower() for c in body["columns"]]
    return [dict(zip(cols, row)) for row in body["rows"]]


def recursos_por_dni(cfg: dict[str, str], dni: str) -> list[dict]:
    """Todos los recursos 'res' del empleado con ese DNI, por los dos
    caminos (emp.dni y res.cif). Devuelve [{reside, cif, conide, restip}]."""
    dn = _norm_dni(dni)
    sql = (
        "SELECT res.ide AS reside, res.cif AS cif, res.conide AS conide, "
        "auxrestip.res AS restip, rescon.res AS recurso_nombre "
        "FROM res "
        "JOIN con rescon ON rescon.ide = res.ide "
        "LEFT JOIN auxrestip ON auxrestip.ide = res.restipide "
        "LEFT JOIN emp ON emp.ide = res.conide "
        "WHERE REPLACE(REPLACE(UPPER(ISNULL(emp.dni,'')),'-',''),' ','') = ? "
        "   OR REPLACE(REPLACE(UPPER(ISNULL(res.cif,'')),'-',''),' ','') = ?"
    )
    return sql_read(cfg, sql, [dn, dn])


def reshor_de(cfg: dict[str, str], reside_list: list[int]) -> list[dict]:
    if not reside_list:
        return []
    in_list = ",".join(str(int(i)) for i in reside_list)
    return sql_read(cfg, (
        "SELECT reshor.reside AS reside, auxhor.cod AS cod, "
        "auxhor.res AS res, auxhor.ext AS ext, reshor.candef AS candef "
        f"FROM reshor JOIN auxhor ON auxhor.ide = reshor.horide "
        f"WHERE reshor.reside IN ({in_list}) "
        "ORDER BY reshor.reside, auxhor.ext, auxhor.cod"
    ), [])


def con_extra_sv4(cfg: dict[str, str], reside_list: list[int]) -> set[int]:
    """Consulta EXACTA de sv4: hora extra = codigo LIKE 'HE%'."""
    if not reside_list:
        return set()
    in_list = ",".join(str(int(i)) for i in reside_list)
    filas = sql_read(cfg, (
        "SELECT DISTINCT reshor.reside AS reside "
        "FROM reshor JOIN auxhor ON auxhor.ide = reshor.horide "
        f"WHERE auxhor.cod LIKE 'HE%' AND reshor.reside IN ({in_list})"
    ), [])
    return {int(f["reside"]) for f in filas}


def main() -> int:
    cfg = _leer_dotenv(ENV_PATH)
    print(f"base de datos: {_database(cfg)}\n")

    todos_reside: list[int] = []
    reside_por_dni: dict[str, list[int]] = {}

    for dni in DNIS:
        print(f"===== DNI {dni} =====")
        recs = recursos_por_dni(cfg, dni)
        if not recs:
            print("  (ningun recurso 'res' localizado por emp.dni ni res.cif)")
            reside_por_dni[dni] = []
            continue
        ides = []
        for r in recs:
            rid = int(r["reside"])
            ides.append(rid)
            print(f"  reside={rid}  cif={r.get('cif')!r}  "
                  f"conide={r.get('conide')}  restip={r.get('restip')!r}  "
                  f"{r.get('recurso_nombre')}")
        reside_por_dni[dni] = ides
        todos_reside.extend(ides)

        filas = reshor_de(cfg, ides)
        # Criterio REAL: hora extra = codigo que empieza por 'HE'
        # (el flag auxhor.ext viene 0 incluso para las HE*).
        con_extra = {int(f["reside"]) for f in filas
                     if str(f["cod"] or "").upper().startswith("HE")}
        for f in filas:
            es_he = str(f["cod"] or "").upper().startswith("HE")
            marca = "  <-- EXTRA (HE)" if es_he else ""
            print(f"     reshor reside={f['reside']} cod={f['cod']:<10} "
                  f"ext={f['ext']!r:<5} candef={f['candef']}  "
                  f"{f['res']}{marca}")
        estado = "TIENE extra" if con_extra else "SIN extra (se excluiria)"
        print(f"  >>> {estado}\n")

    print("===== RESUMEN (consulta exacta de sv4) =====")
    con = con_extra_sv4(cfg, todos_reside)
    for dni in DNIS:
        ides = reside_por_dni.get(dni, [])
        tiene = any(i in con for i in ides)
        print(f"  {dni}: reside={ides}  ->  "
              f"{'CUENTA en totales' if tiene else 'EXCLUIDO de totales'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())