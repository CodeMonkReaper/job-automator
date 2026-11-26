"""Pruebas de evaluación de ofertas, selección de CVs del catálogo, respuestas deterministas y Fact-Checker."""

from __future__ import annotations

from job_automator.intelligence.answer_generator import (
    answer_question_deterministically,
    prepare_application_answers,
)
from job_automator.intelligence.cv_selector import select_cv_and_salary
from job_automator.intelligence.evaluator import evaluate_offer
from job_automator.intelligence.fact_checker import validate_text_truthfulness


def test_offer_evaluator_filters_correctly() -> None:
    # 1. Oferta TI compatible en Valparaíso -> Elegible
    res_ok = evaluate_offer(
        title="Analista Soporte Técnico TI N1/N2",
        description="Se requiere Ingeniero o Técnico en Informática para soporte en terreno y redes.",
        location="Valparaíso",
    )
    assert res_ok.eligible is True
    assert res_ok.decision == "ELIGIBLE"

    # 2. Oferta excluida por rubro no TI (Chofer / Guardia)
    res_excl = evaluate_offer(
        title="Chofer Conductor de Camión con Licencia A4",
        description="Transporte de carga en ruta.",
        location="Valparaíso",
    )
    assert res_excl.eligible is False
    assert res_excl.decision == "REJECTED_FILTER"

    # 3. Ofertas en Santiago y Concepción habilitadas sin filtro de ubicación
    res_stgo = evaluate_offer(
        title="Soporte TI Presencial",
        description="Trabajo presencial en sucursal.",
        location="Santiago",
    )
    assert res_stgo.eligible is True
    assert res_stgo.decision == "ELIGIBLE"

    res_ccp = evaluate_offer(
        title="Desarrollador Python",
        description="Desarrollo de software en oficina.",
        location="Concepción, Biobío",
    )
    assert res_ccp.eligible is True
    assert res_ccp.decision == "ELIGIBLE"

    # 4. Oferta 100% Remota -> Elegible
    res_remote = evaluate_offer(
        title="Desarrollador Full Stack Python React",
        description="Modalidad 100% teletrabajo / remoto desde todo Chile.",
        location="Antofagasta",
    )
    assert res_remote.eligible is True
    assert res_remote.detected_modality == "Remoto"

    # 5. Prueba de exclusión selectiva cuando all_regions_enabled es False
    custom_prefs = {
        "all_jobs_allowed": False,
        "inclusion_keywords": ["python", "soporte ti"],
        "exclusion_title_keywords": ["chofer"],
        "regions": {"all_regions_enabled": False, "excluded_regions": ["santiago"]},
        "modalities": {"exclude_remote": True},
    }
    res_custom = evaluate_offer(
        title="Soporte TI",
        location="Santiago",
        preferences=custom_prefs,
    )
    assert res_custom.eligible is False
    assert res_custom.decision == "REJECTED_FILTER"


def test_evaluator_rejects_mining_and_maintenance_programmers() -> None:
    """Verifica que ofertas de mantención/minería/tronadura con título 'programador' sean rechazadas."""
    false_positives = [
        ("Programador de Mantención Equipos Mina", "Mantenimiento preventivo en faena"),
        ("Operador programador tronadura", "Carga de explosivos y programación de tiros"),
        ("Programador de Proyecto / Sierra Gorda, Antofagasta", "Planificación de obras civiles y minería"),
        ("Programador de Mantenimiento", "Planificar paradas de planta y mantención de maquinaria"),
        ("Programador de Mantenimiento Chancado / Minería / 4x3", "Mantención de chancadores y correas"),
        ("Programadora/or de mantenimiento", "Coordinación de mantención industrial"),
        ("Programador de Flota y Rutas", "Programación de despachos y logística"),
    ]
    for title, desc in false_positives:
        res = evaluate_offer(title=title, description=desc)
        assert res.eligible is False, f"Falló rechazo de falso positivo: {title}"
        assert res.decision == "REJECTED_FILTER"

    # Ofertas legítimas de programación de software -> Deben ser ELIGIBLE
    valid_jobs = [
        ("Programador Python Backend", "Desarrollo de servicios API REST en Python"),
        ("Programador Web Full Stack", "Desarrollo con React y Node.js"),
        ("Analista Programador de Sistemas", "Desarrollo de software y mantención de sistemas informáticos"),
        ("Programador Java Junior", "Desarrollo de microservicios con Spring Boot"),
    ]
    for title, desc in valid_jobs:
        res = evaluate_offer(title=title, description=desc)
        assert res.eligible is True, f"Falló aprobación de oferta TI: {title}"
        assert res.decision == "ELIGIBLE"


def test_cv_selector_picks_audited_cvs_and_fast_hire_salary() -> None:
    sel_dotnet = select_cv_and_salary(
        title="Desarrollador Backend .NET C# SQL Server",
        description="APIs con ASP.NET Core",
        salary_strategy="FAST_HIRE",
    )
    assert sel_dotnet.cv_base == "CV_Candidate_Backend_DotNet_CSharp"
    assert sel_dotnet.src_pdf.exists()
    assert sel_dotnet.src_docx.exists()
    assert sel_dotnet.salary_clp == 850000

    sel_support = select_cv_and_salary(
        title="Técnico de Soporte TI Mesa de Ayuda N1",
        description="Atención de tickets y Windows",
        salary_strategy="FAST_HIRE",
    )
    assert sel_support.cv_base == "CV_Candidate_Support_IT"
    assert sel_support.src_pdf.exists()
    assert sel_support.salary_clp == 750000

    sel_part_time = select_cv_and_salary(
        title="Soporte Técnico Informático Part Time",
        description="Media jornada en Santiago",
        salary_strategy="FAST_HIRE",
    )
    assert sel_part_time.salary_clp == 700000

    # Prueba de garantía 100%: 'negocios' no debe activar 'ios' (Mobile)
    sel_bdm = select_cv_and_salary(
        title="Desarrollador de Negocios (BDM)",
        description="Empresa de rubro de tecnología y venta de servicios.",
    )
    assert sel_bdm.cv_base == "CV_Candidate_Software_Engineer"
    assert "mobile" not in sel_bdm.cv_base.lower()

    # Prueba de coincidencia real de iOS / Mobile
    sel_ios = select_cv_and_salary(
        title="Desarrollador iOS Swift",
        description="Desarrollo de aplicaciones móviles iOS en Swift.",
    )
    assert sel_ios.cv_base == "CV_Candidate_Mobile_Apps"


def test_fact_checker_blocks_hallucinated_experience() -> None:
    valid_res = validate_text_truthfulness(
        "Soy Ingeniero en Informática titulado en Universidad Tecnológica Demo con 2 años de experiencia profesional en Acme Cloud y DataFlow Labs."
    )
    assert valid_res.valid is True

    invalid_years = validate_text_truthfulness(
        "Cuento con 5 años de experiencia liderando equipos de arquitectura cloud."
    )
    assert invalid_years.valid is False
    assert any("5 años" in v for v in invalid_years.violations)

    invalid_cert = validate_text_truthfulness(
        "Poseo certificación oficial PMP y AWS Certified Solutions Architect."
    )
    assert invalid_cert.valid is False


def test_deterministic_answer_generator() -> None:
    questions = [
        {"field_name": "q2", "label": "Indique sus pretensiones de renta líquida"},
        {"field_name": "q3", "label": "¿Cuál es su disponibilidad para comenzar?"},
        {"field_name": "q4", "label": "Comente su experiencia en soporte técnico y redes"},
        {"field_name": "q5", "label": "¿En qué comuna o ciudad reside actualmente?"},
    ]
    gen = prepare_application_answers(
        company="Empresa TI",
        title="Analista Soporte TI",
        salary_clp=750000,
        questions=questions,
        location_hint="Santiago",
    )
    assert len(gen.unanswered_questions) == 0
    assert "$750.000" in gen.answers_by_field["q2"]
    assert "inmediata" in gen.answers_by_field["q3"].lower()
    assert "Universidad Tecnológica Demo" in gen.answers_by_field["q4"]

    # Pruebas para preguntas compuestas y herramientas de ticketing
    ans_sn = answer_question_deterministically(
        "¿Cuántos años de experiencia tienes gestionando incidentes y tickets en ServiceNow?",
        750000,
    )
    assert "ServiceNow" in ans_sn
    assert "2 años" in ans_sn

    ans_comp = answer_question_deterministically(
        "¿Tienes licencia de conducir clase B? Indícame tus pretensiones de renta",
        750000,
    )
    assert "no cuento con licencia" in ans_comp.lower()
    assert "$750.000" in ans_comp
    assert "Santiago" in gen.answers_by_field["q5"]


def test_programmer_it_token_disambiguation_and_canned_template_elimination() -> None:
    from job_automator.intelligence.ai_client import synthesize_ai_answer_dynamic

    # 1. 'para ti' en la descripción no debe validar un cargo no-TI con 'programador'
    res_logistics = evaluate_offer(
        title="Programador de Flota y Rutas",
        description="Programación de despachos y logística. ¡Este puesto es para ti!",
    )
    assert res_logistics.eligible is False
    assert res_logistics.decision == "REJECTED_FILTER"

    res_bare = evaluate_offer(
        title="Programador",
        description="Empresa de producción busca programador. ¡Este empleo es para ti!",
    )
    assert res_bare.eligible is False
    assert res_bare.decision == "REJECTED_FILTER"

    # Cargo TI legítimo con 'Programador TI' debe ser ELIGIBLE
    res_ti = evaluate_offer(
        title="Programador TI",
        description="Soporte y desarrollo de sistemas informáticos.",
    )
    assert res_ti.eligible is True
    assert res_ti.decision == "ELIGIBLE"

    # 2. Eliminación de clichés rígidos de 'Clean Architecture'
    ans_net = answer_question_deterministically("¿Cuántos años de experiencia tienes con .NET?", 850000)
    assert "Clean Architecture" not in ans_net
    assert "FinanceCore" not in ans_net
    assert "2 años" in ans_net

    dyn_unknown = synthesize_ai_answer_dynamic(
        question_text="¿Tienes experiencia en Salesforce y Apex?",
        offer_title="Desarrollador Salesforce",
        company="Tech Corp",
    )
    assert "Clean Architecture" not in dyn_unknown
    assert "Acme Cloud" in dyn_unknown

