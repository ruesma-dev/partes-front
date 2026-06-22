# application/services/tipo_hora_matcher.py
"""Casa CATEGORIA del trabajador + tipo (normal/extra) con el CODIGO DE HORA
de Sigrid (``auxhor``), para la creacion MANUAL de partes en sv4.

Replica el matcheo determinista de sv3 (``tipo_hora_resolver._best_cat``):
clasifica extra vs normal por la palabra 'EXTRA' en la descripcion, y elige
el codigo con mas solape de tokens de categoria (CAPATAZ, OFICIAL, ...),
priorizando 'LABORABLE' para las normales y menos tokens sobrantes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_STOP_TOKENS = {
    "HORA", "HORAS", "LABORABLE", "LABORABLES", "EXTRA", "EXTRAS",
    "MES", "MESES", "DE", "DEL", "LA", "EL", "POR", "Y",
}
_CAT_SYN = {
    "OF": "OFICIAL", "OFIC": "OFICIAL", "OFICIAL": "OFICIAL",
    "CAP": "CAPATAZ", "CAPATAZ": "CAPATAZ",
    "AY": "AYUDANTE", "AYTE": "AYUDANTE", "AYUD": "AYUDANTE",
    "AYUDANTE": "AYUDANTE", "AYDTE": "AYUDANTE",
    "ENC": "ENCARGADO", "ENCARGADO": "ENCARGADO",
    "GRUA": "GRUISTA", "GRUISTA": "GRUISTA",
    "PEON": "PEON", "PEONES": "PEON",
    "MIRAS": "MIRAS",
    "ESP": "ESPECIALISTA", "ESPECIALISTA": "ESPECIALISTA",
}
_ACCENTS = str.maketrans("ÁÉÍÓÚÜÑáéíóúüñ", "AEIOUUNAEIOUUN")


def _category_tokens(descripcion: str | None) -> set[str]:
    if not descripcion:
        return set()
    words = re.findall(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+", descripcion.upper())
    return {w for w in words if w not in _STOP_TOKENS and len(w) >= 3}


def _normalize_categoria(categoria: str | None) -> set[str]:
    if not categoria:
        return set()
    raw = categoria.translate(_ACCENTS).upper()
    raw = re.sub(r"[^A-Z0-9 ]", " ", raw)
    raw = re.sub(r"\b\d+[AOª]?\b", " ", raw)
    toks: set[str] = set()
    for w in raw.split():
        if w in _CAT_SYN:
            toks.add(_CAT_SYN[w])
        elif len(w) >= 3 and w not in _STOP_TOKENS:
            toks.add(w)
    return toks


@dataclass
class HoraMatch:
    ide: int
    codigo: str | None
    descripcion: str | None
    ext: int
    pre: float | None
    prenom: float | None
    method: str


class TipoHoraMatcher:
    """``tipos`` son objetos con .ide .codigo .descripcion .ext .pre .prenom
    (p.ej. ``TipoHoraOption`` del cliente Sigrid de sv4)."""

    def __init__(self, tipos) -> None:
        self._tipos = list(tipos)

    def _best_cat(self, cat_tokens: set[str], *, want_extra: bool):
        best = None
        best_key = (0, 0, 0)
        for t in self._tipos:
            desc = (t.descripcion or "").upper()
            is_extra = "EXTRA" in desc
            if is_extra != want_extra:
                continue
            cand = _category_tokens(t.descripcion)
            overlap = len(cat_tokens & cand)
            if overlap == 0:
                continue
            is_laborable = 1 if ("LABORABLE" in desc and not want_extra) else 0
            extra_tokens = len(cand - cat_tokens)
            key = (overlap, is_laborable, -extra_tokens)
            if key > best_key:
                best_key = key
                best = t
        return best

    def match(self, categoria: str | None, *, extra: bool) -> HoraMatch | None:
        cat = _normalize_categoria(categoria)
        if not cat:
            return None
        if extra:
            row = self._best_cat(cat, want_extra=True)
            if row is not None:
                return self._mk(row, "categoria_extra")
            row = self._best_cat(cat, want_extra=False)
            if row is not None:
                return self._mk(row, "categoria_extra_a_normal")
            return None
        row = self._best_cat(cat, want_extra=False)
        if row is not None:
            return self._mk(row, "categoria_normal")
        return None

    @staticmethod
    def _mk(t, method: str) -> HoraMatch:
        return HoraMatch(
            ide=t.ide, codigo=t.codigo, descripcion=t.descripcion,
            ext=int(t.ext), pre=getattr(t, "pre", None),
            prenom=getattr(t, "prenom", None), method=method,
        )
