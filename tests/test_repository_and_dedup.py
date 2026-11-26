"""Pruebas unitarias e integradas de persistencia SQLite, deduplicación, estados y caché."""

from __future__ import annotations

from pathlib import Path

from job_automator.db.repository import JobRepository


def test_deduplication_by_id_url_and_fingerprint(tmp_path: Path) -> None:
    db_file = tmp_path / "test_jobs.db"
    repo = JobRepository(db_path=db_file)

    offer_id, app_id = repo.register_offer_and_application(
        portal="chiletrabajos",
        url="https://www.chiletrabajos.cl/trabajo/desarrollador-python-3917769",
        company_name="23people",
        title="Full Stack Java Developer",
        external_job_id="3917769",
        initial_state="CONFIRMED",
    )
    assert offer_id > 0
    assert app_id > 0

    # 1. Duplicado por external_job_id
    dup1, reason1 = repo.is_offer_duplicate(
        portal="chiletrabajos",
        external_job_id="3917769",
        url="https://www.chiletrabajos.cl/otra-url",
        company="Otra",
        title="Otro Cargo",
    )
    assert dup1 is True
    assert "external_job_id" in (reason1 or "")

    # 2. Duplicado por canonical_url (diferente formato de URL del mismo ID)
    dup2, reason2 = repo.is_offer_duplicate(
        portal="chiletrabajos",
        external_job_id=None,
        url="https://www.chiletrabajos.cl/trabajo/postular/3917769#fragment",
        company="Empresa Distinta",
        title="Cargo Distinto",
    )
    assert dup2 is True
    assert "canonical_url" in (reason2 or "")

    # 3. Duplicado por huella semántica (misma empresa y mismo cargo en otro portal)
    dup3, reason3 = repo.is_offer_duplicate(
        portal="computrabajo",
        external_job_id="ABC123456789",
        url="https://cl.computrabajo.com/ofertas/full-stack-java-developer-123",
        company="23People!",
        title="Full-Stack Java Developer",
    )
    assert dup3 is True
    assert "huella" in (reason3 or "")


def test_state_transitions_and_cache(tmp_path: Path) -> None:
    repo = JobRepository(db_path=tmp_path / "test_states.db")
    _, app_id = repo.register_offer_and_application(
        portal="computrabajo",
        url="https://cl.computrabajo.com/ofertas/analista-ti-999",
        company_name="KMI Security",
        title="Soporte Técnico TI",
        initial_state="READY_TO_APPLY",
    )

    updated = repo.transition_application_state(
        app_id,
        "APPLYING",
        reason="Iniciando postulación",
        last_step_reached="SUBMIT_CLICK",
        increment_attempt=True,
    )
    assert updated["state"] == "APPLYING"
    assert updated["attempt_count"] == 1
    assert updated["last_step_reached"] == "SUBMIT_CLICK"

    # Probar caché SHA-256 de respuestas
    repo.set_cached_answer(
        "KILLER_ANSWER",
        "¿Cuentas con experiencia en Docker y Linux?",
        "Sí, cuento con 2 años de experiencia administrando contenedores Docker y servidores Linux en Acme Cloud.",
        source="MCP_AGENT",
    )
    cached = repo.get_cached_answer("KILLER_ANSWER", "  ¿cuentas con experiencia en docker y linux? ")
    assert cached is not None
    assert "Docker" in cached["output"]
