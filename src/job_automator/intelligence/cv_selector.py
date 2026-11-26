"""Selector determinista y auditable de CV especializado."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from job_automator.config import get_cv_catalog, get_cvs_dir, get_search_preferences, get_workspace_root


@dataclass
class CVSelection:
    profile_key: str
    cv_base: str
    target_name: str
    src_docx: Path
    src_pdf: Path
    salary_clp: int
    salary_category: str
    confidence: str = "HIGH"  # "HIGH", "MEDIUM", "LOW"
    score: int = 0
    matched_keywords: list[str] = field(default_factory=list)
    selection_reason: str = ""
    requires_human_confirmation: bool = False


def _match_keyword(keyword: str, text: str) -> bool:
    """Comprueba coincidencia de palabra clave respetando límites de palabra."""
    kw = keyword.strip().lower()
    if not kw or not text:
        return False

    pattern = r"(?<![a-zA-Z0-9_áéíóúÁÉÍÓÚñÑ])" + re.escape(kw) + r"(?![a-zA-Z0-9_áéíóúÁÉÍÓÚñÑ])"
    return bool(re.search(pattern, text.lower()))


def _resolve_cv_directory(catalog: dict[str, Any]) -> Path:
    if os.environ.get("JOB_AUTOMATOR_CVS_DIR"):
        return get_cvs_dir()
    raw_dir = catalog.get("cv_directory")
    if raw_dir:
        cand = Path(str(raw_dir))
        if not cand.is_absolute():
            return (get_workspace_root() / cand).resolve()
        if cand.exists():
            return cand
    return get_cvs_dir()


def select_cv_and_salary(
    title: str,
    description: str = "",
    salary_strategy: str | None = None,
    catalog: dict[str, Any] | None = None,
    preferences: dict[str, Any] | None = None,
    min_confidence_score: int = 10,
) -> CVSelection:
    """Selecciona el CV base más afín del catálogo y determina la pretensión salarial en CLP."""
    cat = catalog or get_cv_catalog()
    prefs = preferences or get_search_preferences()

    strategy_name = (salary_strategy or prefs.get("salary_policy", {}).get("default_strategy", "FAST_HIRE")).upper()
    strategies = prefs.get("salary_policy", {}).get("strategies", {})
    strategy_table = strategies.get(strategy_name, strategies.get("FAST_HIRE", {}))

    title_clean = (title or "").strip()
    desc_clean = (description or "").strip()
    title_low = title_clean.lower()

    is_part_time = any(pt in title_low for pt in ["part time", "part-time", "media jornada", "fin de semana"])

    profile_scores: list[tuple[int, dict[str, Any], list[str]]] = []

    for prof in cat.get("profiles", []):
        score = 0
        matched_kws: list[str] = []
        for kw in prof.get("keywords", []):
            if _match_keyword(kw, title_clean):
                score += 15
                matched_kws.append(f"title:{kw}")
            elif _match_keyword(kw, desc_clean):
                score += 2
                matched_kws.append(f"desc:{kw}")

        if score > 0:
            profile_scores.append((score, prof, matched_kws))

    profile_scores.sort(key=lambda x: x[0], reverse=True)

    default_base = cat.get("default_cv_base", "CV_Candidate_Software_Engineer")
    default_target = cat.get("default_target_name", "CV_Candidate_Software_Engineer")

    best_profile: dict[str, Any] | None = None
    best_score = 0
    matched_kws_final: list[str] = []
    confidence = "HIGH"
    requires_confirmation = False
    reason = ""

    if not profile_scores or profile_scores[0][0] < min_confidence_score:
        cv_base = default_base
        target_name = default_target
        sal_cat = "developer_and_infra"
        p_key = "default_profile"
        best_score = profile_scores[0][0] if profile_scores else 0
        confidence = "HIGH" if any(term in title_low for term in ["ingeniero", "informática", "informatica", "software", "desarrollo"]) else "MEDIUM"
        reason = (
            f"Asignado CV Base ({default_base}): Sin coincidencia de especialidad unívoca (puntaje {best_score} < umbral {min_confidence_score})."
        )
    else:
        top_score, top_prof, top_kws = profile_scores[0]
        if len(profile_scores) > 1 and (top_score - profile_scores[1][0]) <= 2:
            confidence = "LOW"
            requires_confirmation = True
            reason = (
                f"Ambigüedad entre {top_prof['key']} ({top_score} pts) y {profile_scores[1][1]['key']} ({profile_scores[1][0]} pts). "
                f"Términos coincidentes: {', '.join(top_kws)}."
            )
        else:
            confidence = "HIGH" if top_score >= 15 else "MEDIUM"
            reason = (
                f"Coincidencia especializada con {top_prof['key']} (puntaje {top_score}). "
                f"Términos clave detectados: {', '.join(top_kws)}."
            )

        best_profile = top_prof
        best_score = top_score
        matched_kws_final = top_kws
        cv_base = best_profile["cv_base"]
        target_name = best_profile["target_name"]
        sal_cat = best_profile.get("salary_category", "developer_and_infra")
        p_key = best_profile["key"]

    if is_part_time:
        salary_clp = int(strategy_table.get("part_time", 700000))
    elif sal_cat == "minimum_wage":
        salary_clp = int(strategy_table.get("minimum_wage", 700000))
    else:
        salary_clp = int(strategy_table.get(sal_cat, 800000))

    cv_dir = _resolve_cv_directory(cat)
    src_docx = cv_dir / f"{cv_base}.docx"
    src_pdf = cv_dir / f"{cv_base}.pdf"

    return CVSelection(
        profile_key=p_key,
        cv_base=cv_base,
        target_name=target_name,
        src_docx=src_docx,
        src_pdf=src_pdf,
        salary_clp=salary_clp,
        salary_category=sal_cat,
        confidence=confidence,
        score=best_score,
        matched_keywords=matched_kws_final,
        selection_reason=reason,
        requires_human_confirmation=requires_confirmation,
    )
