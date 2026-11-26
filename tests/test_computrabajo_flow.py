"""Pruebas unitarias para el nuevo flujo de Computrabajo (ComputrabajoFlow)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
import pytest

from job_automator.db.repository import JobRepository
from job_automator.flows.computrabajo_flow import ComputrabajoFlow, _fill_killer_questions_if_present


def test_computrabajo_flow_properties():
    flow = ComputrabajoFlow()
    assert flow.portal_name == "computrabajo"


@pytest.mark.asyncio
async def test_computrabajo_simulate(tmp_path: Path):
    db_path = tmp_path / "test_jobs.db"
    repo = JobRepository(db_path=db_path)
    dossier_dir = tmp_path / "dossiers"

    repo.create_session("ses_test", mode="DRY_RUN")
    _, app_id = repo.register_offer_and_application(
        portal="computrabajo",
        url="https://cl.computrabajo.com/ofertas-de-trabajo/oferta-de-trabajo-de-desarrollador-python-12345",
        company_name="Empresa Tech",
        title="Desarrollador Python Backend",
        session_id="ses_test",
        initial_state="READY_TO_APPLY",
        description_text="Buscamos Desarrollador Python con experiencia en APIs REST y bases de datos.",
    )

    offer = repo.get_application(app_id)
    assert offer is not None

    flow = ComputrabajoFlow()
    mock_page = AsyncMock()

    res = await flow.simulate(mock_page, offer, repo, dossier_dir)
    assert res["ok"] is True
    assert res["status"] == "SIMULATED_READY"
    assert "cv_selected" in res

    updated = repo.get_application(app_id)
    assert updated["state"] == "SIMULATED_READY"


@pytest.mark.asyncio
async def test_fill_killer_questions_helper():
    mock_page = AsyncMock()
    mock_page.evaluate = AsyncMock(return_value=True)
    mock_page.query_selector_all = AsyncMock(return_value=[])

    res = await _fill_killer_questions_if_present(
        mock_page,
        salary_clp=950000,
        cover_letter="Estimado equipo...",
        precomputed_answers={"¿Tienes experiencia en Python?": "2 años de experiencia"},
    )
    assert res is True
