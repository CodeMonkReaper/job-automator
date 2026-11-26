"""Pruebas de herramientas del Servidor MCP y control de sesiones."""

from __future__ import annotations

from pathlib import Path
import pytest

from job_automator.db.repository import JobRepository
import job_automator.mcp_server as mcp_mod


def test_mcp_session_lifecycle_and_ai_answer_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    test_db = tmp_path / "mcp_test.db"
    monkeypatch.setattr(mcp_mod, "JobRepository", lambda: JobRepository(db_path=test_db))

    repo = JobRepository(db_path=test_db)
    repo.create_session("ses_test_01", mode="DRY_RUN")
    _, app_id = repo.register_offer_and_application(
        portal="chiletrabajos",
        url="https://www.chiletrabajos.cl/trabajo/postular/7777777",
        company_name="Consultora Test",
        title="Ingeniero de Software",
        session_id="ses_test_01",
        initial_state="NEEDS_HUMAN_REVIEW",
    )

    # 1. Consultar estado ('estado')
    st = mcp_mod.get_session_status("ses_test_01")
    assert st["ok"] is True
    assert st["session_id"] == "ses_test_01"
    assert st["session_counts_by_state"]["NEEDS_HUMAN_REVIEW"] == 1
    assert "portal_readiness" in st
    assert st["portal_readiness"]["chiletrabajos"]["status"] == "OPERATIONAL"
    assert st["portal_readiness"]["computrabajo"]["status"] in ("PENDING_DESIGN", "OPERATIONAL")

    # 2. Pausar sesión ('pausar')
    p_res = mcp_mod.pause_session("ses_test_01")
    assert p_res["ok"] is True
    assert p_res["new_status"] == "PAUSED"

    # 3. Intentar inyectar una respuesta que inventa 5 años de experiencia -> debe ser rechazada
    bad_ans = mcp_mod.submit_ai_answers_for_application(
        application_id=app_id,
        answers_by_question={"¿Años de experiencia?": "Tengo 5 años de experiencia como arquitecto."},
    )
    assert bad_ans["ok"] is False
    assert bad_ans["error"] == "FACT_CHECK_REJECTED"

    # 4. Inyectar una respuesta verídica -> debe aprobarse, guardarse en caché y pasar a READY_TO_APPLY
    good_ans = mcp_mod.submit_ai_answers_for_application(
        application_id=app_id,
        answers_by_question={
            "¿Experiencia con microservicios?": "Cuento con 2 años de experiencia desarrollando y manteniendo microservicios en Acme Cloud y ClinicGeo."
        },
    )
    assert good_ans["ok"] is True
    assert good_ans["new_state"] == "READY_TO_APPLY"

    # 5. Detener sesión ('detener')
    s_res = mcp_mod.stop_session("ses_test_01")
    assert s_res["ok"] is True
    assert s_res["status"] == "STOPPED"


def test_unsupported_portal_blocked_safeguard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifica que un portal no operativo (ej: Laborum) esté bloqueado de forma segura en el MCP."""
    test_db = tmp_path / "mcp_test_gate.db"
    monkeypatch.setattr(mcp_mod, "JobRepository", lambda: JobRepository(db_path=test_db))
    monkeypatch.setattr(mcp_mod, "migrate_legacy_workspace", lambda **kw: None)

    res = mcp_mod.start_job_session(mode="LIVE", portals=["laborum"])
    assert res["ok"] is False
    assert res["error"] == "PORTAL_NOT_OPERATIONAL"
    assert "laborum" in res["blocked_portals"]
    assert res["portal_readiness"]["laborum"]["status"] == "PENDING_ADAPTATION"


def test_investigate_portal_diagnostic() -> None:
    """Verifica que el comando de diagnóstico técnico de portal retorne estructura válida."""
    diag = mcp_mod.investigate_portal("computrabajo")
    assert "portal" in diag
    assert diag["portal"] == "computrabajo"
    assert "portal_readiness" in diag
    assert diag["portal_readiness"]["status"] in ("PENDING_DESIGN", "IN_INVESTIGATION", "OPERATIONAL")
    if diag.get("ok"):
        assert "diagnostics" in diag


@pytest.mark.asyncio
async def test_interactive_assistant_mcp_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifica las herramientas interactivas expuestas en el servidor MCP."""
    test_db = tmp_path / "interactive_test.db"
    import job_automator.worker.interactive_assistant as ia_mod
    monkeypatch.setattr(ia_mod, "JobRepository", lambda: JobRepository(db_path=test_db))
    monkeypatch.setattr(mcp_mod, "JobRepository", lambda: JobRepository(db_path=test_db))

    repo = JobRepository(db_path=test_db)
    _, app_id = repo.register_offer_and_application(
        portal="computrabajo",
        url="https://cl.computrabajo.com/ofertas-de-trabajo/oferta-desarrollador-backend-12345",
        company_name="Empresa Tech",
        title="Desarrollador Backend Python",
        description_text="Buscamos Desarrollador Backend con Python y SQL para desarrollo de microservicios.",
        killer_questions=[{"id": "q1", "text": "¿Cuántos años de experiencia tienes?"}],
        initial_state="NEEDS_HUMAN_REVIEW",
    )

    # 1. Inyectar respuestas mentirosas -> Rechazo Fact-Checker
    bad_res = await mcp_mod.inject_tailored_answers(
        app_id=app_id,
        answers_by_question={"¿Años?": "Tengo 8 años de experiencia liderando como Staff Architect."},
    )
    assert bad_res["ok"] is False
    assert bad_res["error"] == "FACT_CHECK_REJECTED"

    # 2. Inyectar respuestas verídicas a medida -> Aprobación y persistencia
    good_res = await mcp_mod.inject_tailored_answers(
        app_id=app_id,
        answers_by_question={
            "¿Experiencia en Python?": "Poseo 2 años de experiencia desarrollando microservicios backend en Acme Cloud y proyectos universitarios en Universidad Tecnológica Demo."
        },
    )
    assert good_res["ok"] is True
    assert good_res["new_state"] == "READY_TO_APPLY"

    app_rec = repo.get_application(app_id)
    assert app_rec is not None
    assert app_rec["state"] == "READY_TO_APPLY"
    import json
    ans_saved = json.loads(app_rec["killer_answers_json"])
    assert "¿Experiencia en Python?" in ans_saved

