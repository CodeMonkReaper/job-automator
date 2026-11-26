"""Worker persistente en segundo plano para búsqueda, evaluación y postulación controlada."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from job_automator.adapters.base import BasePortalAdapter
from job_automator.adapters.chiletrabajos import ChiletrabajosAdapter
from job_automator.adapters.computrabajo import ComputrabajoAdapter
from job_automator.adapters.getonboard import GetOnBoardAdapter
from job_automator.adapters.indeed import IndeedAdapter
from job_automator.adapters.laborum import LaborumAdapter
from job_automator.browser.cdp_manager import discover_cdp_endpoint
from job_automator.browser.hud import update_browser_hud
from job_automator.config import get_candidate_profile, get_cvs_dir, get_search_preferences
from job_automator.db.repository import JobRepository
from job_automator.intelligence.answer_generator import build_cover_letter
from job_automator.intelligence.cv_selector import select_cv_and_salary
from job_automator.intelligence.evaluator import evaluate_offer
from job_automator.logging_utils import log_event
from job_automator.reporting.dossier_writer import (
    append_submission_audit_log,
    clean_folder_name,
    prepare_dossier_files,
)
from job_automator.reporting.report_builder import build_consolidated_report
from job_automator.flows import get_portal_flow
from job_automator.worker.recovery import recover_orphaned_tasks


ADAPTERS: dict[str, BasePortalAdapter] = {
    "chiletrabajos": ChiletrabajosAdapter(),
    "computrabajo": ComputrabajoAdapter(),
    "laborum": LaborumAdapter(),
    "indeed": IndeedAdapter(),
    "getonboard": GetOnBoardAdapter(),
}


async def run_worker_session(session_id: str, db_path: str | None = None) -> dict[str, Any]:
    """Ejecuta el ciclo completo de una sesión de trabajo de forma controlada y persistente."""
    repo = JobRepository(db_path=db_path)
    recover_orphaned_tasks(repo)

    session = repo.get_session(session_id)
    if not session:
        raise ValueError(f"Sesión {session_id} no existe en la base de datos.")

    my_pid = os.getpid()
    repo.touch_heartbeat(session_id, worker_pid=my_pid)
    log_event("worker", "session_worker_started", session_id=session_id, details={"pid": my_pid})

    prefs = get_search_preferences()
    mode = session["mode"]
    dry_run = mode == "DRY_RUN"
    salary_strategy = session.get("salary_strategy", "FAST_HIRE")
    max_apps = int(session.get("max_applications", 10))
    portals: list[str] = json.loads(session.get("portals_json") or '["chiletrabajos", "computrabajo"]')
    fallback_locs = json.dumps(prefs.get("regions", {}).get("target_locations") or ["Remote"])
    locations: list[str] = json.loads(session.get("locations_json") or fallback_locs)
    queries: list[str] = prefs.get("search_queries", ["desarrollador", "soporte ti", "analista ti"])

    # 1. Descubrir endpoint CDP de Chrome
    cdp_info = discover_cdp_endpoint()
    if not cdp_info.available or not cdp_info.ws_url:
        repo.update_session_status(
            session_id,
            "PAUSED_AUTH_REQUIRED",
            notes=cdp_info.error or "Puerto CDP de Chrome no disponible.",
        )
        log_event("worker", "cdp_unavailable_halt", level="ERROR", session_id=session_id, details={"error": cdp_info.error})
        return {"status": "PAUSED_AUTH_REQUIRED", "reason": cdp_info.error}

    processed_count = 0
    confirmed_count = 0

    async with async_playwright() as p:
        browser = None
        last_err = None
        cdp_headers = None
        if cdp_info.ws_url and "://" in cdp_info.ws_url:
            host_part = cdp_info.ws_url.split("://", 1)[1].split("/", 1)[0]
            cdp_headers = {"Host": host_part}

        for attempt_cdp in range(3):
            try:
                browser = await p.chromium.connect_over_cdp(cdp_info.ws_url, headers=cdp_headers, timeout=25000)
                break
            except Exception as e:
                try:
                    browser = await p.chromium.connect_over_cdp(cdp_info.ws_url, timeout=25000)
                    break
                except Exception:
                    pass
                last_err = e
                await asyncio.sleep(2.0)
        if browser is None:
            msg = f"No fue posible conectar vía CDP al navegador del usuario: {last_err}"
            repo.update_session_status(session_id, "PAUSED_AUTH_REQUIRED", notes=msg)
            log_event("worker", "cdp_connect_failed", level="ERROR", session_id=session_id, details={"error": str(last_err)})
            return {"status": "PAUSED_AUTH_REQUIRED", "reason": msg}

        context = browser.contexts[0] if browser.contexts else await browser.new_context()

        # Reutilizar pestaña dedicada con HUD de Antigravity si ya está abierta para que el usuario siempre vea la misma ventana
        page = None
        for existing_p in context.pages:
            try:
                if not existing_p.url.startswith("http"):
                    continue
                has_hud = await asyncio.wait_for(
                    existing_p.evaluate("() => !!document.getElementById('antigravity-live-hud')"),
                    timeout=2.0,
                )
                if has_hud:
                    page = existing_p
                    break
            except Exception:
                continue
        if page is None:
            page = context.pages[0] if context.pages else await context.new_page()

        try:
            await update_browser_hud(
                page,
                portal="INICIANDO",
                status="Pre-Flight Check",
                offer_title="Verificando sesiones activas en portales...",
                detail=f"Sesión {session_id} en modo {mode}",
            )

            # 2. Pre-Flight Check obligatorio de sesión en los portales configurados
            authenticated_portals: list[str] = []
            for portal_name in portals:
                adapter = ADAPTERS.get(portal_name.lower())
                if not adapter:
                    continue
                repo.touch_heartbeat(session_id, worker_pid=my_pid)
                ok, reason = await adapter.check_session(page)
                if ok:
                    authenticated_portals.append(portal_name.lower())
                else:
                    log_event(
                        "worker",
                        "portal_preflight_failed",
                        level="WARNING",
                        session_id=session_id,
                        details={"portal": portal_name, "reason": reason},
                    )

            if not authenticated_portals:
                msg = "Ninguno de los portales solicitados tiene sesión activa en Chrome (Pre-Flight Check rechazado)."
                repo.update_session_status(session_id, "PAUSED_AUTH_REQUIRED", notes=msg)
                return {"status": "PAUSED_AUTH_REQUIRED", "reason": msg}

            # 3. En modo simulación (DRY_RUN), si hay menos ofertas que max_apps, buscar ofertas nuevas
            target_existing = ["READY_TO_APPLY"] if dry_run else ["SIMULATED_READY", "READY_TO_APPLY"]
            existing_ready = repo.list_applications(
                states=target_existing, portals=portals, include_legacy=False, limit=max_apps
            )
            if dry_run and len(existing_ready) < max_apps:
                needed = max(5, max_apps - len(existing_ready))
                for portal_name in authenticated_portals:
                    cur_ses = repo.get_session(session_id)
                    if cur_ses and cur_ses["status"] in ("PAUSED", "STOP_REQUESTED", "STOPPED"):
                        break

                    adapter = ADAPTERS[portal_name]
                    repo.touch_heartbeat(session_id, worker_pid=my_pid)
                    offers = await adapter.search_offers(
                        page,
                        queries=queries,
                        locations=locations,
                        max_results=needed,
                        is_known_offer_fn=lambda p_name, ext_id, u, comp, tit: repo.is_offer_duplicate(
                            p_name, ext_id, u, comp, tit
                        )[0],
                    )
                    for off in offers:
                        is_dup, _ = repo.is_offer_duplicate(
                            off.portal, off.external_job_id, off.url, off.company, off.title
                        )
                        if is_dup:
                            continue

                        eval_res = evaluate_offer(
                            title=off.title,
                            description=off.description,
                            location=off.location,
                            preferences=prefs,
                            url=off.url,
                        )
                        cv_sel = select_cv_and_salary(
                            title=off.title,
                            description=off.description,
                            salary_strategy=salary_strategy,
                            preferences=prefs,
                        )
                        letter = build_cover_letter(off.company, off.title, cv_sel.salary_clp)
                        f_name = clean_folder_name(off.company, off.title)

                        initial_st = "READY_TO_APPLY" if eval_res.eligible else "SKIPPED_FILTER"
                        repo.register_offer_and_application(
                            portal=off.portal,
                            url=off.url,
                            company_name=off.company,
                            title=off.title,
                            external_job_id=off.external_job_id,
                            apply_url=off.apply_url,
                            location=off.location,
                            modality=eval_res.detected_modality,
                            raw_salary_text=off.raw_salary,
                            description_text=off.description,
                            match_score=eval_res.score,
                            match_decision=eval_res.decision,
                            rejection_reason=None if eval_res.eligible else eval_res.reason,
                            session_id=session_id,
                            initial_state=initial_st,
                            selected_cv_base=cv_sel.cv_base,
                            selected_cv_target=cv_sel.target_name,
                            folder_name=f_name,
                            expected_salary_clp=cv_sel.salary_clp,
                            cover_letter_text=letter,
                        )

            # 4. Procesar cola de ofertas en estado READY_TO_APPLY consultando dinámicamente en cada iteración
            waiting_ai_count = 0
            while processed_count < max_apps:
                # Verificar señales de control (pausa / detención) antes de iniciar cada oferta
                cur_ses = repo.get_session(session_id)
                if not cur_ses:
                    break
                if cur_ses["status"] == "STOP_REQUESTED":
                    repo.update_session_status(session_id, "STOPPED", notes="Detención controlada completada.")
                    break
                if cur_ses["status"] in ("PAUSED", "PAUSED_AUTH_REQUIRED", "STOPPED"):
                    break

                target_states = ["READY_TO_APPLY"] if dry_run else ["SIMULATED_READY", "READY_TO_APPLY"]
                next_batch = repo.list_applications(
                    session_id=session_id if dry_run else None,
                    states=target_states,
                    portals=portals,
                    include_legacy=False,
                    limit=1,
                )
                if not next_batch:
                    break
                app = next_batch[0]

                app_id = int(app["id"])
                portal_name = app["portal"].lower()
                adapter = ADAPTERS.get(portal_name)
                if not adapter:
                    continue

                repo.touch_heartbeat(session_id, worker_pid=my_pid)

                # --- MODO POSTULAR ASISTIDO POR PORTAL ---
                if not dry_run:
                    flow = get_portal_flow(portal_name)
                    if flow and portal_name in ("chiletrabajos", "computrabajo"):
                        flow_res = await flow.postulate_assisted(
                            page=page,
                            offer=app,
                            repo=repo,
                            current_idx=processed_count + 1,
                            total_offers=max_apps,
                            session_id=session_id,
                        )
                        if flow_res.get("status") == "CONFIRMED":
                            confirmed_count += 1
                        processed_count += 1
                        continue
                    elif flow:
                        flow_res = await flow.postulate_assisted(
                            page=page,
                            offer=app,
                            repo=repo,
                            current_idx=processed_count + 1,
                            total_offers=max_apps,
                            session_id=session_id,
                        )
                        repo.transition_application_state(
                            app_id,
                            "NEEDS_HUMAN_REVIEW",
                            reason=flow_res.get("message", "Portal en investigación o no adaptado."),
                        )
                        processed_count += 1
                        continue

                repo.transition_application_state(
                    app_id,
                    "APPLYING",
                    reason=f"Iniciando ejecución en modo {mode}",
                    last_step_reached="PREFLIGHT",
                    increment_attempt=True,
                )

                cv_sel = select_cv_and_salary(
                    title=app["title"],
                    description=app.get("description_text") or "",
                    salary_strategy=salary_strategy,
                )
                if app.get("selected_cv_base"):
                    cvs_dir = get_cvs_dir()
                    cand_pdf = cvs_dir / f"{app['selected_cv_base']}.pdf"
                    cand_docx = cvs_dir / f"{app['selected_cv_base']}.docx"
                    if cand_pdf.exists():
                        cv_sel.cv_base = app["selected_cv_base"]
                        cv_sel.target_name = app.get("selected_cv_target") or cv_sel.target_name
                        cv_sel.src_pdf = cand_pdf
                        if cand_docx.exists():
                            cv_sel.src_docx = cand_docx
                        cv_sel.requires_human_confirmation = False
                precomputed_answers = json.loads(app.get("killer_answers_json") or "{}")
                cover_letter = app.get("cover_letter_text") or build_cover_letter(
                    app["company_name"], app["title"], int(app["expected_salary_clp"])
                )

                default_loc = get_candidate_profile().get("residence") or "Remote"

                # Preparar carpeta de expediente para la oferta
                dossier_info = prepare_dossier_files(
                    company=app["company_name"],
                    title=app["title"],
                    portal=portal_name,
                    offer_url=app["canonical_url"],
                    external_job_id=app.get("external_job_id"),
                    location=app.get("location") or default_loc,
                    salary_clp=int(app["expected_salary_clp"]),
                    src_docx=cv_sel.src_docx,
                    src_pdf=cv_sel.src_pdf,
                    cv_target_name=cv_sel.target_name,
                    cover_letter=cover_letter,
                    answered_questions=precomputed_answers,
                    status_label="Simulación (Dry-Run)" if dry_run else "Procesando envío",
                    cv_selection_reason=cv_sel.selection_reason,
                    app_id=app_id,
                )
                dossier_dir: Path = dossier_info["folder_path"]

                try:
                    outcome = await asyncio.wait_for(
                        adapter.apply_to_offer(
                            page=page,
                            offer=app,
                            cv_pdf_path=dossier_info["pdf_path"],
                            salary_clp=int(app["expected_salary_clp"]),
                            cover_letter=cover_letter,
                            precomputed_answers=precomputed_answers,
                            dossier_dir=dossier_dir,
                            dry_run=dry_run,
                        ),
                        timeout=float(prefs.get("limits", {}).get("task_hard_timeout_sec", 60)),
                    )
                except asyncio.TimeoutError:
                    repo.transition_application_state(
                        app_id,
                        "FAILED_RETRYABLE",
                        reason="Timeout duro de 60s excedido durante la navegación/formulario",
                        last_step_reached="FORM_LOAD",
                        error_code="TASK_TIMEOUT",
                        error_message="Timeout de 60s excedido",
                    )
                    processed_count += 1
                    continue
                except Exception as exc:
                    repo.transition_application_state(
                        app_id,
                        "FAILED_RETRYABLE",
                        reason=f"Excepción de navegador: {exc}",
                        last_step_reached="FORM_LOAD",
                        error_code="BROWSER_EXCEPTION",
                        error_message=str(exc),
                    )
                    processed_count += 1
                    continue

                # Actualizar archivos de expediente con las respuestas reales contestadas
                status_human = {
                    "CONFIRMED": "Postulación enviada y confirmada",
                    "SIMULATED_READY": "Simulada (Pendiente de aprobación para envío real)",
                    "SUBMITTED_UNVERIFIED": "Enviada - Pendiente de verificación en portal",
                    "ALREADY_APPLIED_PORTAL": "Ya postulada previamente en el portal",
                    "CLOSED_ON_PORTAL": "CERRADA EN PORTAL",
                    "NEEDS_HUMAN_REVIEW": "Esperando respuestas personalizadas de Antigravity (IA) / Revisión",
                    "FAILED_VALIDATION": "Rechazada por validación de formulario",
                }.get(outcome.status_code, outcome.status_code)

                prepare_dossier_files(
                    company=app["company_name"],
                    title=app["title"],
                    portal=portal_name,
                    offer_url=app["canonical_url"],
                    external_job_id=app.get("external_job_id"),
                    location=app.get("location") or default_loc,
                    salary_clp=int(app["expected_salary_clp"]),
                    src_docx=cv_sel.src_docx,
                    src_pdf=cv_sel.src_pdf,
                    cv_target_name=cv_sel.target_name,
                    cover_letter=cover_letter,
                    answered_questions=outcome.answered_questions if not outcome.extracted_questions else precomputed_answers,
                    status_label=status_human,
                    cv_selection_reason=cv_sel.selection_reason,
                    app_id=app_id,
                )

                if outcome.form_screenshot_path and outcome.form_screenshot_path.exists():
                    repo.add_evidence(app_id, "FORM_SCREENSHOT", outcome.form_screenshot_path)
                if outcome.final_screenshot_path and outcome.final_screenshot_path.exists():
                    ev_type = "CONFIRMATION_SCREENSHOT" if outcome.status_code == "CONFIRMED" else "UNCERTAIN_SCREENSHOT"
                    repo.add_evidence(
                        app_id,
                        ev_type,
                        outcome.final_screenshot_path,
                        verified_valid=(outcome.status_code == "CONFIRMED"),
                    )

                if outcome.status_code == "AUTH_EXPIRED":
                    repo.transition_application_state(
                        app_id,
                        "READY_TO_APPLY",
                        reason=outcome.reason,
                        last_step_reached="PREFLIGHT",
                        error_code="AUTH_EXPIRED",
                        error_message=outcome.reason,
                    )
                    repo.update_session_status(session_id, "PAUSED_AUTH_REQUIRED", notes=outcome.reason)
                    break

                step_reached = (
                    "WAITING_AI_ANSWERS"
                    if outcome.extracted_questions
                    else (
                        "POST_VERIFY"
                        if outcome.status_code in ("CONFIRMED", "SUBMITTED_UNVERIFIED", "FAILED_VALIDATION")
                        else "FORM_FILL"
                    )
                )
                extra_db_updates: dict[str, Any] = {
                    "folder_path": str(dossier_dir),
                    "folder_name": dossier_dir.name,
                }
                if outcome.extracted_questions:
                    extra_db_updates["killer_questions_json"] = json.dumps(outcome.extracted_questions, ensure_ascii=False)
                    waiting_ai_count += 1
                else:
                    extra_db_updates["killer_answers_json"] = json.dumps(outcome.answered_questions, ensure_ascii=False)

                repo.transition_application_state(
                    app_id,
                    outcome.status_code,
                    reason=outcome.reason,
                    last_step_reached=step_reached,
                    extra_updates=extra_db_updates,
                )

                try:
                    append_submission_audit_log(
                        session_id=session_id,
                        application_id=app_id,
                        portal=portal_name,
                        external_job_id=app.get("external_job_id"),
                        company=app["company_name"],
                        title=app["title"],
                        offer_url=app["canonical_url"],
                        final_url=outcome.final_url,
                        status_code=outcome.status_code,
                        reason=outcome.reason,
                        step_reached=step_reached,
                        selected_cv_base=cv_sel.cv_base,
                        selected_cv_pdf=f"{cv_sel.target_name}.pdf",
                        cv_selection_reason=cv_sel.selection_reason,
                        salary_clp=int(app["expected_salary_clp"]),
                        cover_letter=cover_letter,
                        extracted_questions=outcome.extracted_questions,
                        written_answers=outcome.answered_questions if not outcome.extracted_questions else precomputed_answers,
                        dossier_dir=dossier_dir,
                        form_screenshot=outcome.form_screenshot_path,
                        final_screenshot=outcome.final_screenshot_path,
                    )
                except Exception:
                    pass

                processed_count += 1
                if outcome.status_code == "CONFIRMED":
                    confirmed_count += 1

            await update_browser_hud(
                page,
                portal="RESUMEN LOTE",
                status="Finalizado",
                offer_title=f"Procesadas: {processed_count} · Confirmadas: {confirmed_count}",
                detail=f"Ofertas esperando redacción IA de Antigravity: {waiting_ai_count}",
                bring_to_front=False,
            )

        finally:
            # No cerrar la pestaña para que el usuario conserve el HUD y la vista en su ventana de Chrome
            pass

    final_ses = repo.get_session(session_id)
    if final_ses and final_ses["status"] == "RUNNING":
        repo.update_session_status(
            session_id,
            "COMPLETED",
            notes=f"Sesión finalizada. Procesadas: {processed_count}, Confirmadas: {confirmed_count}.",
        )
    elif final_ses and final_ses["status"] == "STOP_REQUESTED":
        repo.update_session_status(session_id, "STOPPED", notes="Sesión detenida por solicitud del usuario.")

    build_consolidated_report(repo=repo, write_markdown=True)
    repo.backup_to_workspace()
    return {
        "session_id": session_id,
        "processed_count": processed_count,
        "confirmed_count": confirmed_count,
        "final_status": (repo.get_session(session_id) or {}).get("status"),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Worker persistente del Buscador de Empleo Automático")
    parser.add_argument("--session-id", required=True, help="Identificador de la sesión de trabajo")
    parser.add_argument("--db-path", default=None, help="Ruta opcional a jobs.db")
    args = parser.parse_args()

    res = asyncio.run(run_worker_session(session_id=args.session_id, db_path=args.db_path))
    print(json.dumps(res, ensure_ascii=False))


if __name__ == "__main__":
    main()
