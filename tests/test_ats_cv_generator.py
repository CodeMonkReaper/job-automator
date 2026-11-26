"""Pruebas unitarias para el generador de CV personalizado ATS (ats_cv_generator.py)."""

from __future__ import annotations

import re
from pathlib import Path
import pytest
import pymupdf

from job_automator.intelligence.ats_cv_generator import (
    ATSCVGenerationError,
    ATSCVGenerator,
    ATSCVResult,
    TruthfulnessViolationError,
    async_generate_ats_cv_for_job,
    calculate_ats_affinity_score,
    extract_ats_keywords,
    generate_ats_cv_for_job,
    get_ats_cv_filename,
    render_html,
    synthesize_tailored_cv_data,
)


def test_keyword_extraction_and_ats_score():
    """Test 1: Extracción de palabras clave técnicas y cálculo de afinidad ATS."""
    title = "Desarrollador Full Stack .NET C# y React"
    desc = (
        "Requerimos Ingeniero de Software con conocimientos en .NET 8, C#, PostgreSQL, "
        "Docker Compose, CI/CD, Clean Architecture y APIs RESTful."
    )

    keywords = extract_ats_keywords(title, desc)
    assert ".NET" in keywords
    assert "C#" in keywords
    assert "React" in keywords
    assert "PostgreSQL" in keywords
    assert "Docker" in keywords
    assert "CI/CD" in keywords
    assert "Clean Architecture" in keywords

    score = calculate_ats_affinity_score(title, desc, keywords)
    assert 85 <= score <= 98


def test_truthfulness_validation_blocks_forbidden_claims():
    """Test 2: Verificación estricta de veracidad impidiendo afirmaciones falsas o exageradas."""
    gen = ATSCVGenerator()
    cv_data = gen.synthesize_tailored_data("Desarrollador Python", "Experiencia en desarrollo")

    # Inyectar afirmación prohibida de años de experiencia (> 2 años)
    cv_data["summary"] = "Cuento con 4 años de experiencia profesional en arquitectura de software."
    with pytest.raises(TruthfulnessViolationError) as exc_info:
        gen._validate_truthfulness(cv_data)
    assert "superando el máximo real comprobado" in str(exc_info.value) or "Afirmación no autorizada" in str(exc_info.value)

    # Inyectar certificación prohibida
    cv_data["summary"] = "Ingeniero con 2 años de experiencia y certificación AWS Certified Solutions Architect."
    with pytest.raises(TruthfulnessViolationError) as exc_info_cert:
        gen._validate_truthfulness(cv_data)
    assert "AWS Certified Solutions Architect" in str(exc_info_cert.value)

    # Inyectar licencia de conducir falsa
    cv_data["summary"] = "Ingeniero en Informática con licencia clase b al día y 2 años de experiencia."
    with pytest.raises(TruthfulnessViolationError) as exc_info_lic:
        gen._validate_truthfulness(cv_data)
    assert "licencia" in str(exc_info_lic.value).lower()


def test_synthesized_data_matches_candidate_profile():
    """Test 3: La síntesis de datos respeta estrictamente los antecedentes verificados del perfil."""
    title = "Desarrollador Backend Java y Spring Boot"
    desc = "Buscamos Ingeniero para sistemas empresariales con Java, Spring Boot y microservicios."

    cv_data = synthesize_tailored_cv_data(title, desc)
    assert cv_data["full_name"] == "Alex Rivera"
    assert cv_data["education"]["degree"] == "Ingeniero en Informática"
    assert cv_data["education"]["institution"] == "Universidad Tecnológica Demo"

    # Verificar que solo aparezcan las empresas autorizadas
    companies = [exp["company"] for exp in cv_data["experiences"]]
    assert "Acme Cloud" in companies
    assert "DataFlow Labs" in companies
    assert any("WebShop Demo" in c for c in companies)
    assert len(companies) == 3

    # Verificar que el resumen menciona 2 años de experiencia y Universidad Tecnológica Demo
    summary = cv_data["summary"]
    assert "Universidad Tecnológica Demo" in summary
    assert "2 años" in summary
    assert "Santiago" in summary


def test_pdf_generation_single_page_vector_text(tmp_path: Path):
    """Test 4: Renderizado de PDF de exactamente 1 página con capa de texto plana vectorial limpia."""
    output_pdf = tmp_path / "CV_Alex_Rivera_Test_ATS.pdf"

    res = generate_ats_cv_for_job(
        job_title="Desarrollador Full Stack Python y TypeScript",
        job_description="Desarrollo de microservicios con Python, FastAPI, TypeScript, PostgreSQL y Docker.",
        output_path=output_pdf,
        company_name="Empresa Test",
        app_id=999,
    )

    assert res.ok is True
    assert res.pdf_path is not None
    assert res.pdf_path.exists()
    assert res.pdf_path.stat().st_size > 5000

    # Abrir con PyMuPDF y verificar página única y texto seleccionable
    doc = pymupdf.open(str(res.pdf_path))
    assert len(doc) == 1, f"El CV ATS debe tener exactamente 1 página, pero tiene {len(doc)}"

    text = doc[0].get_text()
    assert "Alex Rivera" in text
    assert "Universidad Tecnológica Demo" in text
    assert "Acme Cloud" in text
    assert "DataFlow Labs" in text
    assert "WebShop Demo" in text
    assert "Santiago" in text
    assert len(text) > 1000


@pytest.mark.asyncio
async def test_async_pdf_generation(tmp_path: Path):
    """Test 5: Orquestación asincrónica del generador de CV ATS."""
    output_pdf = tmp_path / "CV_Alex_Rivera_Async_Test.pdf"

    res = await async_generate_ats_cv_for_job(
        job_title="Ingeniero de Software .NET / C#",
        job_description="Desarrollo en .NET 8, C#, Clean Architecture, Domain-Driven Design y SQL Server.",
        output_path=output_pdf,
        company_name="Empresa Async",
        app_id=1001,
    )

    assert res.ok is True
    assert res.pdf_path is not None
    assert res.pdf_path.exists()
    assert res.ats_score >= 80
    assert any(k in res.matched_keywords for k in ("C#", ".NET", "Clean Architecture", "SQL Server"))

    doc = pymupdf.open(str(res.pdf_path))
    assert len(doc) == 1
    text = doc[0].get_text()
    assert "FinanceCore" in text
    assert "Clean Architecture" in text


def test_ats_generator_strict_error_handling(tmp_path: Path):
    """Test 6: Manejo estricto de errores ante rutas inválidas o excepciones."""
    # Usar una ruta cuyo padre es un archivo regular para forzar error multiplataforma
    blocker = tmp_path / "not_a_directory"
    blocker.write_text("blocker", encoding="utf-8")
    invalid_path = blocker / "cv.pdf"

    res = generate_ats_cv_for_job(
        job_title="Cargo Test",
        job_description="Descripción Test",
        output_path=invalid_path,
        company_name="Empresa Error",
    )

    assert res.ok is False
    assert res.pdf_path is None
    assert res.error is not None
    assert "Error" in res.error or "Fallo" in res.error


def test_extreme_job_descriptions_stay_single_page(tmp_path: Path):
    """Test 7: Comprobar que descripciones extensas y títulos largos se ajustan a exactamente 1 página."""
    long_title = "Ingeniero de Software Senior Full Stack Python, Django, React, TypeScript, Cloud AWS, Microservicios y DevOps"
    long_desc = (
        "Liderar equipos de desarrollo, diseñar microservicios, optimizar bases de datos postgresql y oracle, "
        "implementar pipelines de CI/CD con GitHub Actions y GitLab, unit testing con pytest y jest, arquitectura DDD, "
        "patrones de diseño, cloud computing, soporte n3 y continuidad operativa 24/7. Requisitos: ingenieria en informatica, "
        "2 años de experiencia, docker, kubernetes, redis, apis rest, django, react, next.js, nestjs, bash, linux ubuntu."
    )
    out_pdf = tmp_path / "extreme_single_page.pdf"
    res = generate_ats_cv_for_job(long_title, long_desc, output_path=out_pdf, company_name="Empresa Extrema")
    assert res.ok is True
    assert res.pdf_path is not None and res.pdf_path.exists()

    doc = pymupdf.open(str(res.pdf_path))
    assert len(doc) == 1, f"El CV superó 1 página: {len(doc)} páginas"
    doc.close()


@pytest.mark.asyncio
async def test_simulate_records_sqlite_and_stops_on_ats_failure(tmp_path: Path):
    """Test 8: Validación de persistencia en SQLite de folder_path y detención estricta en simulate."""
    from unittest.mock import AsyncMock, patch
    from job_automator.db.repository import JobRepository
    from job_automator.flows.computrabajo_flow import ComputrabajoFlow

    db_path = tmp_path / "test_ats_jobs.db"
    repo = JobRepository(db_path=db_path)
    dossier_dir = tmp_path / "dossiers"

    repo.create_session("ses_ats_test", mode="DRY_RUN")
    _, app_id = repo.register_offer_and_application(
        portal="computrabajo",
        url="https://cl.computrabajo.com/ofertas/test-ats-1",
        company_name="Empresa ATS Test",
        title="Desarrollador Full Stack ATS",
        session_id="ses_ats_test",
        initial_state="READY_TO_APPLY",
        description_text="Requerimos Python, React, PostgreSQL y Docker.",
    )

    offer = repo.get_application(app_id)
    assert offer is not None

    flow = ComputrabajoFlow()
    mock_page = AsyncMock()

    # 1. Simulación exitosa -> verifica que SQLite guarda folder_path y selected_cv_target
    res = await flow.simulate(mock_page, offer, repo, dossier_dir)
    assert res["ok"] is True
    assert res["status"] == "SIMULATED_READY"
    assert res["ats_score"] > 70
    assert "Python" in res["matched_keywords"]

    updated_app = repo.get_application(app_id)
    assert updated_app["folder_path"] is not None
    assert "Empresa ATS Test" in updated_app["folder_name"]
    assert "CV_Alex_Rivera_ATS" in updated_app["selected_cv_target"]

    # 2. Simulación con fallo de ATS -> detención estricta con FAILED_RETRYABLE
    with patch("job_automator.flows.computrabajo_flow.async_generate_ats_cv_for_job") as mock_gen:
        mock_gen.return_value = ATSCVResult(
            ok=False,
            pdf_path=None,
            ats_score=0,
            matched_keywords=[],
            tailored_summary="",
            selected_projects=[],
            error="Simulated Truthfulness Violation",
        )
        fail_res = await flow.simulate(mock_page, offer, repo, dossier_dir)
        assert fail_res["ok"] is False
        assert fail_res["status"] == "ERROR_CV_GENERATION"

        failed_app = repo.get_application(app_id)
        assert failed_app["state"] == "FAILED_RETRYABLE"


def test_ats_cv_filename_and_header_includes_app_id(tmp_path: Path):
    """Test 9: Verificación de que el nombre del PDF y el encabezado incluyan el ID de la postulación."""
    # 1. Función auxiliar get_ats_cv_filename
    assert get_ats_cv_filename(None) == "CV_Candidate_ATS.pdf"
    assert get_ats_cv_filename(1529) == "CV_Candidate_ATS_ID1529.pdf"
    assert get_ats_cv_filename(1529, "Alex Rivera") == "CV_Alex_Rivera_ATS_ID1529.pdf"

    # 2. Generación automática con app_id
    res = generate_ats_cv_for_job(
        job_title="Desarrollador Python Backend",
        job_description="Microservicios en Python con FastAPI y PostgreSQL.",
        output_path=tmp_path,
        company_name="Empresa Test ID",
        app_id=1529,
    )

    assert res.ok is True
    assert res.pdf_path is not None
    assert res.pdf_path.name == "CV_Alex_Rivera_ATS_ID1529.pdf"
    assert res.pdf_path.exists()

    # 3. Comprobar que en el texto vectorial renderizado aparece ID: #1529
    doc = pymupdf.open(str(res.pdf_path))
    assert doc.page_count == 1
    page_text = doc[0].get_text()
    assert "ID: #1529" in page_text
    doc.close()


