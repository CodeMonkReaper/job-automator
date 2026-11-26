"""Validador de veracidad (Fact-Checker) para impedir invención de experiencia o certificaciones."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from job_automator.config import get_candidate_profile


@dataclass
class FactCheckResult:
    valid: bool
    violations: list[str]


def validate_text_truthfulness(
    text: str,
    profile: dict[str, Any] | None = None,
) -> FactCheckResult:
    """Verifica que una respuesta o carta de presentación no invente experiencia ni certificaciones."""
    prof = profile or get_candidate_profile()
    max_years = int(prof.get("total_experience_years", 2))
    violations: list[str] = []
    low = (text or "").lower()

    # 1. Verificar afirmaciones prohibidas explícitas
    for forbidden in prof.get("forbidden_claims", []):
        if forbidden.lower() in low:
            # Si la afirmación prohibida se refiere a poseer licencia de conducir, verificar si está negada explícitamente
            if any(lic in forbidden.lower() for lic in ("licencia", "conducir")):
                negations = ("no cuento", "no poseo", "no tengo", "no dispongo", "sin licencia")
                if any(neg in low for neg in negations):
                    continue
            violations.append(f"Afirmación no autorizada detectada: '{forbidden}'")

    # 2. Verificar que no declare más años de experiencia laboral que los reales (2 años)
    for m in re.finditer(r"(\d+)\s*(?:\+\s*)?años\s+de\s+(?:experiencia|trayectoria)", low):
        claimed_years = int(m.group(1))
        if claimed_years > max_years:
            violations.append(
                f"Declara {claimed_years} años de experiencia laboral, superando el máximo real comprobado ({max_years} años)"
            )

    # 3. Verificar que no declare credencial de discapacidad si no la posee
    if not prof.get("disability_credential", False):
        if "cuento con credencial de discapacidad" in low or "poseo credencial de discapacidad" in low:
            if "no cuento con credencial" not in low and "no poseo credencial" not in low:
                violations.append("Afirma poseer credencial de discapacidad en contradicción con el perfil real")

    # 4. Verificar que no declare poseer licencia de conducir si el perfil indica que no la posee
    has_driver_license = "posee licencia" in prof.get("driver_license", "").lower() and "no posee" not in prof.get("driver_license", "").lower()
    if not has_driver_license:
        if any(term in low for term in ("cuento con licencia", "poseo licencia", "tengo licencia", "licencia clase b al día", "licencia clase b vigente", "licencia vigente")):
            if not any(neg in low for neg in ("no cuento con licencia", "no poseo licencia", "no tengo licencia", "no dispongo de licencia")):
                violations.append("Afirma poseer licencia de conducir en contradicción con el perfil real (el candidato no posee licencia)")

    return FactCheckResult(valid=len(violations) == 0, violations=violations)
