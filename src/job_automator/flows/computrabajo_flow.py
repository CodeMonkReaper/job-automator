"""Flujo interactivo asistido exclusivo para Computrabajo.

Reglas ratificadas con el usuario:
1. Human-in-the-Loop: Sincronización en segundo plano + precarga de Killer Questions;
   el usuario realiza el clic final de envío.
2. Política de 1 solo CV: En segundo plano se elimina cualquier CV previo y se sube
   exclusivamente el CV específico requerido.
3. Relleno asistido de Killer Questions: Pre-completa preguntas abiertas y radios
   (invirtiendo 0=Sí / 1=No en Computrabajo) según candidate_profile.json sin inventar datos.
4. Transición suave con cuenta regresiva: Al confirmar, toma pantallazo, actualiza SQLite
   y muestra cuenta regresiva de 5 segundos antes de avanzar.
5. Detección de ofertas externas: Clasifica como EXTERNAL_REDIRECT / revisión y continúa
   con la siguiente oferta interna.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page

from job_automator.browser.guards import (
    inspect_html_for_security_barriers,
    safe_page_screenshot,
    verify_post_submit_confirmation,
    verify_pre_apply_offer_state,
)
from job_automator.config import get_candidate_profile, get_cvs_dir, get_output_dir, get_candidate_profile
from job_automator.db.repository import JobRepository
from job_automator.flows.base_flow import BasePortalFlow
from job_automator.intelligence.answer_generator import (
    answer_question_deterministically,
    build_cover_letter,
    generate_tailored_application_answer,
)
from job_automator.intelligence.ats_cv_generator import (
    async_generate_ats_cv_for_job,
    generate_ats_cv_for_job,
    get_ats_cv_filename,
)
from job_automator.intelligence.cv_selector import select_cv_and_salary
from job_automator.reporting.dossier_writer import clean_folder_name, prepare_dossier_files


async def _sync_cv_background(
    browser_context: BrowserContext,
    cv_pdf_path: Path,
    dossier_dir: Path | None = None,
) -> bool:
    """Sincroniza el CV en segundo plano manteniendo la regla de UN SOLO CV en la cuenta.

    No altera la pestaña activa donde el usuario está navegando.
    """
    if not cv_pdf_path.exists():
        return False

    bg_page = await browser_context.new_page()
    try:
        await bg_page.goto(
            "https://candidato.cl.computrabajo.com/candidate/cv/uploadcv",
            wait_until="domcontentloaded",
            timeout=25000,
        )
        await bg_page.wait_for_timeout(2000)

        # 1. Obtener lista de CVs actualmente subidos
        uploaded_items = await bg_page.evaluate(
            """() => {
                const rows = Array.from(document.querySelectorAll('ul.fila_tabla'));
                return rows.map((r, idx) => {
                    const radio = r.querySelector('input[type="radio"]');
                    const fileLink = r.querySelector('.it-file');
                    if (!radio) return null;
                    radio.setAttribute('data-ag-radio-idx', String(idx));
                    return {
                        id: radio.id || `ag-radio-${idx}`,
                        idx: idx,
                        checked: radio.checked,
                        text: fileLink ? fileLink.innerText.trim() : r.innerText.trim()
                    };
                }).filter(Boolean);
            }"""
        )

        exact_stem = cv_pdf_path.stem.lower().replace("_actualizado", "")
        target_name_low = cv_pdf_path.name.lower()
        is_custom_ats = "personalizado_ats" in target_name_low or "ats" in target_name_low

        # Si ya existe exactamente 1 CV, coincide con el solicitado y está marcado, y NO es un CV personalizado dinámico, terminamos rápido
        if not is_custom_ats and len(uploaded_items) == 1:
            item = uploaded_items[0]
            if (target_name_low in item["text"].lower() or exact_stem in item["text"].lower()) and item["checked"]:
                if dossier_dir:
                    dossier_dir.mkdir(parents=True, exist_ok=True)
                    await safe_page_screenshot(bg_page, dossier_dir / "pantallazo_cv_activo_computrabajo.png")
                return True

        # 2. Si hay 10 CVs (o aviso de máximo), eliminar uno desmarcado para asegurar cupo
        page_text = await bg_page.inner_text("body")
        max_reached = "máximo de documentos" in page_text.lower() or "maximo de documentos" in page_text.lower() or len(uploaded_items) >= 10
        if max_reached:
            deleted = await bg_page.evaluate(
                """() => {
                    const rows = Array.from(document.querySelectorAll('ul.fila_tabla'));
                    for (let i = rows.length - 1; i >= 0; i--) {
                        const r = rows[i];
                        const radio = r.querySelector('input[type="radio"]');
                        const del = r.querySelector('#it-delete, .papelera, a.delete');
                        if (radio && !radio.checked && del) {
                            del.click();
                            const bt = document.getElementById('btDelete');
                            if (bt) {
                                bt.click();
                                return true;
                            }
                        }
                    }
                    return false;
                }"""
            )
            if deleted:
                await bg_page.wait_for_timeout(2500)
                await bg_page.wait_for_load_state("domcontentloaded")

        # 3. Verificar si el CV exacto ya está presente en la lista (solo para CVs estáticos)
        target_item = None
        if not is_custom_ats:
            for item in uploaded_items:
                t_low = item["text"].lower()
                if target_name_low in t_low or exact_stem in t_low:
                    target_item = item
                    break

        # Si es un CV ATS personalizado o no está presente, subir el PDF generado
        if is_custom_ats or not target_item:
            file_input = await bg_page.query_selector(
                "#ContainerGeneralOverWrite_ContainerGeneral_fuUploadCv, input[type='file']"
            )
            if file_input:
                await file_input.set_input_files(str(cv_pdf_path))
                await bg_page.wait_for_timeout(3000)
                await bg_page.wait_for_load_state("domcontentloaded")
                uploaded_items = await bg_page.evaluate(
                    """() => {
                        const rows = Array.from(document.querySelectorAll('ul.fila_tabla'));
                        return rows.map((r, idx) => {
                            const radio = r.querySelector('input[type="radio"]');
                            const fileLink = r.querySelector('.it-file');
                            if (!radio) return null;
                            radio.setAttribute('data-ag-radio-idx', String(idx));
                            return {
                                id: radio.id || `ag-radio-${idx}`,
                                idx: idx,
                                checked: radio.checked,
                                text: fileLink ? fileLink.innerText.trim() : r.innerText.trim()
                            };
                        }).filter(Boolean);
                    }"""
                )
                for item in uploaded_items:
                    t_low = item["text"].lower()
                    if target_name_low in t_low or exact_stem in t_low:
                        target_item = item
                        break
                # Si recién se subió y es personalizado, el primer ítem suele ser el recién subido
                if not target_item and uploaded_items:
                    target_item = uploaded_items[0]

        # 4. Asegurar que el CV objetivo quede marcado como activo
        if target_item:
            target_id = target_item["id"]
            await bg_page.evaluate(
                """(tid) => {
                    let el = document.getElementById(tid);
                    if (!el && tid.startsWith('ag-radio-')) {
                        const idx = tid.replace('ag-radio-', '');
                        el = document.querySelector(`input[type="radio"][data-ag-radio-idx="${idx}"]`);
                    }
                    if (el) {
                        el.click();
                        el.checked = true;
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }
                }""",
                target_id,
            )
            await bg_page.wait_for_timeout(1000)

        if dossier_dir:
            dossier_dir.mkdir(parents=True, exist_ok=True)
            await safe_page_screenshot(bg_page, dossier_dir / "pantallazo_cv_activo_computrabajo.png")
        return True
    except Exception:
        return False
    finally:
        try:
            await bg_page.close()
        except Exception:
            pass


async def _fill_killer_questions_if_present(
    page: Page,
    salary_clp: int,
    cover_letter: str,
    precomputed_answers: dict[str, str],
    offer_title: str = "",
    company: str = "",
    offer_desc: str = "",
) -> bool:
    """Detecta y pre-rellena preguntas filtro en /candidate/kq o modales de Computrabajo.

    Aplica la generación contextualizada basada en candidate_profile.json y enfoque técnico detallado.
    En Computrabajo, en radio buttons binarios, value='0' es 'Sí' y value='1' es 'No'.
    """
    has_kq = await page.evaluate(
        """() => {
            const isKqUrl = window.location.href.toLowerCase().includes('/candidate/kq') ||
                            window.location.href.toLowerCase().includes('killerquestions');
            const hasForm = !!document.querySelector('#btnKiller, #btnApply, input[value="Enviar mi CV"], textarea, .box_input');
            return isKqUrl || hasForm;
        }"""
    )
    if not has_kq:
        return False

    norm_answers = {
        k.strip().strip('"').strip("'").lower(): v
        for k, v in precomputed_answers.items()
    }

    # 1. Rellenar Textareas abiertas
    textareas = await page.query_selector_all("textarea")
    for idx, ta in enumerate(textareas):
        try:
            val = await ta.input_value()
            if val and val.strip():
                continue
            q_label = await ta.evaluate(
                """el => {
                    const parent = el.closest('.form_group, .box_input, .b_input, li, div');
                    if (parent) {
                        const lbl = parent.querySelector('label, p.fs16, p.title, h3, h4, span.fs16');
                        if (lbl && lbl !== el && lbl.innerText.trim()) return lbl.innerText.trim();
                        const clone = parent.cloneNode(true);
                        const taC = clone.querySelector('textarea');
                        if (taC) taC.remove();
                        const t = clone.innerText.trim();
                        if (t.length > 5) return t;
                    }
                    return '';
                }"""
            )
            clean_label = q_label.strip()
            clean_norm = clean_label.lower()

            ans = (
                norm_answers.get(clean_norm)
                or norm_answers.get(f"p{idx + 1}")
                or norm_answers.get(f"q{idx}")
                or generate_tailored_application_answer(
                    question_text=clean_label,
                    offer_title=offer_title,
                    company=company,
                    salary_clp=salary_clp,
                    offer_desc=offer_desc,
                )
            )
            if not ans and any(w in clean_norm for w in ("carta", "presentación", "presentacion", "motivo", "por qué")):
                ans = cover_letter

            if ans and len(ans) > 480:
                clean_trimmed = ans[:480]
                last_period = clean_trimmed.rfind(".")
                if last_period > 250:
                    ans = clean_trimmed[:last_period + 1]
                else:
                    ans = clean_trimmed

            await ta.fill(ans)
        except Exception:
            pass

    # 2. Resolver preguntas binarias de selección y radios (value='0' es Sí, value='1' es No en Computrabajo)
    await page.evaluate(
        r"""() => {
            const radioNames = new Set(Array.from(document.querySelectorAll('input[type="radio"]')).map(r => r.name));
            for (const name of radioNames) {
                const radios = Array.from(document.querySelectorAll(`input[type="radio"][name="${name}"]`));
                const isChecked = radios.some(r => r.checked);
                if (isChecked) continue;

                const m = name.match(/KillerQuestions\[(\d+)\]/i);
                let qTitle = '';
                if (m) {
                    const tEl = document.querySelector(`input[name="KillerQuestions[${m[1]}].Title"]`);
                    if (tEl) qTitle = tEl.value;
                }
                const container = radios[0].closest('.group, .form_group, .box_input, .b_input, li, div') || document.body;
                const fullText = (qTitle + ' ' + container.innerText).toLowerCase();

                let chosenVal = '0'; // default SÍ
                if (fullText.includes('licencia') || fullText.includes('conducir') || fullText.includes('auto') || 
                    fullText.includes('minero') || fullText.includes('minería') || fullText.includes('zona norte')) {
                    chosenVal = '1'; // NO
                }

                const targetRadio = radios.find(r => r.value === chosenVal) || radios[0];
                if (targetRadio) {
                    targetRadio.click();
                    targetRadio.checked = true;
                    targetRadio.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }
        }"""
    )
    return True


class ComputrabajoFlow(BasePortalFlow):
    """Flujo interactivo asistido exclusivo para Computrabajo."""

    portal_name: str = "computrabajo"

    async def simulate(
        self,
        page: Page,
        offer: dict[str, Any],
        repo: JobRepository,
        dossier_dir: Path,
    ) -> dict[str, Any]:
        """Modo simular (DRY_RUN): recolecta datos, selecciona CV y guarda expediente sin enviar."""
        app_id = int(offer["id"])
        apply_url = offer.get("apply_url") or offer["canonical_url"]

        cv_sel = select_cv_and_salary(
            title=offer["title"],
            description=offer.get("description_text") or "",
            salary_strategy="FAST_HIRE",
        )
        if offer.get("selected_cv_base"):
            cvs_dir = get_cvs_dir()
            cand_pdf = cvs_dir / f"{offer['selected_cv_base']}.pdf"
            cand_docx = cvs_dir / f"{offer['selected_cv_base']}.docx"
            if cand_pdf.exists():
                cv_sel.cv_base = offer["selected_cv_base"]
                cv_sel.target_name = offer.get("selected_cv_target") or cv_sel.target_name
                cv_sel.src_pdf = cand_pdf
                if cand_docx.exists():
                    cv_sel.src_docx = cand_docx

        # Generar CV personalizado ATS de 1 página con detención estricta
        clean_folder = clean_folder_name(offer["company_name"], offer["title"], app_id=app_id)
        if dossier_dir and dossier_dir.name == clean_folder:
            target_dossier = dossier_dir
        elif dossier_dir:
            target_dossier = dossier_dir / clean_folder
        else:
            target_dossier = get_output_dir() / clean_folder
        target_dossier.mkdir(parents=True, exist_ok=True)
        cand_name = get_candidate_profile().get("full_name")
        custom_ats_pdf = target_dossier / get_ats_cv_filename(app_id, candidate_name=cand_name)

        ats_result = await async_generate_ats_cv_for_job(
            job_title=offer["title"],
            job_description=offer.get("description_text") or "",
            output_path=custom_ats_pdf,
            company_name=offer["company_name"],
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

        salary_clp = int(offer.get("expected_salary_clp") or cv_sel.salary_clp)
        cover_letter = offer.get("cover_letter_text") or build_cover_letter(
            offer["company_name"], offer["title"], salary_clp
        )
        precomputed_answers = json.loads(offer.get("killer_answers_json") or "{}")

        dossier_info = prepare_dossier_files(
            company=offer["company_name"],
            title=offer["title"],
            portal=self.portal_name,
            offer_url=offer["canonical_url"],
            external_job_id=offer.get("external_job_id"),
            location=offer.get("location") or "Chile",
            salary_clp=salary_clp,
            src_docx=cv_sel.src_docx,
            src_pdf=cv_sel.src_pdf,
            cv_target_name=cv_sel.target_name,
            cover_letter=cover_letter,
            answered_questions=precomputed_answers,
            status_label="Simulación (DRY_RUN)",
            cv_selection_reason=f"CV Personalizado ATS compilado (Score: {ats_result.ats_score}%, Keywords: {', '.join(ats_result.matched_keywords[:6])})",
            workspace_root=target_dossier.parent,
            app_id=app_id,
        )

        repo.transition_application_state(
            app_id,
            "SIMULATED_READY",
            reason="Simulación completada en Computrabajo. Expediente generado con CV quirúrgico asignado.",
            last_step_reached="SIMULATED",
            extra_updates={
                "folder_name": dossier_info["folder_path"].name,
                "folder_path": str(dossier_info["folder_path"]),
                "selected_cv_base": cv_sel.target_name,
                "selected_cv_target": cv_sel.target_name,
            },
        )

        return {
            "ok": True,
            "app_id": app_id,
            "status": "SIMULATED_READY",
            "dossier_dir": str(dossier_info["folder_path"]),
            "cv_selected": cv_sel.target_name,
            "custom_ats_cv_path": str(ats_result.pdf_path),
            "ats_score": ats_result.ats_score,
            "matched_keywords": ats_result.matched_keywords,
            "salary_clp": salary_clp,
        }

    async def postulate_assisted(
        self,
        page: Page,
        offer: dict[str, Any],
        repo: JobRepository,
        current_idx: int,
        total_offers: int,
        max_wait_seconds: int = 600,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Flujo de postulación asistido en Computrabajo con Human-in-the-Loop."""
        app_id = int(offer["id"])
        apply_url = offer.get("apply_url") or offer["canonical_url"]

        # 1. Generar CV personalizado ATS con detención estricta ante fallos
        ats_result = await async_generate_ats_cv_for_job(
            job_title=offer["title"],
            job_description=offer.get("description_text") or "",
            company_name=offer["company_name"],
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

        cv_sel = select_cv_and_salary(
            title=offer["title"],
            description=offer.get("description_text") or "",
            salary_strategy="FAST_HIRE",
        )
        cv_sel.src_pdf = ats_result.pdf_path
        cv_sel.target_name = ats_result.pdf_path.stem

        salary_clp = int(offer.get("expected_salary_clp") or cv_sel.salary_clp)
        cover_letter = offer.get("cover_letter_text") or build_cover_letter(
            offer["company_name"], offer["title"], salary_clp
        )
        precomputed_answers = json.loads(offer.get("killer_answers_json") or "{}")

        dossier_info = prepare_dossier_files(
            company=offer["company_name"],
            title=offer["title"],
            portal=self.portal_name,
            offer_url=offer["canonical_url"],
            external_job_id=offer.get("external_job_id"),
            location=offer.get("location") or "Chile",
            salary_clp=salary_clp,
            src_docx=cv_sel.src_docx,
            src_pdf=ats_result.pdf_path,
            cv_target_name=ats_result.pdf_path.stem,
            cover_letter=cover_letter,
            answered_questions=precomputed_answers,
            status_label="Listo para revisión y envío manual",
            cv_selection_reason=f"CV Personalizado ATS compilado (Score: {ats_result.ats_score}%, Keywords: {', '.join(ats_result.matched_keywords[:6])})",
            app_id=app_id,
        )
        dossier_dir: Path = dossier_info["folder_path"]

        # 2. Sincronización del CV ATS en SEGUNDO PLANO (sin tocar la pestaña del usuario)
        repo.transition_application_state(
            app_id,
            "APPLYING",
            reason=f"Sincronizando CV ATS personalizado en segundo plano ({ats_result.pdf_path.name})",
            last_step_reached="CV_SYNC",
            extra_updates={
                "folder_name": dossier_info["folder_path"].name,
                "folder_path": str(dossier_info["folder_path"]),
                "selected_cv_base": ats_result.pdf_path.stem,
                "selected_cv_target": ats_result.pdf_path.stem,
            },
        )
        await _sync_cv_background(page.context, dossier_info["pdf_path"], dossier_dir)

        # 3. Navegación a la oferta en la pestaña activa
        await page.goto(apply_url, wait_until="domcontentloaded", timeout=20000)
        await page.bring_to_front()
        await page.wait_for_timeout(1000)

        # 4. Inspección de barreras de seguridad, expiración o enlace externo
        body_text = await page.inner_text("body")
        barrier = inspect_html_for_security_barriers(page.url, body_text)
        if barrier and barrier.status_code == "CAPTCHA_OR_EXTERNAL":
            ext_shot = dossier_dir / "oferta_externa.png"
            await safe_page_screenshot(page, ext_shot)
            repo.transition_application_state(
                app_id,
                "NEEDS_HUMAN_REVIEW",
                reason=f"EXTERNAL_REDIRECT: {barrier.reason}",
                last_step_reached="EXTERNAL_DETECTED",
            )
            return {
                "ok": False,
                "app_id": app_id,
                "status": "EXTERNAL_REDIRECT",
                "message": barrier.reason,
            }

        # Verificar si la oferta tiene botón explícito hacia sitio externo
        has_external_apply_btn = await page.evaluate(
            """() => {
                const internalBtn = document.querySelector('#btn-postular, a.js-o-link[data-href*="postular"], a#apply-button');
                const links = Array.from(document.querySelectorAll('a'));
                const hasExtLink = links.some(a => {
                    const t = (a.innerText || '').toLowerCase();
                    const h = (a.href || '').toLowerCase();
                    return (t.includes('web de la empresa') || t.includes('sitio de la empresa')) && !t.includes('postularme');
                });
                return hasExtLink && !internalBtn;
            }"""
        )
        if has_external_apply_btn:
            ext_shot = dossier_dir / "oferta_externa.png"
            await safe_page_screenshot(page, ext_shot)
            repo.transition_application_state(
                app_id,
                "NEEDS_HUMAN_REVIEW",
                reason="EXTERNAL_REDIRECT: Oferta requiere postular en la web externa de la empresa.",
                last_step_reached="EXTERNAL_DETECTED",
            )
            return {
                "ok": False,
                "app_id": app_id,
                "status": "EXTERNAL_REDIRECT",
                "message": "Oferta externa clasificada para revisión.",
            }

        pre_check = verify_pre_apply_offer_state(self.portal_name, page.url, body_text)
        if not pre_check.ok:
            if pre_check.status_code == "ALREADY_APPLIED":
                repo.transition_application_state(app_id, "ALREADY_APPLIED_PORTAL", reason=pre_check.reason)
                return {"ok": False, "app_id": app_id, "status": "ALREADY_APPLIED_PORTAL", "message": pre_check.reason}
            if pre_check.status_code == "CLOSED_OFFER":
                repo.transition_application_state(app_id, "CLOSED_ON_PORTAL", reason=pre_check.reason)
                return {"ok": False, "app_id": app_id, "status": "CLOSED_ON_PORTAL", "message": pre_check.reason}

        # 5. Inyectar Banner HUD informativo para el usuario
        banner_text = (
            f"👉 [{current_idx}/{total_offers}] COMPUTRABAJO #{app_id}: {offer['company_name']} - {offer['title']} | "
            f"CV ATS: {dossier_info['pdf_path'].name} (Afinidad: {ats_result.ats_score}%). Revisa y pulsa 'Postularme'."
        )
        await page.evaluate(
            """(txt) => {
                let b = document.getElementById('antigravity-flow-banner');
                if (!b) {
                    b = document.createElement('div');
                    b.id = 'antigravity-flow-banner';
                    b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:999999;background:#ea580c;color:#ffffff;font-size:15px;font-weight:bold;text-align:center;padding:12px;box-shadow:0 4px 14px rgba(0,0,0,0.35);font-family:sans-serif;';
                    document.body.prepend(b);
                }
                b.style.background = '#ea580c';
                b.innerText = txt;
            }""",
            banner_text,
        )

        # 6. Bucle de monitoreo asistido
        start_time = asyncio.get_event_loop().time()
        confirmed = False
        kq_filled_once = False

        while (asyncio.get_event_loop().time() - start_time) < max_wait_seconds:
            await asyncio.sleep(1.0)
            if session_id:
                cur_ses = repo.get_session(session_id)
                if cur_ses and cur_ses["status"] in ("STOP_REQUESTED", "PAUSED", "STOPPED"):
                    break
            cur_app = repo.get_application(app_id)
            if cur_app and cur_app["state"] != "APPLYING":
                break

            # Monitorear context.pages para detectar si el formulario se abrió en una pestaña popup secundaria
            active_pages = [p for p in page.context.pages if not p.is_closed()]
            target_pages = [page] if page in active_pages else []
            for p_sub in active_pages:
                if p_sub not in target_pages:
                    target_pages.append(p_sub)

            try:
                for target_page in target_pages:
                    try:
                        cur_url = target_page.url or ""
                        cur_body = await target_page.inner_text("body")
                    except Exception:
                        continue

                    # A. Detectar pantalla o modal de Killer Questions (/candidate/kq o selectores DOM visibles)
                    has_kq = False
                    if not kq_filled_once:
                        if "/candidate/kq" in cur_url.lower() or "killerquestions" in cur_url.lower():
                            has_kq = True
                        else:
                            try:
                                has_kq = await target_page.evaluate(
                                    """() => {
                                        const selectors = ['#btnKiller', '#modalKQ', '.box_input textarea', 'div[class*="question"]'];
                                        for (const s of selectors) {
                                            const el = document.querySelector(s);
                                            if (el && (el.offsetParent !== null || el.getClientRects().length > 0)) {
                                                return true;
                                            }
                                        }
                                        return false;
                                    }"""
                                )
                            except Exception:
                                has_kq = False

                    if has_kq and not kq_filled_once:
                        await _fill_killer_questions_if_present(
                            page=target_page,
                            salary_clp=salary_clp,
                            cover_letter=cover_letter,
                            precomputed_answers=precomputed_answers,
                            offer_title=offer.get("title", ""),
                            company=offer.get("company_name", ""),
                            offer_desc=offer.get("description_text", ""),
                        )
                        kq_filled_once = True
                        # Capturar pantallazo con formulario rellenado
                        form_shot = dossier_dir / "pantallazo_respuestas_formulario.png"
                        await safe_page_screenshot(target_page, form_shot)

                        try:
                            await target_page.evaluate(
                                """() => {
                                    let b = document.getElementById('antigravity-flow-banner');
                                    if (b) {
                                        b.style.background = '#0284c7';
                                        b.innerHTML = '📝 <b>Preguntas filtro pre-rellenadas</b> con tu perfil verificado. Revisa las respuestas y haz clic en <u>Enviar mi CV</u>.';
                                    }
                                }"""
                            )
                        except Exception:
                            pass

                    # B. Aserción estricta de confirmación post-envío
                    verdict = verify_post_submit_confirmation(self.portal_name, cur_url, cur_body)
                    if verdict.ok:
                        confirmed = True
                        conf_shot = dossier_dir / "confirmacion_postulacion.png"
                        await safe_page_screenshot(target_page, conf_shot)

                        # Actualizar SQLite
                        repo.transition_application_state(
                            app_id,
                            "CONFIRMED",
                            reason=f"Confirmada manualmente por el usuario. {verdict.reason}",
                            last_step_reached="POST_VERIFY",
                        )
                        repo.add_evidence(app_id, "CONFIRMATION_SCREENSHOT", conf_shot, verified_valid=True)

                        # Actualizar Markdown POSTULACIONES_SIMULADAS_MANUALES.md si existe
                        md_path = get_output_dir() / "POSTULACIONES_SIMULADAS_MANUALES.md"
                        if md_path.exists():
                            content = md_path.read_text(encoding="utf-8")
                            pat = rf"(###\s*<a id=\"oferta-{app_id}\">.*?-\s*\[)\s*(\]\s*\*\*Postulado manualmente\*\*)"
                            sub_res, count = re.subn(pat, r"\g<1>X\g<2>", content, flags=re.DOTALL)
                            if count > 0:
                                md_path.write_text(sub_res, encoding="utf-8")

                        # Cuenta regresiva de 5 segundos en pantalla
                        for remaining in range(5, 0, -1):
                            try:
                                await target_page.evaluate(
                                    """(rem) => {
                                        let b = document.getElementById('antigravity-flow-banner');
                                        if (b) {
                                            b.style.background = '#16a34a';
                                            b.innerHTML = `🎉 <b>¡POSTULACIÓN ENVIADA CON ÉXITO!</b> Pasando a la siguiente oferta en ${rem} segundos...`;
                                        }
                                    }""",
                                    remaining,
                                )
                            except Exception:
                                pass
                            await asyncio.sleep(1.0)
                        break

                if confirmed:
                    break
            except Exception:
                await asyncio.sleep(1.0)

        if not confirmed:
            return {
                "ok": False,
                "app_id": app_id,
                "status": "TIMEOUT_WAITING_USER_SUBMIT",
                "message": f"Tiempo de espera ({max_wait_seconds}s) agotado para oferta #{app_id}.",
            }

        return {
            "ok": True,
            "app_id": app_id,
            "status": "CONFIRMED",
            "title": offer["title"],
            "company": offer["company_name"],
            "cv_attached": dossier_info["pdf_path"].name,
            "custom_ats_cv_path": str(dossier_info["pdf_path"]),
            "ats_score": ats_result.ats_score,
            "matched_keywords": ats_result.matched_keywords,
            "salary_clp": salary_clp,
            "dossier_dir": str(dossier_dir),
        }
