"""Servidor MCP oficial para operar Job Automator desde clientes compatibles con Model Context Protocol."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

try:
    from mcp.server.mcpserver import MCPServer
except ImportError:
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[no-redef]

from job_automator.browser.cdp_manager import discover_cdp_endpoint
from job_automator.config import get_search_preferences, get_workspace_root
from job_automator.db.migrate_legacy import migrate_legacy_workspace
from job_automator.db.repository import JobRepository
from job_automator.intelligence.fact_checker import validate_text_truthfulness
from job_automator.logging_utils import log_event
from job_automator.reporting.report_builder import build_consolidated_report
from job_automator.worker.interactive_assistant import (
    confirm_application_submission as assistant_confirm_application_submission,
    inject_tailored_answers as assistant_inject_tailored_answers,
    prepare_offer_for_review as assistant_prepare_offer_for_review,
)
from job_automator.worker.recovery import (
    is_pid_running,
    recover_orphaned_tasks,
    requeue_retryable_tasks,
)

mcp = MCPServer("job-automator-mcp")

# Matriz oficial de operatividad de portales
PORTAL_READINESS: dict[str, dict[str, Any]] = {
    "chiletrabajos": {
        "status": "OPERATIONAL",
        "supported_for_session": True,
        "detail": "Flujo asistido interactivo operativo con adjunto de CV ATS y verificación DOM.",
    },
    "computrabajo": {
        "status": "OPERATIONAL",
        "supported_for_session": True,
        "detail": "Flujo asistido con sincronización de CV en segundo plano, pre-llenado de preguntas filtro y verificación DOM.",
    },
    "laborum": {
        "status": "PENDING_ADAPTATION",
        "supported_for_session": False,
        "detail": "Pendiente de adaptación al flujo asistido interactivo.",
    },
    "indeed": {
        "status": "PENDING_ADAPTATION",
        "supported_for_session": False,
        "detail": "Pendiente de adaptación al flujo asistido interactivo.",
    },
    "getonboard": {
        "status": "PENDING_ADAPTATION",
        "supported_for_session": False,
        "detail": "Pendiente de adaptación al flujo asistido interactivo.",
    },
}


def _spawn_background_worker(session_id: str, db_path: str | None = None) -> int:
    """Lanza el proceso Worker de forma desacoplada para no bloquear las llamadas MCP."""
    cmd = [
        sys.executable,
        "-m",
        "job_automator.worker.runner",
        "--session-id",
        session_id,
    ]
    if db_path:
        cmd.extend(["--db-path", db_path])

    ws_root = get_workspace_root()
    env = dict(os.environ)
    src_path = str(Path(__file__).resolve().parent.parent)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{src_path}{os.pathsep}{existing_pp}".rstrip(os.pathsep)

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(
        cmd,
        cwd=str(ws_root),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )
    return int(proc.pid)


@mcp.tool()
def start_job_session(
    mode: Literal["LIVE", "DRY_RUN"] = "LIVE",
    portals: list[str] | None = None,
    max_applications: int = 10,
    salary_strategy: Literal["FAST_HIRE", "STANDARD"] = "FAST_HIRE",
    locations: list[str] | None = None,
) -> dict[str, Any]:
    """
    Comando 'postula' (mode='LIVE') o 'simular' (mode='DRY_RUN').
    Inicia una nueva sesión de trabajo controlada para los portales operativos (Chiletrabajos).
    Bloquea de forma segura portales en investigación técnica (Computrabajo).
    """
    repo = JobRepository()
    # Asegurar que el historial esté importado antes de operar
    counts = repo.get_counts_by_state(include_legacy=True)
    if sum(counts.values()) == 0:
        migrate_legacy_workspace(repo=repo)

    recover_orphaned_tasks(repo)
    active = repo.get_active_session()
    if active and active["status"] == "RUNNING" and is_pid_running(active.get("worker_pid")):
        return {
            "ok": False,
            "error": "SESSION_ALREADY_RUNNING",
            "session_id": active["session_id"],
            "status": active["status"],
            "worker_pid": active.get("worker_pid"),
            "message": f"Ya existe una sesión en ejecución ({active['session_id']}). Usa 'estado', 'pausar' o 'detener'.",
        }

    # 1. Validación de compatibilidad operativa de portales
    requested_portals = [p.lower() for p in (portals or ["chiletrabajos", "computrabajo"])]
    blocked_portals = {
        p: PORTAL_READINESS.get(p, {}).get("detail", "Portal no adaptado al flujo asistido.")
        for p in requested_portals
        if not PORTAL_READINESS.get(p, {}).get("supported_for_session", False)
    }
    allowed_portals = [
        p for p in requested_portals
        if PORTAL_READINESS.get(p, {}).get("supported_for_session", False)
    ]

    if not allowed_portals:
        return {
            "ok": False,
            "error": "PORTAL_NOT_OPERATIONAL",
            "message": (
                "Ninguno de los portales solicitados está operativo actualmente para el flujo continuo. "
                "Recuerda que el flujo asistido continuo actualmente solo está operativo para Chiletrabajos. "
                "Computrabajo está en estado IN_INVESTIGATION debido al bloqueo de cupo (10 CVs) "
                "y a la subpágina /candidate/kq."
            ),
            "blocked_portals": blocked_portals,
            "portal_readiness": PORTAL_READINESS,
        }

    cdp = discover_cdp_endpoint()
    session_id = f"ses_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    prefs = get_search_preferences()
    selected_locations = locations or prefs.get("regions", {}).get("target_locations") or ["Remote"]

    repo.create_session(
        session_id=session_id,
        mode=mode,
        salary_strategy=salary_strategy,
        max_applications=max_applications,
        portals=allowed_portals,
        locations=selected_locations,
    )

    if not cdp.available:
        repo.update_session_status(
            session_id,
            "PAUSED_AUTH_REQUIRED",
            notes=cdp.error,
        )
        return {
            "ok": False,
            "session_id": session_id,
            "status": "PAUSED_AUTH_REQUIRED",
            "mode": mode,
            "cdp_connected": False,
            "message": (
                f"Sesión {session_id} registrada en pausa: {cdp.error} "
                "Por favor abre tu navegador Chrome con sesión iniciada y ejecuta 'reanudar'."
            ),
        }

    pid = _spawn_background_worker(session_id, db_path=str(repo.db_path))
    repo.update_session_status(session_id, "RUNNING", worker_pid=pid)
    log_event("mcp_server", "session_started", session_id=session_id, details={"mode": mode, "pid": pid, "portals": allowed_portals})

    res: dict[str, Any] = {
        "ok": True,
        "session_id": session_id,
        "status": "RUNNING",
        "mode": mode,
        "salary_strategy": salary_strategy,
        "portals": allowed_portals,
        "locations": selected_locations,
        "max_applications": max_applications,
        "worker_pid": pid,
        "cdp_connected": True,
        "cdp_source": cdp.source,
        "message": f"Sesión '{session_id}' iniciada en modo {mode} sobre portales operativos {allowed_portals} (PID: {pid}).",
    }
    if blocked_portals:
        res["excluded_portals_under_investigation"] = blocked_portals
    return res


@mcp.tool()
def get_session_status(session_id: str | None = None) -> dict[str, Any]:
    """
    Comando 'estado'.
    Consulta el progreso actual de la sesión, el desglose verificado por portal y estado,
    y la matriz oficial de operatividad (Chiletrabajos operativo vs Computrabajo en investigación).
    """
    repo = JobRepository()
    recovery_info = recover_orphaned_tasks(repo)
    ses = repo.get_session(session_id)

    # Desglose detallado por portal desde SQLite
    portal_breakdown: dict[str, dict[str, int]] = {}
    with repo.connect() as conn:
        p_rows = conn.execute("""
            SELECT jo.portal, a.state, count(*) as cnt
            FROM applications a
            JOIN job_offers jo ON jo.id = a.job_offer_id
            WHERE a.is_legacy_imported = 0
            GROUP BY jo.portal, a.state
        """).fetchall()
        for r in p_rows:
            p = r["portal"].lower()
            if p not in portal_breakdown:
                portal_breakdown[p] = {}
            portal_breakdown[p][r["state"]] = int(r["cnt"])

    global_counts = repo.get_counts_by_state(include_legacy=True)

    if not ses:
        return {
            "ok": True,
            "session_id": None,
            "status": "IDLE",
            "worker_alive": False,
            "portal_readiness": PORTAL_READINESS,
            "portal_breakdown": portal_breakdown,
            "global_counts_by_state": global_counts,
            "message": (
                "Base de datos cargada. Portales operativos configurados en PORTAL_READINESS; "
                "revisa el detalle por portal para iniciar una sesión."
            ),
        }

    sid = ses["session_id"]
    pid = ses.get("worker_pid")
    alive = is_pid_running(pid)
    session_counts = repo.get_counts_by_state(session_id=sid, include_legacy=False)
    active_items = repo.list_applications(
        session_id=sid,
        states=["APPLYING", "READY_TO_APPLY", "SIMULATED_READY", "NEEDS_HUMAN_REVIEW"],
        include_legacy=False,
        limit=15,
    )
    waiting_ai_items = [
        a
        for a in repo.list_applications(
            states=["NEEDS_HUMAN_REVIEW"],
            include_legacy=False,
            limit=30,
        )
        if a.get("last_step_reached") == "WAITING_AI_ANSWERS" or a.get("killer_questions_json")
    ]

    return {
        "ok": True,
        "session_id": sid,
        "mode": ses["mode"],
        "status": ses["status"],
        "salary_strategy": ses.get("salary_strategy"),
        "worker_pid": pid,
        "worker_alive": alive,
        "heartbeat_at": ses.get("heartbeat_at"),
        "notes": ses.get("notes"),
        "recovery_check": recovery_info,
        "portal_readiness": PORTAL_READINESS,
        "portal_breakdown": portal_breakdown,
        "session_counts_by_state": session_counts,
        "global_counts_by_state": global_counts,
        "pending_ai_questions_to_write": [
            {
                "application_id": a["id"],
                "company": a["company_name"],
                "title": a["title"],
                "portal": a["portal"],
                "selected_cv": a.get("selected_cv_target"),
                "questions": json.loads(a.get("killer_questions_json") or "[]"),
            }
            for a in waiting_ai_items
        ],
        "active_or_pending_tasks": [
            {
                "id": a["id"],
                "company": a["company_name"],
                "title": a["title"],
                "portal": a["portal"],
                "state": a["state"],
                "selected_cv": a.get("selected_cv_target"),
                "match_score": a.get("match_score"),
                "last_step": a["last_step_reached"],
                "questions": json.loads(a.get("killer_questions_json") or "[]"),
                "reason": a["last_error_message"],
            }
            for a in active_items
        ],
    }


@mcp.tool()
def investigate_portal(portal: str = "computrabajo") -> dict[str, Any]:
    """
    Comando de investigación técnica para diagnosticar el estado en vivo de un portal en Chrome vía CDP.
    Para 'computrabajo', verifica si la pestaña activa está en /candidate/kq o /candidate/cv/uploadcv,
    identifica campos de preguntas filtro y reporta los bloqueos técnicos detectados.
    """
    p_name = portal.lower().strip()
    readiness = PORTAL_READINESS.get(p_name, {"status": "UNKNOWN", "detail": "Portal no registrado."})

    cdp = discover_cdp_endpoint()
    if not cdp.available:
        return {
            "ok": False,
            "portal": p_name,
            "portal_readiness": readiness,
            "cdp_available": False,
            "error": "CDP_UNAVAILABLE",
            "message": f"Chrome no está disponible en el puerto 9222 ({cdp.error}). Abre Chrome con depuración activa.",
        }

    targets: list[dict[str, Any]] = []
    try:
        req = urllib.request.urlopen("http://127.0.0.1:9222/json", timeout=3)
        targets = json.loads(req.read().decode())
    except Exception as e:
        return {
            "ok": False,
            "portal": p_name,
            "portal_readiness": readiness,
            "cdp_available": True,
            "error": "CDP_QUERY_ERROR",
            "message": f"Error consultando endpoints de Chrome: {e}",
        }

    active_pages = [t for t in targets if t.get("type") == "page"]
    matching_tabs = [t for t in active_pages if p_name in t.get("url", "").lower()]

    known_issues = []
    if p_name == "computrabajo":
        known_issues = [
            "Tope de 10 CVs en cuenta: Computrabajo oculta el input de subida si hay 10 documentos. Requiere borrar uno desmarcado (#it-delete -> #btDelete) para habilitar el cupo.",
            "Subpágina Killer Questions (/candidate/kq): Requiere mapear KillerQuestions[i].Title. En radios, value='0' es Sí y value='1' es No. En campos abiertos se debe respetar el perfil verificado sin inventar experiencia.",
        ]
    elif p_name in ("laborum", "indeed"):
        known_issues = [
            "Portal pendiente de adaptación al flujo interactivo asistido.",
        ]

    return {
        "ok": True,
        "portal": p_name,
        "portal_readiness": readiness,
        "cdp_available": True,
        "total_chrome_tabs": len(active_pages),
        "matching_portal_tabs": [
            {"title": t.get("title"), "url": t.get("url")}
            for t in matching_tabs
        ],
        "diagnostics": {
            "is_portal_open_in_chrome": len(matching_tabs) > 0,
            "is_on_killer_questions": any("/candidate/kq" in t.get("url", "") for t in matching_tabs),
            "is_on_cv_management": any("/uploadcv" in t.get("url", "") for t in matching_tabs),
            "known_issues": known_issues,
            "investigation_recommendation": (
                "Para operar Computrabajo se debe completar la investigación técnica del ciclo de eliminación "
                "de CVs y el mapeo de /candidate/kq antes de habilitar su automatización continua."
                if p_name == "computrabajo" else "Portal pendiente de diseño del flujo asistido."
            ),
        },
    }


@mcp.tool()
def generate_report(
    session_id: str | None = None,
    update_markdown_file: bool = True,
) -> dict[str, Any]:
    """
    Comando 'reporte'.
    Obtiene un resumen verificable de resultados y actualiza APPLICATION_REPORT.md.
    """
    repo = JobRepository()
    recover_orphaned_tasks(repo)
    return build_consolidated_report(
        repo=repo,
        session_id=session_id,
        write_markdown=update_markdown_file,
    )


@mcp.tool()
def pause_session(session_id: str | None = None) -> dict[str, Any]:
    """
    Comando 'pausar'.
    Impide que comiencen nuevas tareas; el Worker finaliza limpiamente la tarea en curso y queda en pausa.
    """
    repo = JobRepository()
    ses = repo.get_session(session_id)
    if not ses:
        return {"ok": False, "message": "No existe ninguna sesión para pausar."}

    sid = ses["session_id"]
    prev = ses["status"]
    updated = repo.update_session_status(
        sid,
        "PAUSED",
        notes="Sesión pausada por el usuario desde Antigravity.",
    )
    log_event("mcp_server", "session_paused", session_id=sid)
    return {
        "ok": True,
        "session_id": sid,
        "previous_status": prev,
        "new_status": updated["status"] if updated else "PAUSED",
        "message": f"Sesión {sid} marcada como PAUSED. No se iniciarán nuevas tareas.",
    }


@mcp.tool()
def resume_session(
    session_id: str | None = None,
    approve_simulated_to_live: bool = False,
) -> dict[str, Any]:
    """
    Comando 'reanudar'.
    Continúa el trabajo pendiente de una sesión pausada (o aprueba el envío real de tareas en SIMULATED_READY).
    """
    repo = JobRepository()
    recover_orphaned_tasks(repo)
    ses = repo.get_session(session_id)
    if not ses:
        return {"ok": False, "message": "No se encontró ninguna sesión para reanudar."}

    sid = ses["session_id"]
    cdp = discover_cdp_endpoint()
    if not cdp.available:
        return {
            "ok": False,
            "session_id": sid,
            "status": ses["status"],
            "message": f"No se puede reanudar todavía: {cdp.error}",
        }

    promoted_count = 0
    new_mode = ses["mode"]
    if approve_simulated_to_live:
        new_mode = "LIVE"
        sim_tasks = repo.list_applications(session_id=sid, states=["SIMULATED_READY"], include_legacy=False)
        if not sim_tasks:
            sim_tasks = repo.list_applications(states=["SIMULATED_READY"], include_legacy=False)
        for t in sim_tasks:
            repo.transition_application_state(
                int(t["id"]),
                "READY_TO_APPLY",
                reason="Aprobada por el usuario para envío real (LIVE)",
            )
            promoted_count += 1

    pid = ses.get("worker_pid")
    if not is_pid_running(pid):
        pid = _spawn_background_worker(sid, db_path=str(repo.db_path))

    repo.update_session_status(
        sid,
        "RUNNING",
        notes="Sesión reanudada por el usuario.",
        worker_pid=pid,
        mode=new_mode,
    )
    log_event("mcp_server", "session_resumed", session_id=sid, details={"pid": pid, "promoted_simulated": promoted_count})
    return {
        "ok": True,
        "session_id": sid,
        "status": "RUNNING",
        "mode": new_mode,
        "worker_pid": pid,
        "promoted_simulated_tasks": promoted_count,
        "message": f"Sesión {sid} reanudada (Worker PID: {pid}).",
    }


@mcp.tool()
def stop_session(session_id: str | None = None) -> dict[str, Any]:
    """
    Comando 'detener'.
    Solicita una parada controlada definitiva sin matar procesos de Chrome del usuario.
    """
    repo = JobRepository()
    ses = repo.get_session(session_id)
    if not ses:
        return {"ok": False, "message": "No existe ninguna sesión activa para detener."}

    sid = ses["session_id"]
    pid = ses.get("worker_pid")
    if is_pid_running(pid):
        repo.update_session_status(
            sid,
            "STOP_REQUESTED",
            notes="Parada controlada solicitada por el usuario.",
        )
        msg = f"Parada controlada solicitada para {sid}. El Worker cerrará la pestaña tras el paso actual y pasará a STOPPED."
    else:
        repo.update_session_status(
            sid,
            "STOPPED",
            notes="Sesión detenida limpiamente.",
        )
        msg = f"Sesión {sid} marcada como STOPPED."

    log_event("mcp_server", "session_stopped", session_id=sid)
    return {
        "ok": True,
        "session_id": sid,
        "status": (repo.get_session(sid) or {}).get("status"),
        "message": msg,
    }


@mcp.tool()
def retry_failed_tasks(
    session_id: str | None = None,
    application_ids: list[int] | None = None,
    auto_resume: bool = True,
) -> dict[str, Any]:
    """
    Comando 'reintentar'.
    Recupera tareas fallidas recuperables (FAILED_RETRYABLE) protegiendo contra reenvíos duplicados en SUBMITTED_UNVERIFIED.
    """
    repo = JobRepository()
    recover_orphaned_tasks(repo)
    ses = repo.get_session(session_id)
    sid = ses["session_id"] if ses else None

    result = requeue_retryable_tasks(repo, session_id=sid, application_ids=application_ids)
    resumed_info = None
    if auto_resume and sid and result["requeued_count"] > 0:
        resumed_info = resume_session(session_id=sid)

    return {
        "ok": True,
        "session_id": sid,
        "retry_summary": result,
        "resumed": resumed_info,
    }


@mcp.tool()
def submit_ai_answers_for_application(
    application_id: int,
    answers_by_question: dict[str, str],
) -> dict[str, Any]:
    """
    Permite al agente de Antigravity inyectar respuestas redactadas para preguntas abiertas nuevas,
    validándolas con el Fact-Checker (sin inventar experiencia) y guardándolas en caché SQLite.
    """
    repo = JobRepository()
    app = repo.get_application(application_id)
    if not app:
        return {"ok": False, "error": f"Postulación {application_id} no encontrada."}

    violations: dict[str, list[str]] = {}
    for q_label, answer_text in answers_by_question.items():
        check = validate_text_truthfulness(answer_text)
        if not check.valid:
            violations[q_label] = check.violations

    if violations:
        return {
            "ok": False,
            "error": "FACT_CHECK_REJECTED",
            "violations": violations,
            "message": "Se rechazaron respuestas porque contienen afirmaciones no autorizadas en el perfil real.",
        }

    existing_answers = {
        k.strip().strip('"').strip("'"): v
        for k, v in json.loads(app.get("killer_answers_json") or "{}").items()
    }
    for q_label, answer_text in answers_by_question.items():
        clean_key = q_label.strip().strip('"').strip("'")
        existing_answers[clean_key] = answer_text
        repo.set_cached_answer("KILLER_ANSWER", clean_key, answer_text, source="MCP_AGENT")

    repo.transition_application_state(
        application_id,
        "READY_TO_APPLY",
        reason="Respuestas validadas por Fact-Checker y almacenadas en caché SQLite.",
        extra_updates={"killer_answers_json": json.dumps(existing_answers, ensure_ascii=False)},
    )
    return {
        "ok": True,
        "application_id": application_id,
        "new_state": "READY_TO_APPLY",
        "cached_questions_count": len(answers_by_question),
    }


@mcp.tool()
async def prepare_offer_for_review(app_id: int) -> dict[str, Any]:
    """
    Comando 'postula' (flujo interactivo 1 a 1).
    Carga y prepara el formulario de la oferta en Chrome para revisión del usuario y del Agente.
    Retorna la descripción completa y las preguntas para que el Agente redacte las respuestas a medida.
    """
    return await assistant_prepare_offer_for_review(app_id)


@mcp.tool()
async def inject_tailored_answers(
    app_id: int,
    answers_by_question: dict[str, str],
) -> dict[str, Any]:
    """
    Inyecta respuestas a medida validadas por el Fact-Checker en la postulación y directamente en el formulario abierto de Chrome.
    """
    return await assistant_inject_tailored_answers(app_id=app_id, answers_by_question=answers_by_question)


@mcp.tool()
async def confirm_application_submission(app_id: int) -> dict[str, Any]:
    """
    Verifica en el DOM que el envío fue realizado, captura evidencia oficial y marca el estado como CONFIRMED.
    """
    return await assistant_confirm_application_submission(app_id)


def main() -> None:
    """Inicia el servidor MCP por stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
