"""Asistente interactivo 1 a 1: prepara formularios en Chrome CDP y espera el clic manual del usuario."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

from job_automator.adapters.chiletrabajos import ChiletrabajosAdapter
from job_automator.adapters.computrabajo import ComputrabajoAdapter
from job_automator.adapters.getonboard import GetOnBoardAdapter
from job_automator.adapters.indeed import IndeedAdapter
from job_automator.adapters.laborum import LaborumAdapter
from job_automator.browser.cdp_manager import discover_cdp_endpoint
from job_automator.browser.guards import safe_page_screenshot, verify_post_submit_confirmation
from job_automator.browser.hud import update_browser_hud
from job_automator.config import get_cvs_dir, get_output_dir
from job_automator.db.repository import JobRepository
from job_automator.intelligence.answer_generator import build_cover_letter
from job_automator.intelligence.ats_cv_generator import async_generate_ats_cv_for_job
from job_automator.intelligence.cv_selector import select_cv_and_salary
from job_automator.intelligence.fact_checker import validate_text_truthfulness
from job_automator.reporting.dossier_writer import clean_folder_name, prepare_dossier_files

ADAPTERS = {
    "chiletrabajos": ChiletrabajosAdapter(),
    "computrabajo": ComputrabajoAdapter(),
    "laborum": LaborumAdapter(),
    "indeed": IndeedAdapter(),
    "getonboard": GetOnBoardAdapter(),
}


async def prepare_offer_in_browser(app_id: int) -> dict[str, Any]:
    """Carga y rellena el formulario de la oferta en Chrome sin pulsar el botón de envío."""
    repo = JobRepository()
    app = repo.get_application(app_id)
    if not app:
        return {"ok": False, "error": f"Postulación {app_id} no encontrada en la base de datos."}

    cdp_info = discover_cdp_endpoint()
    if not cdp_info.available or not cdp_info.ws_url:
        return {
            "ok": False,
            "error": "Chrome no está disponible en puerto 9222. Abre Chrome con: chrome.exe --remote-debugging-port=9222",
        }

    portal_name = app["portal"].lower()
    adapter = ADAPTERS.get(portal_name)
    if not adapter:
        return {"ok": False, "error": f"Portal {portal_name} no soportado."}

    salary_strategy = "FAST_HIRE"
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

    # Generar CV personalizado ATS de 1 página con detención estricta
    ats_result = await async_generate_ats_cv_for_job(
        job_title=app["title"],
        job_description=app.get("description_text") or "",
        company_name=app["company_name"],
        app_id=app_id,
    )
    if not ats_result.ok or not ats_result.pdf_path:
        repo.transition_application_state(
            app_id,
            "FAILED_RETRYABLE",
            reason=f"ERROR_CV_GENERATION: {ats_result.error}",
            last_step_reached="CV_GENERATION",
        )
        return {
            "ok": False,
            "app_id": app_id,
            "status": "ERROR_CV_GENERATION",
            "message": f"Detención estricta: fallo al generar CV personalizado ATS ({ats_result.error}).",
        }

    cv_sel.src_pdf = ats_result.pdf_path
    cv_sel.target_name = ats_result.pdf_path.stem

    precomputed_answers = json.loads(app.get("killer_answers_json") or "{}")
    expected_salary = int(app.get("expected_salary_clp") or cv_sel.salary_clp)
    cover_letter = app.get("cover_letter_text") or build_cover_letter(
        app["company_name"], app["title"], expected_salary
    )

    dossier_info = prepare_dossier_files(
        company=app["company_name"],
        title=app["title"],
        portal=portal_name,
        offer_url=app["canonical_url"],
        external_job_id=app.get("external_job_id"),
        location=app.get("location") or "Chile",
        salary_clp=expected_salary,
        src_docx=cv_sel.src_docx,
        src_pdf=cv_sel.src_pdf,
        cv_target_name=cv_sel.target_name,
        cover_letter=cover_letter,
        answered_questions=precomputed_answers,
        status_label="Preparando para envío manual",
        cv_selection_reason=f"CV ATS compilado (Score: {ats_result.ats_score}%)" if ats_result.ok else cv_sel.selection_reason,
        app_id=app_id,
    )
    dossier_dir: Path = dossier_info["folder_path"]
    repo.transition_application_state(
        app_id,
        "READY_TO_APPLY",
        reason=f"CV ATS generado ({ats_result.ats_score}%) y expediente preparado para revisión.",
        last_step_reached="CV_GENERATION",
        extra_updates={
            "folder_name": dossier_info["folder_path"].name,
            "folder_path": str(dossier_info["folder_path"]),
            "selected_cv_base": ats_result.pdf_path.stem,
            "selected_cv_target": ats_result.pdf_path.stem,
        },
    )

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_info.ws_url, timeout=20000)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()

        # Reutilizar pestaña si ya existe una con el dominio principal del portal, URL o HUD
        from urllib.parse import urlparse
        page = None
        canon_url = (app.get("canonical_url") or "").lower()
        for p_tab in context.pages:
            try:
                if p_tab.is_closed():
                    continue
                t_url = (p_tab.url or "").lower()
                host = urlparse(t_url).netloc.lower()
                if not host or "google" in host or "doubleclick" in host or "facebook" in host:
                    continue
                if canon_url and (canon_url in t_url or t_url in canon_url):
                    page = p_tab
                    break
                if portal_name in host:
                    page = p_tab
                    break
                has_hud = await asyncio.wait_for(
                    p_tab.evaluate("() => !!(document.getElementById('antigravity-live-hud') || document.getElementById('antigravity-user-action-banner'))"),
                    timeout=1.0,
                )
                if has_hud:
                    page = p_tab
                    break
            except Exception:
                continue

        if page is None:
            if context.pages and (context.pages[0].url == "about:blank" or not context.pages[0].url.startswith("http")):
                page = context.pages[0]
            else:
                page = await context.new_page()

        outcome = await adapter.apply_to_offer(
            page=page,
            offer=app,
            cv_pdf_path=dossier_info["pdf_path"],
            salary_clp=expected_salary,
            cover_letter=cover_letter,
            precomputed_answers=precomputed_answers,
            dossier_dir=dossier_dir,
            dry_run=True,
        )

        # Destacar en pantalla para el usuario
        await page.evaluate(
            """() => {
                let banner = document.getElementById('antigravity-user-action-banner');
                if (!banner) {
                    banner = document.createElement('div');
                    banner.id = 'antigravity-user-action-banner';
                    banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:999999;background:#10b981;color:#ffffff;font-size:16px;font-weight:bold;text-align:center;padding:12px;box-shadow:0 4px 12px rgba(0,0,0,0.3);';
                    document.body.prepend(banner);
                }
                banner.innerText = '✅ FORMULARIO LISTO Y CV ADJUNTADO: Revisa los datos y haz clic en Enviar postulación.';
            }"""
        )

        raw_kq = json.loads(app.get("killer_questions_json") or "[]")
        questions_list = list(outcome.extracted_questions or raw_kq)

        # Si aún no hay preguntas extraídas, intentar detectarlas en la página activa o en modales/popups
        if not questions_list:
            active_pages = [p_cand for p_cand in context.pages if not p_cand.is_closed()]
            for p_cand in active_pages:
                try:
                    dom_qs = await p_cand.evaluate(
                        """() => {
                            const qs = [];
                            const elements = document.querySelectorAll('textarea, input[name^="q"], input[name^="ans"]');
                            elements.forEach((el) => {
                                const parent = el.closest('.form_group, .box_input, .b_input, li, div');
                                let text = '';
                                if (parent) {
                                    const lbl = parent.querySelector('label, p.fs16, p.title, h3, h4, span.fs16');
                                    if (lbl && lbl !== el && lbl.innerText.trim()) text = lbl.innerText.trim();
                                }
                                if (!text && el.id) {
                                    const lblFor = document.querySelector(`label[for="${el.id}"]`);
                                    if (lblFor && lblFor.innerText.trim()) text = lblFor.innerText.trim();
                                }
                                if (text && !qs.includes(text)) qs.push(text);
                            });
                            return qs;
                        }"""
                    )
                    if dom_qs:
                        questions_list.extend(dom_qs)
                        break
                except Exception:
                    pass

        return {
            "ok": True,
            "app_id": app_id,
            "title": app["title"],
            "company": app["company_name"],
            "portal": portal_name,
            "url": app["canonical_url"],
            "description_text": app.get("description_text") or "",
            "cv_attached": dossier_info["pdf_path"].name,
            "custom_ats_cv_path": str(dossier_info["pdf_path"]),
            "ats_score": ats_result.ats_score if ats_result.ok else 0,
            "matched_keywords": ats_result.matched_keywords if ats_result.ok else [],
            "salary_clp": expected_salary,
            "status_code": outcome.status_code,
            "questions": questions_list,
            "screenshot": str(outcome.form_screenshot_path) if outcome.form_screenshot_path else None,
        }


# Alias oficial para el flujo interactivo asistido
prepare_offer_for_review = prepare_offer_in_browser


async def inject_tailored_answers(app_id: int, answers_by_question: dict[str, str]) -> dict[str, Any]:
    """
    Inyecta respuestas a medida validadas por el Fact-Checker en la base de datos y en la pestaña activa de Chrome vía CDP.
    """
    repo = JobRepository()
    app = repo.get_application(app_id)
    if not app:
        return {"ok": False, "error": f"Postulación {app_id} no encontrada en la base de datos."}

    # 1. Fact-check estricto de cada respuesta (evita invención o exageración de años de experiencia/certificaciones)
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
            "message": "Se rechazaron respuestas porque contienen afirmaciones no autorizadas en el perfil real del candidato.",
        }

    # 2. Persistir en SQLite en caché de respuestas y en killer_answers_json
    existing_answers = {
        k.strip().strip('"').strip("'"): v
        for k, v in json.loads(app.get("killer_answers_json") or "{}").items()
    }
    for q_label, answer_text in answers_by_question.items():
        clean_key = q_label.strip().strip('"').strip("'")
        existing_answers[clean_key] = answer_text
        repo.set_cached_answer("KILLER_ANSWER", clean_key, answer_text, source="MCP_AGENT")

    repo.transition_application_state(
        app_id,
        "READY_TO_APPLY",
        reason="Respuestas a medida inyectadas por el Agente y validadas por Fact-Checker.",
        extra_updates={"killer_answers_json": json.dumps(existing_answers, ensure_ascii=False)},
    )

    # 3. Inyección en vivo en el navegador si Chrome CDP está conectado
    injected_in_browser = False
    screenshot_path = None
    cdp_info = discover_cdp_endpoint()
    if cdp_info.available and cdp_info.ws_url:
        try:
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(cdp_info.ws_url, timeout=10000)
                context = browser.contexts[0] if browser.contexts else None
                if context:
                    from urllib.parse import urlparse
                    portal_name = app["portal"].lower()
                    target_page = None
                    for page in context.pages:
                        if page.is_closed():
                            continue
                        host = urlparse((page.url or "").lower()).netloc.lower()
                        if portal_name in host and "google" not in host and "doubleclick" not in host:
                            target_page = page
                            break
                    if not target_page and context.pages:
                        target_page = context.pages[0]

                    if target_page and not target_page.is_closed():
                        await target_page.bring_to_front()
                        inputs = await target_page.query_selector_all("textarea, input[type='text'], input:not([type])")
                        norm_ans = {k.strip().strip('"').strip("'").lower(): v for k, v in answers_by_question.items()}

                        for idx, el in enumerate(inputs):
                            el_name = (await el.get_attribute("name") or "").lower()
                            el_id = (await el.get_attribute("id") or "").lower()
                            if el_name in ("q_search", "search", "palabra", "query", "email", "mail"):
                                continue

                            q_label = await el.evaluate(
                                """el => {
                                    const parent = el.closest('.form_group, .box_input, .b_input, li, div');
                                    if (parent) {
                                        const lbl = parent.querySelector('label, p.fs16, p.title, h3, h4, span.fs16');
                                        if (lbl && lbl !== el && lbl.innerText.trim()) return lbl.innerText.trim();
                                    }
                                    if (el.id) {
                                        const lblFor = document.querySelector(`label[for="${el.id}"]`);
                                        if (lblFor && lblFor.innerText.trim()) return lblFor.innerText.trim();
                                    }
                                    return '';
                                }"""
                            )
                            clean_lbl = (q_label or "").strip().lower()
                            matched_val = (
                                norm_ans.get(clean_lbl)
                                or norm_ans.get(el_name)
                                or norm_ans.get(el_id)
                                or norm_ans.get(f"q{idx}")
                                or norm_ans.get(f"p{idx+1}")
                            )
                            if not matched_val:
                                for k, v in norm_ans.items():
                                    if k in clean_lbl or clean_lbl in k or k == el_name or k == el_id:
                                        matched_val = v
                                        break
                            if matched_val:
                                await el.fill(matched_val)
                                injected_in_browser = True

                        dossier_dir = Path(app.get("folder_path") or (get_output_dir() / clean_folder_name(app["company_name"], app["title"])))
                        dossier_dir.mkdir(parents=True, exist_ok=True)
                        form_shot = dossier_dir / "pantallazo_respuestas_formulario.png"
                        await safe_page_screenshot(target_page, form_shot)
                        screenshot_path = str(form_shot)

                        # Actualizar banner HUD
                        await target_page.evaluate(
                            """() => {
                                let b = document.getElementById('antigravity-flow-banner') || document.getElementById('antigravity-user-action-banner');
                                if (b) {
                                    b.style.background = '#0284c7';
                                    b.innerHTML = '📝 <b>Respuestas a medida inyectadas</b> en el formulario. Revisa los campos y pulsa Enviar para finalizar.';
                                }
                            }"""
                        )
        except Exception:
            pass

    return {
        "ok": True,
        "app_id": app_id,
        "new_state": "READY_TO_APPLY",
        "injected_in_browser": injected_in_browser,
        "screenshot": screenshot_path,
        "answers_count": len(answers_by_question),
    }


async def verify_and_record_submission(app_id: int) -> dict[str, Any]:
    """Verifica que el usuario haya hecho clic en enviar, guarda evidencias y actualiza estado."""
    repo = JobRepository()
    app = repo.get_application(app_id)
    if not app:
        return {"ok": False, "error": f"Postulación {app_id} no encontrada."}

    cdp_info = discover_cdp_endpoint()
    if not cdp_info.available or not cdp_info.ws_url:
        return {"ok": False, "error": "Chrome CDP no disponible."}

    portal_name = app["portal"].lower()

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_info.ws_url, timeout=20000)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()

        # Buscar la pestaña activa de la oferta
        page = None
        for p_tab in context.pages:
            try:
                if portal_name in p_tab.url.lower():
                    page = p_tab
                    break
            except Exception:
                continue

        if not page:
            page = context.pages[0] if context.pages else None

        if not page:
            return {"ok": False, "error": "No se encontró pestaña abierta con el portal."}

        body_text = await page.inner_text("body")
        verdict = verify_post_submit_confirmation(portal_name, page.url, body_text)

        dossier_dir = Path(app.get("folder_path") or (get_output_dir() / clean_folder_name(app["company_name"], app["title"])))
        dossier_dir.mkdir(parents=True, exist_ok=True)

        if verdict.ok:
            conf_shot = dossier_dir / "confirmacion_postulacion.png"
            await safe_page_screenshot(page, conf_shot)

            repo.transition_application_state(
                app_id,
                "CONFIRMED",
                reason=f"Confirmada manualmente por el usuario. {verdict.reason}",
                last_step_reached="POST_VERIFY",
            )
            repo.add_evidence(app_id, "CONFIRMATION_SCREENSHOT", conf_shot, verified_valid=True)

            # Actualizar checkbox en POSTULACIONES_SIMULADAS_MANUALES.md
            md_path = get_output_dir() / "POSTULACIONES_SIMULADAS_MANUALES.md"
            if md_path.exists():
                content = md_path.read_text(encoding="utf-8")
                # Reemplazar checkbox correspondiente al ID
                pat = rf"(###\s*<a id=\"oferta-{app_id}\">.*?-\s*\[)\s*(\]\s*\*\*Postulado manualmente\*\*)"
                sub_res, count = re.subn(pat, r"\g<1>X\g<2>", content, flags=re.DOTALL)
                if count > 0:
                    md_path.write_text(sub_res, encoding="utf-8")

            return {
                "ok": True,
                "confirmed": True,
                "app_id": app_id,
                "reason": verdict.reason,
                "screenshot": str(conf_shot),
            }
        else:
            return {
                "ok": False,
                "confirmed": False,
                "app_id": app_id,
                "reason": verdict.reason,
                "note": "Aún no se detecta la confirmación en pantalla. Asegúrate de haber pulsado el botón de envío.",
            }


# Alias oficial para el flujo interactivo asistido
confirm_application_submission = verify_and_record_submission
