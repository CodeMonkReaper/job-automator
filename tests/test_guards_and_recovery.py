"""Pruebas de las 5 Reglas de Oro (Guardias de Seguridad) y Recuperación de Errores."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

from job_automator.browser.guards import (
    run_preflight_check_on_page,
    safe_page_screenshot,
    verify_post_submit_confirmation,
    verify_pre_apply_offer_state,
    verify_session_state,
)
from job_automator.db.repository import JobRepository
from job_automator.worker.recovery import recover_orphaned_tasks, requeue_retryable_tasks


def test_session_guard_detects_expired_sessions() -> None:
    # Chiletrabajos redirigido a /preingreso
    v1 = verify_session_state(
        "chiletrabajos",
        "https://www.chiletrabajos.cl/preingreso?id=3917769",
        "Ingresa a tu cuenta o Crea cuenta",
    )
    assert v1.ok is False
    assert v1.status_code == "AUTH_EXPIRED"

    # Chiletrabajos con sesión activa
    v2 = verify_session_state(
        "chiletrabajos",
        "https://www.chiletrabajos.cl/encuentra-un-empleo",
        "Bienvenido Alex Rivera | Mi Cuenta | Salir",
    )
    assert v2.ok is True

    # Computrabajo redirigido a Login
    v3 = verify_session_state(
        "computrabajo",
        "https://secure.computrabajo.com/Account/Login",
        "Iniciar sesión",
    )
    assert v3.ok is False
    assert v3.status_code == "AUTH_EXPIRED"


def test_pre_apply_guard_detects_closed_already_applied_and_external() -> None:
    # Oferta ya postulada
    v_already = verify_pre_apply_offer_state(
        "chiletrabajos",
        "https://www.chiletrabajos.cl/trabajo/postular/3917769",
        "Aviso: Ya has postulado a este aviso el día 24-10-2026.",
        html_content='<textarea id="carta"></textarea><input name="salary">',
        expected_job_id="3917769",
    )
    assert v_already.ok is False
    assert v_already.status_code == "ALREADY_APPLIED"

    # Oferta cerrada / página no encontrada
    v_closed = verify_pre_apply_offer_state(
        "chiletrabajos",
        "https://www.chiletrabajos.cl/trabajo/postular/3917769",
        "Lo sentimos, PÁGINA NO ENCONTRADA. Este aviso ya no está disponible.",
        expected_job_id="3917769",
    )
    assert v_closed.ok is False
    assert v_closed.status_code == "CLOSED_OFFER"

    # Redirección externa a LinkedIn o ClaveÚnica
    v_ext = verify_pre_apply_offer_state(
        "chiletrabajos",
        "https://www.linkedin.com/login",
        "Welcome Back - Sign In",
    )
    assert v_ext.ok is False
    assert v_ext.status_code == "CAPTCHA_OR_EXTERNAL"


def test_post_submit_guard_blocks_false_positives() -> None:
    # 1. Error de validación en pantalla -> jamás debe ser CONFIRMED
    v_err = verify_post_submit_confirmation(
        "chiletrabajos",
        "https://www.chiletrabajos.cl/trabajo/postular/3917769",
        "Atención: El campo pretensión de renta es requerido.",
    )
    assert v_err.ok is False
    assert v_err.status_code == "VALIDATION_ERROR"

    # 2. Pantalla sin marcador claro -> SUBMITTED_UNVERIFIED (UNCERTAIN)
    v_unc = verify_post_submit_confirmation(
        "computrabajo",
        "https://cl.computrabajo.com/ofertas-de-trabajo/oferta-xyz",
        "Cargando información de la empresa...",
    )
    assert v_unc.ok is False
    assert v_unc.status_code == "UNCERTAIN"

    # 3. Marcador positivo real en Chiletrabajos -> CONFIRMED
    v_ok_ct = verify_post_submit_confirmation(
        "chiletrabajos",
        "https://www.chiletrabajos.cl/trabajo/3917769",
        "Estado: Postulando 2026-10-25. Ya has postulado a este aviso.",
    )
    assert v_ok_ct.ok is True
    assert v_ok_ct.status_code == "CONFIRMED"

    # 4. Marcador positivo real en Computrabajo -> CONFIRMED
    v_ok_cp = verify_post_submit_confirmation(
        "computrabajo",
        "https://candidato.cl.computrabajo.com/candidate/OnLineCv/KillerQuestions",
        "¡Te has postulado! Tu postulación fue enviada exitosamente a KMI SECURITY.",
    )
    assert v_ok_cp.ok is True
    assert v_ok_cp.status_code == "CONFIRMED"


def test_crash_recovery_protects_submitted_tasks_from_duplicate_send(tmp_path: Path) -> None:
    repo = JobRepository(db_path=tmp_path / "recovery.db")
    repo.create_session("ses_crash", mode="LIVE", worker_pid=999999)  # PID inexistente

    # Tarea 1: se interrumpió en FORM_FILL (antes de hacer clic en enviar)
    _, app_pre = repo.register_offer_and_application(
        portal="chiletrabajos",
        url="https://www.chiletrabajos.cl/trabajo/postular/1111111",
        company_name="Empresa A",
        title="Desarrollador Python",
        session_id="ses_crash",
        initial_state="READY_TO_APPLY",
    )
    repo.transition_application_state(app_pre, "APPLYING", last_step_reached="FORM_FILL", increment_attempt=True)

    # Tarea 2: se interrumpió justo después de SUBMIT_CLICK
    _, app_post = repo.register_offer_and_application(
        portal="chiletrabajos",
        url="https://www.chiletrabajos.cl/trabajo/postular/2222222",
        company_name="Empresa B",
        title="Desarrollador Java",
        session_id="ses_crash",
        initial_state="READY_TO_APPLY",
    )
    repo.transition_application_state(app_post, "APPLYING", last_step_reached="SUBMIT_CLICK", increment_attempt=True)

    rec = recover_orphaned_tasks(repo, stale_seconds=0)
    assert rec["recovered_to_ready"] == 1
    assert rec["moved_to_unverified"] == 1

    st_pre = repo.get_application(app_pre)
    st_post = repo.get_application(app_post)
    assert st_pre is not None and st_pre["state"] == "READY_TO_APPLY"
    assert st_post is not None and st_post["state"] == "SUBMITTED_UNVERIFIED"

    # Verificar que requeue_retryable_tasks NO reencola a ciegas la tarea en SUBMITTED_UNVERIFIED
    retry_res = requeue_retryable_tasks(repo, session_id="ses_crash")
    assert app_post in retry_res["unverified_ids_protected_from_blind_retry"]
    assert app_post not in retry_res["requeued_ids"]


@pytest.mark.asyncio
async def test_safe_page_screenshot_handles_closed_page_and_zero_viewport(tmp_path: Path) -> None:
    # 1. Closed page -> returns False safely
    mock_closed_page = MagicMock()
    mock_closed_page.is_closed.return_value = True
    assert await safe_page_screenshot(mock_closed_page, tmp_path / "closed.png") is False

    # 2. Open page with 0 viewport -> sets viewport to 1280x800 and takes screenshot
    mock_page = MagicMock()
    mock_page.is_closed.return_value = False
    mock_page.bring_to_front = AsyncMock()
    mock_page.viewport_size = {"width": 0, "height": 0}
    mock_page.set_viewport_size = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()
    mock_page.screenshot = AsyncMock()

    dest = tmp_path / "sub" / "shot.png"
    result = await safe_page_screenshot(mock_page, dest)
    assert result is True
    mock_page.set_viewport_size.assert_awaited_once_with({"width": 1280, "height": 800})
    mock_page.screenshot.assert_awaited_once_with(path=str(dest), timeout=5000)
    assert dest.parent.exists()


@pytest.mark.asyncio
async def test_preflight_preserves_active_offer_tab() -> None:
    # Si la pestaña ya está navegando Computrabajo, no forzar goto candidate/home
    mock_page = MagicMock()
    mock_page.url = "https://cl.computrabajo.com/ofertas-de-trabajo/oferta-desarrollador-12345"
    mock_page.inner_text = AsyncMock(return_value="Alex Rivera | Mi CV | Mis Postulaciones")
    mock_page.goto = AsyncMock()

    verdict = await run_preflight_check_on_page(mock_page, "computrabajo")
    assert verdict.ok is True
    mock_page.goto.assert_not_awaited()

