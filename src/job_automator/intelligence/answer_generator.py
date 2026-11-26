"""Generador híbrido de cartas de presentación y respuestas a Killer Questions (Determinista + Caché SQLite)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from job_automator.config import get_candidate_profile
from job_automator.db.repository import JobRepository
from job_automator.intelligence.fact_checker import validate_text_truthfulness


@dataclass
class GeneratedAnswers:
    cover_letter: str
    answers_by_field: dict[str, str]
    unanswered_questions: list[dict[str, str]]
    used_cache_count: int


def format_clp(amount: int) -> str:
    return f"${amount:,}".replace(",", ".")


def build_cover_letter(
    company: str,
    title: str,
    salary_clp: int,
    profile: dict[str, Any] | None = None,
) -> str:
    prof = profile or get_candidate_profile()
    sal_fmt = format_clp(salary_clp)
    clean_title = (title or "").replace("…", "").strip(" -.,:")
    comp_clean = (company or "").replace("…", "").strip(" -.,:")
    comp_low = comp_clean.lower()
    title_low = clean_title.lower()

    is_invalid_company = (
        not comp_clean
        or comp_low in ("confidencial", "empresa", "empresa confidencial", "incognito", "incógnito", "selección", "seleccion")
        or "confidencial" in comp_low
        or comp_low == title_low
        or (len(title_low) >= 12 and (comp_low in title_low or title_low in comp_low))
    )
    corp_display = "Selección" if is_invalid_company else comp_clean

    edu = prof.get("education", {})
    companies = [c.get("name", "") for c in prof.get("authorized_companies", []) if c.get("name")]
    companies_txt = f" ({', '.join(companies)})" if companies else ""
    availability = prof.get("availability", "Inmediata")

    return (
        f"Estimado equipo de {corp_display}:\n\n"
        f"Me dirijo a ustedes para presentar mi postulación al cargo de {clean_title}. "
        f"Soy {edu.get('degree', 'Ingeniero en Informática')} ({edu.get('status', 'Titulado')}) en {edu.get('institution', 'Universidad')} "
        f"(carrera profesional de {edu.get('semesters', 8)} semestres) "
        f"y cuento con {prof.get('total_experience_years', 2)} años de experiencia profesional comprobable "
        f"en desarrollo de software, integración de APIs, bases de datos y continuidad operativa en producción{companies_txt}.\n\n"
        f"Mi pretensión de renta líquida es de {sal_fmt} mensuales (conversable) y dispongo de "
        f"disponibilidad {availability.lower()}.\n\n"
        f"Atentamente,\n"
        f"{prof.get('full_name', 'Candidato')}\n"
        f"{prof.get('phone', '')} | {prof.get('email', '')}"
    )


def answer_question_deterministically(
    label_text: str,
    salary_clp: int,
    location_hint: str = "",
    profile: dict[str, Any] | None = None,
) -> str | None:
    """Resuelve preguntas frecuentes sin consumir tokens usando hechos verificados del perfil."""
    lbl = (label_text or "").lower().strip()
    if not lbl:
        return None

    prof = profile or get_candidate_profile()
    edu = prof.get("education", {})
    degree = edu.get("degree", "Ingeniero en Informática")
    institution = edu.get("institution", "Universidad")
    semesters = edu.get("semesters", 8)
    period = edu.get("period", "")
    status = edu.get("status", "Titulado")
    years = int(prof.get("total_experience_years", 2))
    residence = location_hint or prof.get("residence", "Remoto")
    mobility = prof.get("strategy_guidelines", {}).get(
        "mobility_directive",
        f"{residence} (disponibilidad presencial, híbrida o remota)",
    )
    availability = prof.get("availability", "Inmediata")
    driver_lic = prof.get("driver_license", "No posee licencia de conducir de ninguna clase")
    has_license = "posee licencia" in driver_lic.lower() and "no posee" not in driver_lic.lower()
    companies = [c.get("name", "") for c in prof.get("authorized_companies", []) if c.get("name")]
    comp_clause = f" ({', '.join(companies)})" if companies else ""
    sal_fmt = format_clp(salary_clp)

    # 1. Preguntas compuestas frecuentes (ej: Licencia de conducir + Renta pretendida)
    has_lic = any(k in lbl for k in ("licencia", "conducir", "clase b"))
    has_ren = any(k in lbl for k in ("renta", "pretensión", "pretension", "sueldo", "líquido", "liquido", "salario"))
    if has_lic and has_ren:
        lic_txt = f"Cuento con {driver_lic}." if has_license else "Actualmente no cuento con licencia de conducir."
        return f"{lic_txt} Mi pretensión de renta líquida es de {sal_fmt} mensuales (disponibilidad {availability.lower()})."

    # 2. Gestión de incidentes, tickets y mesas de ayuda (ServiceNow, Jira, Zendesk)
    if any(k in lbl for k in ("servicenow", "ticket", "tickets", "incidente", "incidentes", "jira", "remedy", "zendesk", "glpi")):
        return f"Cuento con {years} años de experiencia profesional gestionando incidentes y requerimientos mediante tickets en mesas de ayuda y soporte TI (incluyendo ServiceNow, Jira y plataformas afines)."

    if any(k in lbl for k in ("renta", "pretensión", "pretension", "sueldo", "líquido", "liquido", "remuneración", "remuneracion", "salario")):
        return f"{sal_fmt} líquidos mensuales (disponibilidad {availability.lower()})."

    if any(k in lbl for k in ("disponibilidad", "comenzar", "inmediat", "incorporación", "incorporacion", "cuándo podrías", "cuando podrias")):
        modalities = ", ".join(prof.get("modalities", ["presencial", "híbrida", "remota"]))
        return f"Disponibilidad {availability.lower()} (modalidad {modalities.lower()})."

    if any(k in lbl for k in ("título", "titulo", "carrera", "estudio", "universidad", "institución", "institucion", "egresado", "titulado", "formación", "formacion")):
        return f"{degree} ({status}) en {institution} (carrera profesional de {semesters} semestres, {period})."

    if any(k in lbl for k in ("comuna", "residencia", "ciudad", "dónde vives", "donde vives", "ubicación", "ubicacion", "región", "region")):
        return f"{residence} — {mobility}"

    if any(k in lbl for k in ("licencia de conducir", "licencia clase b", "clase b", "conducir", "licencia")):
        return driver_lic if has_license else "No cuento con licencia de conducir de ninguna clase."

    if any(k in lbl for k in ("vehículo propio", "vehiculo propio", "movilización propia", "movilizacion propia", "auto propio", "movilidad propia")):
        return "No cuento con vehículo propio ni licencia de conducir; me movilizo en transporte público con puntualidad y disponibilidad."

    if any(k in lbl for k in ("discapacidad", "inclusión", "inclusion", "ley 21.015", "ajuste razonable")):
        has_dis = bool(prof.get("disability_credential", False))
        return "Cuento con credencial de discapacidad vigente." if has_dis else "No cuento con credencial de discapacidad ni requiero ajustes especiales para el proceso."

    if any(k in lbl for k in ("teléfono", "telefono", "celular", "contacto", "correo", "email", "whatsapp")):
        return f"Teléfono / WhatsApp: {prof.get('phone', '')} | Correo: {prof.get('email', '')}"

    if any(k in lbl for k in ("inglés", "ingles", "idioma")):
        return "Inglés técnico intermedio (lectura fluida de documentación técnica, APIs y repositorios)."

    if any(k in lbl for k in ("turno", "7x7", "noche", "rotativo", "terreno", "presencial")):
        return "Sí, cuento con total disponibilidad para trabajar en la modalidad, horario o terreno requerido."

    if any(k in lbl for k in ("soporte", "mesa de ayuda", "help desk", "hardware", "redes", "active directory", "windows", "linux", "infraestructura")):
        return f"{degree} ({institution}) con {years} años de experiencia en soporte técnico N1/N2, diagnóstico de hardware/software, redes TCP/IP, Windows/Linux y continuidad operativa."

    if any(k in lbl for k in (".net", "c#", "csharp")):
        return f"{years} años de experiencia profesional comprobable en desarrollo backend con .NET y C#, creación de APIs REST y bases de datos relacionales en producción."

    if any(k in lbl for k in ("java", "spring")) and "javascript" not in lbl:
        return f"{years} años de experiencia profesional en desarrollo backend, programación orientada a objetos con Java, Spring Boot, microservicios REST y bases de datos SQL."

    if any(k in lbl for k in ("python", "fastapi", "django", "machine learning", "opencv", "inteligencia artificial", "llm")):
        return f"{years} años de experiencia desarrollando en Python (automatización de procesos, APIs REST con FastAPI, procesamiento de datos y soluciones de IA){comp_clause}."

    if any(k in lbl for k in ("react", "next", "node", "nest", "typescript", "javascript", "fullstack", "full stack", "frontend", "desarrollo web")):
        return f"{years} años de experiencia desarrollando plataformas web en producción con React, Next.js, Node.js, NestJS, TypeScript y PostgreSQL{comp_clause}."

    if any(k in lbl for k in ("sql", "base de datos", "bases de datos", "oracle", "postgres", "etl", "power bi", "business intelligence")):
        return f"{years} años de experiencia en diseño y optimización de consultas SQL (PostgreSQL, SQL Server, MySQL, Oracle), flujos ETL e integración de datos."

    if any(k in lbl for k in ("quality assurance", "testing", "pruebas", "calidad", "automatizador")):
        return "Experiencia práctica diseñando y ejecutando pruebas unitarias, de integración y E2E en pipelines CI/CD, validación de APIs y consultas SQL."

    if any(k in lbl for k in ("erp", "sap", "odoo", "sistemas")):
        return f"{years} años de experiencia integrando sistemas de gestión empresarial, consumo de APIs REST, administración de bases de datos y soporte funcional/técnico a usuarios."

    return None


def generate_tailored_application_answer(
    question_text: str,
    offer_title: str = "",
    company: str = "",
    salary_clp: int = 850000,
    offer_desc: str = "",
    profile: dict[str, Any] | None = None,
) -> str:
    """Genera respuestas contextualizadas y veraces para preguntas abiertas."""
    from job_automator.intelligence.ai_client import synthesize_ai_answer_dynamic

    return synthesize_ai_answer_dynamic(
        question_text=question_text,
        offer_title=offer_title,
        company=company,
        salary_clp=salary_clp,
        offer_desc=offer_desc,
        profile=profile,
    )


def prepare_application_answers(
    company: str,
    title: str,
    salary_clp: int,
    questions: list[dict[str, str]],
    location_hint: str = "",
    repo: JobRepository | None = None,
    profile: dict[str, Any] | None = None,
) -> GeneratedAnswers:
    """
    Prepara la carta y respuestas para una lista de preguntas [{ 'field_name': 'q2', 'label': '...' }].
    Prioriza reglas deterministas -> Caché SQLite -> generación contextualizada.
    """
    prof = profile or get_candidate_profile()
    cover_letter = build_cover_letter(company, title, salary_clp, profile=prof)
    answers: dict[str, str] = {}
    unanswered: list[dict[str, str]] = []
    used_cache = 0

    for q in questions:
        fn = q.get("field_name", "")
        lbl = q.get("label", "").strip()
        if not fn or not lbl:
            continue

        # 1. Regla determinista
        det_ans = answer_question_deterministically(lbl, salary_clp, location_hint=location_hint, profile=prof)
        if det_ans:
            check = validate_text_truthfulness(det_ans, profile=prof)
            if check.valid:
                answers[fn] = det_ans
                continue

        # 2. Caché SQLite
        if repo is not None:
            cached = repo.get_cached_answer("KILLER_ANSWER", lbl)
            if cached and isinstance(cached.get("output"), str):
                cand_ans = cached["output"]
                if validate_text_truthfulness(cand_ans, profile=prof).valid:
                    answers[fn] = cand_ans
                    used_cache += 1
                    continue

        # 3. Generación contextualizada
        dyn_ans = generate_tailored_application_answer(
            question_text=lbl,
            offer_title=title,
            company=company,
            salary_clp=salary_clp,
            profile=prof,
        )
        if dyn_ans:
            check = validate_text_truthfulness(dyn_ans, profile=prof)
            if check.valid:
                answers[fn] = dyn_ans
                continue

        unanswered.append({"field_name": fn, "label": lbl})

    return GeneratedAnswers(
        cover_letter=cover_letter,
        answers_by_field=answers,
        unanswered_questions=unanswered,
        used_cache_count=used_cache,
    )
