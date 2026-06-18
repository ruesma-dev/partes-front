# application/services/empleado_reconciler.py
"""Segunda pasada de casado: para cada trabajador SIN CASAR (nombre leido)
propone los mejores candidatos del maestro de Sigrid, por similitud de
nombre (algoritmo de text_match: iniciales, umbral adaptativo, fonetico,
anclaje de apellido).

Clasifica cada nombre en:
  - "auto":     candidato claro (>= AUTO y margen sobre el 2o) -> se puede
                aplicar con un clic con confianza alta.
  - "revisar":  hay candidatos plausibles (banda SUGERENCIA..AUTO).
  - "sin":      ningun candidato decente (probablemente no esta en Sigrid).
  - "categoria":el "nombre" es en realidad una categoria/rol (sin nombre).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from application.services import text_match as tm
from infrastructure.sigrid.sigrid_lookup_client import EmpleadoOption

# Umbrales (ajustables).
SUGERENCIA = 0.55   # por debajo: no se muestra como candidato
AUTO = 0.85         # por encima (y con margen): casado claro
AUTO_MARGEN = 0.10  # diferencia minima con el 2o candidato para "auto"


@dataclass(frozen=True)
class Candidate:
    ide: int
    codigo: str | None
    nombre: str | None
    dni: str | None
    score: float


@dataclass
class ReconRow:
    nombre_leido: str
    bucket: str                       # auto | revisar | sin | categoria
    num_registros: int = 0
    obras: list[str] = field(default_factory=list)
    categorias: list[str] = field(default_factory=list)
    candidates: list[Candidate] = field(default_factory=list)


def candidates_for(
    nombre_leido: str,
    empleados: list[EmpleadoOption],
    *,
    top_n: int = 5,
    min_score: float = SUGERENCIA,
) -> list[Candidate]:
    scored: list[Candidate] = []
    for e in empleados:
        s = tm.name_similarity(nombre_leido, e.nombre)
        if s >= min_score:
            scored.append(Candidate(
                ide=e.ide, codigo=e.codigo, nombre=e.nombre,
                dni=e.dni, score=s,
            ))
    scored.sort(key=lambda c: c.score, reverse=True)
    return scored[:top_n]


def classify(
    nombre_leido: str,
    empleados: list[EmpleadoOption],
    *,
    top_n: int = 5,
) -> tuple[str, list[Candidate]]:
    if tm.looks_like_category(nombre_leido):
        return "categoria", []
    cands = candidates_for(nombre_leido, empleados, top_n=top_n)
    if not cands:
        return "sin", []
    best = cands[0].score
    second = cands[1].score if len(cands) > 1 else 0.0
    if best >= AUTO and (best - second) >= AUTO_MARGEN:
        return "auto", cands
    return "revisar", cands
