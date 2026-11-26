"""Pruebas unitarias para el flujo especializado de Chiletrabajos (ChiletrabajosFlow)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from job_automator.browser.guards import verify_post_submit_confirmation
from job_automator.db.repository import JobRepository
from job_automator.flows.chiletrabajos_flow import (
    ChiletrabajosFlow,
    build_enhanced_cover_letter,
    fill_chiletrabajos_form,
    resolve_chiletrabajos_postular_url,
)
from job_automator.intelligence.ats_cv_generator import ATSCVResult
from job_automator.intelligence.fact_checker import validate_text_truthfulness


def test_chiletrabajos_flow_properties():
    flow = ChiletrabajosFlow()
    assert flow.portal_name == "chiletrabajos"


def test_build_enhanced_cover_letter():
    letter_py = build_enhanced_cover_letter("TechCorp", "Desarrollador Python Backend", 800000)
    assert "Universidad Tecnológica Demo" in letter_py
    assert "2 años de experiencia" in letter_py
    assert "Python" in letter_py
    chk = validate_text_truthfulness(letter_py)
    assert chk.valid is True

    letter_net = build_enhanced_cover_letter("Soluciones .NET", "Desarrollador C# .NET", 850000)
    assert ".NET" in letter_net or "C#" in letter_net
    assert validate_text_truthfulness(letter_net).valid is True

    letter_ai = build_enhanced_cover_letter("AI Labs", "Ingeniero de IA y LLMs", 900000)
    assert "IA" in letter_ai or "modelos de lenguaje" in letter_ai
    assert validate_text_truthfulness(letter_ai).valid is True


@pytest.mark.asyncio
async def test_chiletrabajos_simulate(tmp_path: Path):
    db_path = tmp_path / "test_jobs.db"
    repo = JobRepository(db_path=db_path)
    dossier_dir = tmp_path / "dossiers"

    repo.create_session("ses_test_ct", mode="DRY_RUN")
    _, app_id = repo.register_offer_and_application(
        portal="chiletrabajos",
        url="https://www.chiletrabajos.cl/trabajo/postular/1899999",
        company_name="Empresa Software SpA",
        title="Desarrollador Full Stack Python React",
        session_id="ses_test_ct",
        initial_state="READY_TO_APPLY",
        description_text="Buscamos Desarrollador Full Stack con conocimientos en Python, React y APIs REST.",
        external_job_id="1899999",
    )

    offer = repo.get_application(app_id)
    assert offer is not None

    flow = ChiletrabajosFlow()
    mock_page = AsyncMock()

    res = await flow.simulate(mock_page, offer, repo, dossier_dir)
    assert res["ok"] is True
    assert res["status"] == "SIMULATED_READY"
    assert "custom_ats_cv_path" in res
    assert Path(res["custom_ats_cv_path"]).exists()

    updated = repo.get_application(app_id)
    assert updated["state"] == "SIMULATED_READY"


@pytest.mark.asyncio
async def test_chiletrabajos_simulate_strict_stop_on_ats_failure(tmp_path: Path):
    db_path = tmp_path / "test_jobs.db"
    repo = JobRepository(db_path=db_path)
    dossier_dir = tmp_path / "dossiers"

    repo.create_session("ses_fail", mode="DRY_RUN")
    _, app_id = repo.register_offer_and_application(
        portal="chiletrabajos",
        url="https://www.chiletrabajos.cl/trabajo/postular/1899998",
        company_name="Empresa Fallo",
        title="Desarrollador Backend",
        session_id="ses_fail",
        initial_state="READY_TO_APPLY",
        description_text="Oferta de prueba para fallo.",
        external_job_id="1899998",
    )
    offer = repo.get_application(app_id)

    flow = ChiletrabajosFlow()
    mock_page = AsyncMock()

    with patch(
        "job_automator.flows.chiletrabajos_flow.async_generate_ats_cv_for_job",
        new_callable=AsyncMock,
        return_value=ATSCVResult(
            ok=False,
            pdf_path=None,
            ats_score=0,
            matched_keywords=[],
            tailored_summary="",
            selected_projects=[],
            error="Simulated Chromium timeout",
        ),
    ):
        res = await flow.simulate(mock_page, offer, repo, dossier_dir)
        assert res["ok"] is False
        assert res["status"] == "ERROR_CV_GENERATION"

        updated = repo.get_application(app_id)
        assert updated["state"] == "FAILED_RETRYABLE"
        assert "Detención estricta" in res["message"]


@pytest.mark.asyncio
async def test_fill_chiletrabajos_form_helper(tmp_path: Path):
    mock_page = AsyncMock()

    mock_carta = AsyncMock()
    mock_salary = AsyncMock()
    mock_dispo = AsyncMock()
    mock_cv = AsyncMock()
    mock_input_q1 = AsyncMock()

    async def query_selector_side_effect(sel):
        if "#carta" in sel:
            return mock_carta
        if 'name="salary"' in sel:
            return mock_salary
        if "#dispoIn" in sel:
            return mock_dispo
        if "#cv" in sel or 'input[type="file"]' in sel:
            return mock_cv
        if "#pregunta_1" in sel:
            return mock_input_q1
        return None

    mock_page.query_selector = AsyncMock(side_effect=query_selector_side_effect)
    mock_page.query_selector_all = AsyncMock(return_value=[])
    mock_page.eval_on_selector_all = AsyncMock(
        return_value=[
            {"field_name": "pregunta_1", "label": "¿Cuántos años de experiencia tienes en desarrollo web?"}
        ]
    )
    mock_page.evaluate = AsyncMock(return_value=True)

    test_pdf = tmp_path / "CV_test.pdf"
    test_pdf.write_bytes(b"%PDF-1.4 test")

    answers = await fill_chiletrabajos_form(
        page=mock_page,
        cover_letter="Estimado equipo...",
        salary_clp=800000,
        ats_pdf_path=test_pdf,
        precomputed_answers={},
        offer_title="Desarrollador Web",
        company="Empresa Demo",
        offer_desc="Requerimos desarrollador con experiencia web.",
    )

    mock_carta.fill.assert_awaited_once_with("Estimado equipo...")
    mock_salary.fill.assert_awaited_once_with("800000")
    mock_dispo.check.assert_awaited_once()
    mock_cv.set_input_files.assert_awaited_once_with(str(test_pdf))
    assert len(answers) == 1
    assert "¿Cuántos años de experiencia tienes en desarrollo web?" in answers
    mock_input_q1.fill.assert_awaited_once()


def test_post_submit_confirmation_chiletrabajos():
    # 1. Caso exitoso
    res_ok = verify_post_submit_confirmation(
        "chiletrabajos",
        "https://www.chiletrabajos.cl/trabajo/postular/1899999",
        "¡Tu postulación ha sido enviada con éxito!",
    )
    assert res_ok.ok is True
    assert res_ok.status_code == "CONFIRMED"

    # 2. Caso ya postulado
    res_already = verify_post_submit_confirmation(
        "chiletrabajos",
        "https://www.chiletrabajos.cl/trabajo/postular/1899999",
        "Ya te has postulado a este aviso anteriormente.",
    )
    assert res_already.ok is True
    assert res_already.status_code == "CONFIRMED"

    # 3. Caso error de validación
    res_err = verify_post_submit_confirmation(
        "chiletrabajos",
        "https://www.chiletrabajos.cl/trabajo/postular/1899999",
        "El campo carta de presentación es requerido.",
    )
    assert res_err.ok is False
    assert res_err.status_code == "VALIDATION_ERROR"

    # 4. Caso redirección a login
    res_auth = verify_post_submit_confirmation(
        "chiletrabajos",
        "https://www.chiletrabajos.cl/chtlogin",
        "Inicia sesión para continuar",
    )
    assert res_auth.ok is False
    assert res_auth.status_code == "AUTH_EXPIRED"

    # 5. Caso no concluyente
    res_unc = verify_post_submit_confirmation(
        "chiletrabajos",
        "https://www.chiletrabajos.cl/trabajo/postular/1899999",
        "Texto cualquiera sin indicadores de envío",
    )
    assert res_unc.ok is False
    assert res_unc.status_code == "UNCERTAIN"


def test_queue_filtering_rejects_non_ti_offer_for_chiletrabajos(tmp_path: Path):
    from job_automator.intelligence.evaluator import evaluate_offer

    db_path = tmp_path / "test_filter_jobs.db"
    repo = JobRepository(db_path=db_path)

    _, id_ti = repo.register_offer_and_application(
        portal="chiletrabajos",
        url="https://www.chiletrabajos.cl/trabajo/postular/22222",
        company_name="Tech Solutions",
        title="Desarrollador Backend Python FastAPI",
        initial_state="READY_TO_APPLY",
        description_text="Se busca desarrollador Python con experiencia en APIs REST y bases de datos.",
        external_job_id="22222",
    )

    _, id_non_ti = repo.register_offer_and_application(
        portal="chiletrabajos",
        url="https://www.chiletrabajos.cl/trabajo/chofer-camion-11111",
        company_name="Minera Norte",
        title="Chofer de Camión Tolva",
        initial_state="READY_TO_APPLY",
        description_text="Se busca chofer con licencia A4 para faena minera.",
    )

    eval_ti = evaluate_offer(
        title="Desarrollador Backend Python FastAPI",
        description="Se busca desarrollador Python con experiencia en APIs REST y bases de datos.",
    )
    eval_non_ti = evaluate_offer(
        title="Chofer de Camión Tolva",
        description="Se busca chofer con licencia A4 para faena minera.",
    )
    assert eval_ti.eligible is True
    assert eval_non_ti.eligible is False

    repo.transition_application_state(id_non_ti, "SKIPPED_FILTER", reason=eval_non_ti.reason)
    assert repo.get_application(id_ti)["state"] == "READY_TO_APPLY"
    assert repo.get_application(id_non_ti)["state"] == "SKIPPED_FILTER"


@pytest.mark.asyncio
async def test_interactive_assistant_handles_missing_app_and_unavailable_cdp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import job_automator.worker.interactive_assistant as ia_mod

    db_path = tmp_path / "test_cdp_fallback.db"
    repo = JobRepository(db_path=db_path)
    monkeypatch.setattr(ia_mod, "JobRepository", lambda: repo)
    monkeypatch.setattr(
        ia_mod,
        "discover_cdp_endpoint",
        lambda: MagicMock(available=False, ws_url=None),
    )

    missing_res = await ia_mod.prepare_offer_in_browser(999999)
    assert missing_res["ok"] is False

    _, app_id = repo.register_offer_and_application(
        portal="chiletrabajos",
        url="https://www.chiletrabajos.cl/trabajo/postular/33333",
        company_name="Fintech Chile",
        title="Ingeniero de Software .NET",
        initial_state="READY_TO_APPLY",
    )
    no_cdp_res = await ia_mod.prepare_offer_in_browser(app_id)
    assert no_cdp_res["ok"] is False
    assert "Chrome" in no_cdp_res["error"]


def test_resolve_chiletrabajos_postular_url():
    # 1. URL estándar de vacante
    u1 = "https://www.chiletrabajos.cl/trabajo/desarrollador-fullstack-python-1854321"
    assert resolve_chiletrabajos_postular_url(u1) == "https://www.chiletrabajos.cl/trabajo/postular/1854321"

    # 2. URL ya contiene /postular/
    u2 = "https://www.chiletrabajos.cl/trabajo/postular/1854321"
    assert resolve_chiletrabajos_postular_url(u2) == u2

    # 3. Fallback con external_job_id si no hay dígitos en URL
    u3 = "https://www.chiletrabajos.cl/trabajo/desarrollador"
    assert resolve_chiletrabajos_postular_url(u3, external_job_id="998877") == "https://www.chiletrabajos.cl/trabajo/postular/998877"

    # 4. Cadena vacía
    assert resolve_chiletrabajos_postular_url("") == ""


@pytest.mark.asyncio
async def test_fill_chiletrabajos_form_edge_cases(tmp_path: Path):
    mock_page = AsyncMock()
    mock_carta = AsyncMock()
    mock_salary = AsyncMock()
    mock_dispo = AsyncMock()
    mock_cv = AsyncMock()

    # Simular elemento que falla al hacer .fill (ej. elemento de solo lectura o modificado)
    mock_failing_inp = AsyncMock()
    mock_failing_inp.fill = AsyncMock(side_effect=Exception("Playwright fill error on read-only"))

    async def query_selector_side_effect(sel):
        if "#carta" in sel:
            return mock_carta
        if 'name="salary"' in sel:
            return mock_salary
        if "#dispoIn" in sel:
            return mock_dispo
        if "#cv" in sel:
            return mock_cv
        if "#failing_q" in sel:
            return mock_failing_inp
        return None

    mock_page.query_selector = AsyncMock(side_effect=query_selector_side_effect)
    mock_page.query_selector_all = AsyncMock(return_value=[])
    mock_page.eval_on_selector_all = AsyncMock(
        return_value=[
            {"field_name": "failing_q", "label": "Pregunta conflictiva"}
        ]
    )
    mock_page.evaluate = AsyncMock(return_value=True)

    # 1. ats_pdf_path como string
    test_pdf = tmp_path / "CV_string_path.pdf"
    test_pdf.write_bytes(b"%PDF-1.4 test")

    answers = await fill_chiletrabajos_form(
        page=mock_page,
        cover_letter="Carta...",
        salary_clp=800000,
        ats_pdf_path=str(test_pdf),  # str en lugar de Path
        precomputed_answers={},
        offer_title="Dev",
        company="Co",
    )
    # Debe haber ejecutado sin lanzar excepción
    mock_carta.fill.assert_awaited()
    mock_salary.fill.assert_awaited()
    mock_dispo.check.assert_awaited()
    mock_cv.set_input_files.assert_awaited_once_with(str(test_pdf))
    # El campo con fallo no debe haber tumbado la ejecución
    assert isinstance(answers, dict)


@pytest.mark.asyncio
async def test_chiletrabajos_postulate_assisted_success(tmp_path: Path):
    db_path = tmp_path / "test_jobs.db"
    repo = JobRepository(db_path=db_path)
    repo.create_session("ses_postula", mode="LIVE")

    dossier = tmp_path / "dossier_postula"
    dossier.mkdir()

    _, app_id = repo.register_offer_and_application(
        portal="chiletrabajos",
        url="https://www.chiletrabajos.cl/trabajo/desarrollador-fullstack-1900001",
        company_name="Innovatech",
        title="Desarrollador Full Stack",
        session_id="ses_postula",
        initial_state="READY_TO_APPLY",
        description_text="Puesto Full Stack Python",
        external_job_id="1900001",
    )
    offer = repo.get_application(app_id)
    flow = ChiletrabajosFlow()

    mock_page = AsyncMock()
    mock_page.url = "https://www.chiletrabajos.cl/trabajo/postular/1900001"
    mock_page.is_closed = MagicMock(return_value=False)
    mock_page.inner_text = AsyncMock(return_value="¡Tu postulación ha sido enviada con éxito!")
    mock_page.query_selector = AsyncMock(return_value=None)
    mock_page.eval_on_selector_all = AsyncMock(return_value=[])
    mock_page.evaluate = AsyncMock(return_value=True)

    with patch("job_automator.flows.chiletrabajos_flow.safe_page_screenshot", new_callable=AsyncMock, return_value=True), \
         patch("job_automator.flows.chiletrabajos_flow.build_consolidated_report", return_value={"summary": {"confirmed": 1}}):
        res = await flow.postulate_assisted(
            page=mock_page,
            offer=offer,
            repo=repo,
            current_idx=1,
            total_offers=1,
            max_wait_seconds=5,
        )
        assert res["ok"] is True
        assert res["status"] == "CONFIRMED"
        updated = repo.get_application(app_id)
        assert updated["state"] == "CONFIRMED"


@pytest.mark.asyncio
async def test_chiletrabajos_postulate_assisted_page_closed_early(tmp_path: Path):
    db_path = tmp_path / "test_jobs.db"
    repo = JobRepository(db_path=db_path)
    repo.create_session("ses_postula_closed", mode="LIVE")

    _, app_id = repo.register_offer_and_application(
        portal="chiletrabajos",
        url="https://www.chiletrabajos.cl/trabajo/postular/1900002",
        company_name="TechCorp",
        title="Desarrollador Backend",
        session_id="ses_postula_closed",
        initial_state="READY_TO_APPLY",
        description_text="Puesto Backend",
        external_job_id="1900002",
    )
    offer = repo.get_application(app_id)
    flow = ChiletrabajosFlow()

    mock_page = AsyncMock()
    mock_page.url = "https://www.chiletrabajos.cl/trabajo/postular/1900002"
    mock_page.is_closed = MagicMock(return_value=True)  # Pestaña cerrada por el usuario
    mock_page.inner_text = AsyncMock(return_value="")
    mock_page.query_selector = AsyncMock(return_value=None)
    mock_page.eval_on_selector_all = AsyncMock(return_value=[])
    mock_page.evaluate = AsyncMock(return_value=True)

    with patch("job_automator.flows.chiletrabajos_flow.safe_page_screenshot", new_callable=AsyncMock, return_value=True):
        res = await flow.postulate_assisted(
            page=mock_page,
            offer=offer,
            repo=repo,
            current_idx=1,
            total_offers=1,
            max_wait_seconds=2,
        )
        assert res["ok"] is False
        assert res["status"] == "TIMEOUT_WAITING_USER_SUBMIT"


