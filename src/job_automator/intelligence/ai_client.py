"""Cliente de Inteligencia Artificial para generación en tiempo real de respuestas a Killer Questions."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from job_automator.config import get_candidate_profile, get_workspace_root
from job_automator.intelligence.fact_checker import validate_text_truthfulness


def _load_env_api_key() -> str | None:
    """Intenta cargar GEMINI_API_KEY desde variables de entorno o archivo .env."""
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key

    candidates = (
        get_workspace_root() / ".env",
        Path.cwd() / ".env",
    )
    for candidate in candidates:
        if candidate.exists():
            for line in candidate.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("GEMINI_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
    return None


def call_gemini_api_realtime(
    prompt: str,
    api_key: str | None = None,
    timeout: float = 8.0,
) -> str | None:
    """Llama a la API de Gemini (generativelanguage.googleapis.com) en tiempo real sin dependencias pesadas."""
    key = api_key or _load_env_api_key()
    if not key:
        return None

    import urllib.request

    model = os.environ.get("GEMINI_MODEL", "gemini-1.5-flash").strip() or "gemini-1.5-flash"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 300,
        },
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            res_data = json.loads(resp.read().decode("utf-8"))
            candidates = res_data.get("candidates", [])
            if candidates:
                parts = candidates[0].get("content", {}).get("parts", [])
                if parts:
                    return parts[0].get("text", "").strip()
    except Exception:
        return None
    return None


def _profile_summary_fields(prof: dict[str, Any]) -> dict[str, Any]:
    edu = prof.get("education", {})
    companies = [c.get("name", "") for c in prof.get("authorized_companies", []) if c.get("name")]
    return {
        "full_name": prof.get("full_name", "Candidato"),
        "degree": edu.get("degree", "Ingeniero en Informática"),
        "institution": edu.get("institution", "Universidad"),
        "semesters": edu.get("semesters", 8),
        "period": edu.get("period", ""),
        "status": edu.get("status", "Titulado"),
        "years": int(prof.get("total_experience_years", 2)),
        "residence": prof.get("residence", "Remoto"),
        "companies_str": ", ".join(companies) if companies else "proyectos en producción",
        "driver_license": prof.get("driver_license", "No posee licencia de conducir"),
        "salary_directive": prof.get("strategy_guidelines", {}).get(
            "salary_directive",
            prof.get("salary_expectation", "Conversable y abierta al rango de mercado"),
        ),
        "mobility_directive": prof.get("strategy_guidelines", {}).get(
            "mobility_directive",
            f"Residencia en {prof.get('residence', 'Remoto')} con disponibilidad inmediata.",
        ),
        "availability": prof.get("availability", "Inmediata"),
    }


def _collect_direct_tech_tokens(prof: dict[str, Any]) -> set[str]:
    tokens: set[str] = set()
    for proj in prof.get("authorized_projects", []):
        for item in proj.get("stack", []):
            tokens.add(str(item).upper())
    skills = prof.get("skills", {})
    if isinstance(skills, dict):
        for cat_items in skills.values():
            for item in cat_items:
                tokens.add(str(item).upper())
    return tokens


def synthesize_ai_answer_dynamic(
    question_text: str,
    offer_title: str = "",
    company: str = "",
    salary_clp: int = 850000,
    offer_desc: str = "",
    profile: dict[str, Any] | None = None,
) -> str:
    """Genera respuestas dinámicas y contextualizadas a partir del perfil oficial configurado."""
    prof = profile or get_candidate_profile()
    meta = _profile_summary_fields(prof)
    q_low = (question_text or "").lower().strip()
    sal_fmt = f"${salary_clp:,}".replace(",", ".")

    # 1. Preguntas de residencia / ubicación / comuna / ciudad
    if any(k in q_low for k in ("comuna", "residencia", "ciudad", "dónde vives", "donde vives", "ubicación", "ubicacion", "región", "region")):
        if any(k in q_low for k in ("título", "titulo", "titulado", "titulación", "titulacion", "año", "ano")):
            return (
                f"Residencia: {meta['mobility_directive']} "
                f"Título profesional: {meta['degree']} ({meta['status']}) en {meta['institution']} "
                f"(carrera de {meta['semesters']} semestres, {meta['period']})."
            )
        return f"Residencia: {meta['mobility_directive']}"

    # 2. Licencia de conducir / vehículo propio (estricta verdad)
    if any(k in q_low for k in ("licencia", "conducir", "clase b", "auto propio", "vehículo propio", "vehiculo propio")):
        has_lic = "posee licencia" in meta["driver_license"].lower() and "no posee" not in meta["driver_license"].lower()
        lic_phrase = (
            f"Cuento con {meta['driver_license']}."
            if has_lic
            else "Actualmente no cuento con licencia de conducir de ninguna clase."
        )
        if any(k in q_low for k in ("renta", "pretensión", "pretension", "sueldo", "salario")):
            return f"{lic_phrase} Pretensión de renta líquida: {sal_fmt} mensuales (conversable, disponibilidad {meta['availability'].lower()})."
        return lic_phrase

    # 3. Pretensiones de renta
    if any(k in q_low for k in ("renta", "pretensión", "pretension", "sueldo", "líquido", "liquido", "remuneración", "remuneracion", "salario")):
        return f"{meta['salary_directive']} (referencia: {sal_fmt} líquidos mensuales, disponibilidad {meta['availability'].lower()})."

    # 4. Tipo de contrato / boleta de honorarios / facturación
    if any(k in q_low for k in ("boleta", "honorario", "factura", "modalidad contractual", "tipo de contrato", "emite boleta", "facturación", "facturacion")):
        contracts = ", ".join(prof.get("contract_types", ["Contrato dependiente", "Boletas de honorarios"]))
        return f"Cuento con total disponibilidad para adaptarme a la modalidad contractual requerida ({contracts})."

    # 5. Turnos rotativos / jornada / horarios
    if any(k in q_low for k in ("turno", "rotativo", "jornada", "5x2", "6x1", "7x7", "horario", "guardia")):
        return "Cuento con absoluta disponibilidad horaria y flexibilidad para trabajar en turnos rotativos, jornadas especiales o guardias según las necesidades operativas del equipo."

    # 6. Disponibilidad para incorporarse
    if any(k in q_low for k in ("disponibilidad", "comenzar", "inmediat", "incorporación", "incorporacion", "cuándo podrías", "cuando podrias")):
        modalities = ", ".join(prof.get("modalities", ["Presencial", "Híbrido", "Remoto"]))
        return f"Disponibilidad {meta['availability']} (modalidad: {modalities})."

    # 7. Título profesional o casa de estudios
    if any(k in q_low for k in ("título", "titulo", "carrera", "estudio", "universidad", "institución", "institucion", "egresado", "titulado", "formación", "formacion")):
        return (
            f"{meta['degree']} ({meta['status']}) en {meta['institution']} "
            f"(carrera profesional de {meta['semesters']} semestres, {meta['period']})."
        )

    # 8. Generación en vivo vía Gemini API si existe clave configurada
    gemini_key = _load_env_api_key()
    if gemini_key:
        prompt = (
            f"Actúa como {meta['full_name']}, {meta['degree']} ({meta['status']}) en {meta['institution']} con {meta['years']} años "
            f"de experiencia profesional comprobable en producción ({meta['companies_str']}). "
            f"{meta['mobility_directive']} "
            f"Licencia de conducir: {meta['driver_license']}. Renta: {meta['salary_directive']}. "
            f"Estás postulando al cargo de '{offer_title}' en la empresa '{company}'. "
            f"Responde la siguiente pregunta de postulación en primera persona, con enfoque técnico detallado y veraz: '{question_text}'. "
            f"Máximo 480 caracteres. Responde únicamente con el texto de la respuesta, sin saludos iniciales ni comillas."
        )
        ai_resp = call_gemini_api_realtime(prompt, api_key=gemini_key)
        if ai_resp:
            check = validate_text_truthfulness(ai_resp, profile=prof)
            if check.valid:
                return ai_resp

    # 9. Síntesis contextual determinista (Caso Único / Diamante en Bruto)
    tech_match = re.search(
        r"\b(sap|abap|is-u|kotlin|android|ios|swift|flutter|react|node|next|typescript|\.net|c#|python|fastapi|django|java|spring|sql|oracle|postgres|docker|kubernetes|aws|azure|gcp|linux|jira|servicenow|scrum|qa|testing|ciberseguridad|iso 27001|power bi|powerbi|pos|hardware|redes|soporte)\b",
        q_low,
    )
    target_tech = tech_match.group(1).upper() if tech_match else (offer_title or "el cargo requerido")
    known_tokens = _collect_direct_tech_tokens(prof)
    is_direct = any(target_tech in tok or tok in target_tech for tok in known_tokens) if known_tokens else False

    if is_direct:
        return (
            f"Como {meta['degree']} ({meta['institution']}) con {meta['years']} años de experiencia profesional en producción ({meta['companies_str']}), "
            f"cuento con dominio práctico en {target_tech}, integrando APIs REST y bases de datos relacionales "
            f"en proyectos operativos, lo que me permite aportar valor y autonomía técnica inmediata a {company or 'la empresa'}."
        )
    return (
        f"Como {meta['degree']} ({meta['institution']}) con {meta['years']} años de experiencia comprobada en producción ({meta['companies_str']}), "
        f"cuento con sólidas bases en lógica de programación, consumo de APIs, modelado de bases de datos y arquitectura de software. "
        f"Ante requerimientos en {target_tech}, aplico metodologías ágiles y equivalencias técnicas desde mi experiencia en backend "
        f"para lograr una rápida adaptación técnica y aportar valor operativo a {company or 'la empresa'}."
    )
