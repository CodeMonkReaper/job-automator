"""Recuperación de tareas interrumpidas, gestión de reintentos y sonda para SUBMITTED_UNVERIFIED."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from job_automator.db.repository import JobRepository
from job_automator.logging_utils import log_event


def is_pid_running(pid: int | None) -> bool:
    """Verifica de forma segura en Windows/POSIX si un proceso con el PID indicado sigue vivo."""
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def recover_orphaned_tasks(repo: JobRepository, stale_seconds: int = 45) -> dict[str, Any]:
    """
    Detecta tareas que quedaron en estado 'APPLYING' por un cierre inesperado del Worker:
    - Si last_step_reached == 'SUBMIT_CLICK', las mueve a 'SUBMITTED_UNVERIFIED' (prohibido reenviar a ciegas).
    - Si last_step_reached es previo al clic de envío, las revierte de forma segura a 'READY_TO_APPLY'.
    """
    active_ses = repo.get_active_session()
    worker_alive = False
    if active_ses:
        pid = active_ses.get("worker_pid")
        hb_str = active_ses.get("heartbeat_at")
        if is_pid_running(pid) and hb_str:
            try:
                hb_dt = datetime.fromisoformat(hb_str)
                age = (datetime.now(timezone.utc) - hb_dt).total_seconds()
                if age <= stale_seconds:
                    worker_alive = True
            except Exception:
                pass

    if worker_alive:
        return {"worker_alive": True, "recovered_to_ready": 0, "moved_to_unverified": 0}

    applying_tasks = repo.list_applications(states=["APPLYING"], include_legacy=False)
    recovered_ready = 0
    moved_unverified = 0

    for task in applying_tasks:
        app_id = int(task["id"])
        last_step = (task.get("last_step_reached") or "").upper()
        if last_step in ("SUBMIT_CLICK", "POST_VERIFY"):
            repo.transition_application_state(
                app_id,
                "SUBMITTED_UNVERIFIED",
                reason="Recuperación tras interrupción después de pulsar Enviar; requiere sonda de verificación antes de reintentar.",
                error_code="INTERRUPTED_AFTER_SUBMIT",
            )
            moved_unverified += 1
        else:
            repo.transition_application_state(
                app_id,
                "READY_TO_APPLY",
                reason="Recuperación segura tras interrupción previa al envío.",
                error_code="INTERRUPTED_PRE_SUBMIT",
            )
            recovered_ready += 1

    if recovered_ready or moved_unverified:
        log_event(
            "recovery",
            "orphaned_tasks_recovered",
            details={"recovered_to_ready": recovered_ready, "moved_to_unverified": moved_unverified},
        )

    return {
        "worker_alive": False,
        "recovered_to_ready": recovered_ready,
        "moved_to_unverified": moved_unverified,
    }


def requeue_retryable_tasks(
    repo: JobRepository,
    session_id: str | None = None,
    application_ids: list[int] | None = None,
) -> dict[str, Any]:
    """
    Reencola tareas en FAILED_RETRYABLE que no hayan superado max_attempts.
    NO reencola a ciegas tareas en SUBMITTED_UNVERIFIED ni NEEDS_HUMAN_REVIEW.
    """
    candidates = repo.list_applications(
        session_id=session_id,
        states=["FAILED_RETRYABLE", "SUBMITTED_UNVERIFIED"],
        include_legacy=False,
    )
    requeued: list[int] = []
    skipped_max_attempts: list[int] = []
    unverified_requiring_probe: list[int] = []

    for app in candidates:
        aid = int(app["id"])
        if application_ids and aid not in application_ids:
            continue

        if app["state"] == "SUBMITTED_UNVERIFIED":
            unverified_requiring_probe.append(aid)
            continue

        if int(app["attempt_count"]) >= int(app["max_attempts"]):
            repo.transition_application_state(
                aid,
                "FAILED_FATAL",
                reason=f"Alcanzó el límite máximo de intentos ({app['max_attempts']})",
            )
            skipped_max_attempts.append(aid)
            continue

        repo.transition_application_state(
            aid,
            "READY_TO_APPLY",
            reason="Reencolada por comando 'reintentar'",
        )
        requeued.append(aid)

    return {
        "requeued_ids": requeued,
        "requeued_count": len(requeued),
        "skipped_max_attempts_ids": skipped_max_attempts,
        "unverified_ids_protected_from_blind_retry": unverified_requiring_probe,
    }
