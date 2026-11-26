"""Evaluador determinista de compatibilidad y filtrado de ofertas laborales."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from job_automator.config import get_search_preferences


PROGRAMMER_IT_TOKENS = (
    "software",
    "sistemas",
    "ti",
    "web",
    "backend",
    "frontend",
    "fullstack",
    "java",
    "python",
    "c#",
    ".net",
    "desarrollo",
    "computac",
)

INDUSTRIAL_MINING_KEYWORDS = (
    "área seca",
    "area seca",
    "tronadura",
    "faena",
    "mina",
    "minería",
    "mineria",
    "equipos mina",
    "maquinaria pesada",
    "chancado",
    "flotación",
    "fundición",
    "relaves",
    "electromecánico",
    "electromecanico",
    "parada de planta",
    "instrumentista",
    "oleohidráulico",
    "caldera",
    "soldador",
    "taller mantencion",
)

TITLE_MAINTENANCE_KEYWORDS = (
    "mantenimiento",
    "mantención",
    "mantencion",
    "mecánico",
    "mecanico",
    "electromecánico",
    "electromecanico",
    "industrial",
    "producción",
    "produccion",
    "parada de planta",
    "taller mantencion",
    "flota",
    "rutas",
    "despacho",
    "despachos",
    "logístico",
    "logistico",
    "logística",
    "logistica",
    "ductero",
    "soldador",
    "aseo",
    "bodeguero",
    "enfermero",
    "guardia",
)


@dataclass
class EvaluationResult:
    eligible: bool
    score: float
    decision: str  # 'ELIGIBLE' | 'REJECTED_FILTER' | 'REJECTED_PROFILE'
    reason: str
    detected_modality: str


def evaluate_offer(
    title: str,
    description: str = "",
    location: str = "",
    modality: str = "",
    preferences: dict[str, Any] | None = None,
    url: str = "",
) -> EvaluationResult:
    """Evalúa de forma determinista si una oferta laboral es compatible con el perfil y preferencias."""
    prefs = preferences or get_search_preferences()
    title_low = (title or "").lower().strip()
    desc_low = (description or "").lower().strip()
    loc_low = (location or "").lower().strip()
    mod_low = (modality or "").lower().strip()
    url_low = (url or "").lower().strip()
    full_text = f"{title_low} {desc_low}"

    if not title_low:
        return EvaluationResult(False, 0.0, "REJECTED_FILTER", "Título de oferta vacío", "No especificada")

    # 1. Exclusiones directas en el título (rubros no TI o prácticas estudiantiles)
    for excl in prefs.get("exclusion_title_keywords", []):
        if re.search(r"(?<![a-zA-Z0-9_áéíóúÁÉÍÓÚñÑ])" + re.escape(excl.lower()), title_low):
            return EvaluationResult(
                False,
                0.0,
                "REJECTED_FILTER",
                f"Excluida por palabra clave ajena al perfil en título: '{excl}'",
                modality or "Presencial",
            )

    # 1b. Desambiguación para títulos con 'programador' / 'programadora' (evitar falsos positivos mineros/industriales)
    is_programmer_title = bool(
        re.search(r"(?<![a-zA-Z0-9_áéíóúÁÉÍÓÚñÑ])programador[a-z]*(?![a-zA-Z0-9_áéíóúÁÉÍÓÚñÑ])", title_low)
    )
    if is_programmer_title:
        # Si el título indica mantenimiento o producción industrial
        for tm in TITLE_MAINTENANCE_KEYWORDS:
            if re.search(r"(?<![a-zA-Z0-9_áéíóúÁÉÍÓÚñÑ])" + re.escape(tm) + r"(?![a-zA-Z0-9_áéíóúÁÉÍÓÚñÑ])", title_low):
                return EvaluationResult(
                    False,
                    0.0,
                    "REJECTED_FILTER",
                    f"Cargo de programador de mantenimiento/industrial ajeno a informática en título ('{tm}')",
                    modality or "Presencial",
                )
        # Si la oferta pertenece a faenas mineras o rubros industriales pesados
        for ind in INDUSTRIAL_MINING_KEYWORDS:
            if re.search(r"(?<![a-zA-Z0-9_áéíóúÁÉÍÓÚñÑ])" + re.escape(ind) + r"(?![a-zA-Z0-9_áéíóúÁÉÍÓÚñÑ])", full_text):
                return EvaluationResult(
                    False,
                    0.0,
                    "REJECTED_FILTER",
                    f"Cargo de programador de faena/minería ajeno a informática ('{ind}')",
                    modality or "Presencial",
                )
        # Debe co-ocurrir obligatoriamente con al menos un token de software/TI
        def check_token(tok: str) -> bool:
            if tok == "ti":
                # 'ti' en el título como palabra completa (ej: Programador TI)
                if re.search(r"(?<![a-zA-Z0-9_áéíóúÁÉÍÓÚñÑ])ti(?![a-zA-Z0-9_áéíóúÁÉÍÓÚñÑ])", title_low):
                    return True
                # En la descripción, TI debe estar en contexto de tecnología y no como pronombre ('para ti', 'a ti')
                if re.search(r"\b(?:área|area|soporte|consultor|sector|departamento|dpto|tecnología|tecnologia|servicios|proyectos|equipo|profesional|soluciones|infraestructura)\s+(?:de\s+)?ti\b", full_text):
                    return True
                if re.search(r"\bti\s+(?:y|e|en)\s+(?:redes|sistemas|comunicaciones|desarrollo|infraestructura)\b", full_text):
                    return True
                return False
            return bool(re.search(r"(?<![a-zA-Z0-9_áéíóúÁÉÍÓÚñÑ])" + re.escape(tok) + r"(?![a-zA-Z0-9_áéíóúÁÉÍÓÚñÑ])", full_text))

        has_it_token = any(check_token(tok) for tok in PROGRAMMER_IT_TOKENS)
        if not has_it_token:
            return EvaluationResult(
                False,
                0.0,
                "REJECTED_FILTER",
                "Cargo con 'programador' sin token obligatorio de software/TI en título o descripción",
                modality or "Presencial",
            )

    # 2. Inclusión obligatoria de al menos una palabra clave TI (a menos que allow_all_jobs esté activo)
    allow_all = prefs.get("allow_all_jobs", False)
    inc_keywords = prefs.get("inclusion_keywords", [])
    matched_keywords = [
        kw
        for kw in inc_keywords
        if re.search(
            r"(?<![a-zA-Z0-9_áéíóúÁÉÍÓÚñÑ])"
            + re.escape(kw.lower())
            + r"(?![a-zA-Z0-9_áéíóúÁÉÍÓÚñÑ])",
            full_text,
        )
    ]
    if not matched_keywords and not allow_all:
        return EvaluationResult(
            False,
            0.1,
            "REJECTED_FILTER",
            "No contiene palabras clave de Informática / Software / Soporte TI",
            modality or "Presencial",
        )

    # 3. Detectar modalidad
    detected_mod = "Presencial"
    if "remoto" in full_text or "teletrabajo" in full_text or "remote" in full_text or "100% online" in full_text or "remoto" in mod_low:
        detected_mod = "Remoto"
    elif "híbrido" in full_text or "hibrido" in full_text or "mixto" in full_text or "híbrido" in mod_low:
        detected_mod = "Híbrido"

    # Si se excluyen remotos según preferencias
    modalities_cfg = prefs.get("modalities", {})
    if modalities_cfg.get("exclude_remote", False) or not prefs.get("regions", {}).get("remote_countrywide", True):
        if detected_mod == "Remoto":
            return EvaluationResult(
                False,
                0.0,
                "REJECTED_FILTER",
                "Excluida por modalidad: Cargo 100% Remoto (preferencia: solo presencial / híbrido en regiones)",
                detected_mod,
            )

    # 4. Ubicación y Regiones excluidas (si all_regions_enabled es False)
    regions_cfg = prefs.get("regions", {})
    if not regions_cfg.get("all_regions_enabled", False):
        excluded_regions = regions_cfg.get("excluded_regions", [])
        for exc in excluded_regions:
            exc_norm = exc.lower().strip()
            if not exc_norm:
                continue
            pattern = r"(?<![a-zA-Z0-9_áéíóúÁÉÍÓÚñÑ])" + re.escape(exc_norm) + r"(?![a-zA-Z0-9_áéíóúÁÉÍÓÚñÑ])"
            if re.search(pattern, loc_low) or re.search(pattern, title_low) or re.search(pattern, desc_low) or (exc_norm in url_low):
                return EvaluationResult(
                    False,
                    0.0,
                    "REJECTED_FILTER",
                    f"Excluida por región/ciudad no deseada: '{exc}' (ubicación, URL o descripción)",
                    detected_mod,
                )

    # 5. Penalizar ofertas que exijan > 5 años excluyentes en el título o descripción
    m_years = re.search(r"(?:mínimo|minimo|al menos|excluyente)\s*(\d+)\s*años\s*de\s*experiencia", desc_low)
    if m_years and int(m_years.group(1)) >= 6:
        return EvaluationResult(
            False,
            0.35,
            "REJECTED_PROFILE",
            f"Exige {m_years.group(1)} años de experiencia excluyente (perfil cuenta con 2 años)",
            detected_mod,
        )

    if allow_all and not matched_keywords:
        score = 0.70
        reason_msg = "Oferta compatible con modalidad abierta / inserción rápida (sueldo mínimo)"
    else:
        score = min(1.0, 0.55 + 0.08 * len(set(matched_keywords)))
        reason_msg = f"Compatible con perfil TI ({', '.join(matched_keywords[:4])})"

    return EvaluationResult(
        True,
        round(score, 2),
        "ELIGIBLE",
        reason_msg,
        detected_mod,
    )
