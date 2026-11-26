"""Flujo especializado exclusivo para Chiletrabajos.

- Modo 'simular': Evalúa la oferta, recolecta datos, precalcula respuestas y guarda expediente en SIMULATED_READY.
- Modo 'postula': Navega a la oferta, rellena los formularios con la información recolectada en la simulación,
                  muestra banner visual informativo, espera la revisión y el clic manual del usuario,
                  y tras detectar la confirmación salta de inmediato a la siguiente oferta.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from job_automator.browser.guards import safe_page_screenshot, verify_post_submit_confirmation
from job_automator.config import get_candidate_profile, get_output_dir
from job_automator.db.repository import JobRepository
from job_automator.flows.base_flow import BasePortalFlow
from job_automator.intelligence.ai_client import synthesize_ai_answer_dynamic
from job_automator.intelligence.answer_generator import (
    answer_question_deterministically,
    build_cover_letter,
)
from job_automator.intelligence.ats_cv_generator import (
    async_generate_ats_cv_for_job,
    get_ats_cv_filename,
)
from job_automator.intelligence.cv_selector import select_cv_and_salary
from job_automator.intelligence.fact_checker import validate_text_truthfulness
from job_automator.reporting.dossier_writer import clean_folder_name, prepare_dossier_files
from job_automator.reporting.report_builder import build_consolidated_report


def resolve_chiletrabajos_postular_url(url: str, external_job_id: str | None = None) -> str:
    """Asegura que la URL apunte al formulario de postulación directa /trabajo/postular/<id>."""
    if not url:
        return ""
    if "/postular/" in url:
        return url
    jid = external_job_id
    if not jid:
        m = re.search(r"-(\d{6,8})(?:$|/|\?)", url)
        if not m:
            m = re.search(r"/(\d{6,8})(?:$|/|\?)", url)
        jid = m.group(1) if m else None
    if jid:
        return f"https://www.chiletrabajos.cl/trabajo/postular/{jid}"
    return url


def build_enhanced_cover_letter(company: str, title: str, salary_clp: int) -> str:
    """Genera una carta de presentación altamente adaptada a la especialidad técnica requerida."""
    prof = get_candidate_profile()
    base = build_cover_letter(company, title, salary_clp, prof)

    t_low = (title or "").lower()
    tech_highlight = ""
    if any(k in t_low for k in ["ia", "inteligencia artificial", "llm", "machine learning"]):
        tech_highlight = "Cuento con sólida experiencia práctica en Python, consumo e integración de modelos de lenguaje (LLMs), APIs REST y diseño de flujos de automatización y backend para operar soluciones de IA en producción."
    elif any(k in t_low for k in [".net", "c#", "csharp", "vb.net"]):
        tech_highlight = "Cuento con experiencia demostrable en el ecosistema .NET (C#, ASP.NET Core, APIs REST y bases de datos SQL), creando soluciones robustas y mantenibles."
    elif any(k in t_low for k in ["fullstack", "full stack", "react", "node", "angular", "vue", "frontend"]):
        tech_highlight = "Cuento con experiencia práctica integral tanto en frontend moderno (React, Next.js, TypeScript) como en backend (Node.js, APIs REST y bases de datos relacionales y NoSQL)."
    elif any(k in t_low for k in ["python", "django", "fastapi"]):
        tech_highlight = "Cuento con sólida experiencia en desarrollo backend con Python (APIs REST, FastAPI, Django), automatización de procesos y persistencia de datos."
    elif any(k in t_low for k in ["qa", "testing", "automatizador"]):
        tech_highlight = "Cuento con experiencia práctica en diseño y ejecución de casos de prueba, automatización de pruebas de software y aseguramiento de calidad en producción."

    if tech_highlight:
        if "Mi pretensión de renta" in base:
            parts = base.split("Mi pretensión de renta")
            if len(parts) == 2:
                return f"{parts[0].strip()}\n\n{tech_highlight}\n\nMi pretensión de renta{parts[1]}"
        elif "Atentamente," in base:
            parts = base.split("Atentamente,")
            return f"{parts[0].strip()}\n\n{tech_highlight}\n\nAtentamente,{parts[1]}"
        else:
            return f"{base.strip()}\n\n{tech_highlight}"
    return base


async def fill_chiletrabajos_form(
    page: Page,
    cover_letter: str,
    salary_clp: int,
    ats_pdf_path: Path | str | None = None,
    precomputed_answers: dict[str, str] | None = None,
    offer_title: str = "",
    company: str = "",
    offer_desc: str = "",
) -> dict[str, str]:
    """Rellena los campos obligatorios del formulario de Chiletrabajos (#carta, salary, dispoIn, cv, killer questions)."""
    # 1. Carta de presentación
    try:
        carta_el = await page.query_selector('#carta, textarea[name="app_letter"]')
        if carta_el:
            await carta_el.fill(cover_letter)
    except Exception:
        pass

    # 2. Sueldo pretendido
    try:
        sal_el = await page.query_selector('input[name="salary"]')
        if sal_el:
            await sal_el.fill(str(salary_clp))
    except Exception:
        pass

    # 3. Disponibilidad inmediata
    try:
        dispo_cb = await page.query_selector("#dispoIn, input[name='dispoIn']")
        if dispo_cb:
            await dispo_cb.check()
    except Exception:
        pass

    # 4. Adjuntar archivo CV ATS en input#cv desocultando .oculto_cv
    pdf_path = Path(ats_pdf_path) if ats_pdf_path else None
    if pdf_path and pdf_path.exists():
        try:
            await page.evaluate("""() => {
                document.querySelectorAll('.oculto_cv').forEach(el => el.classList.remove('d-none'));
            }""")
            cv_input = await page.query_selector('#cv, input[name="att1"], input[type="file"]')
            if cv_input:
                await cv_input.set_input_files(str(pdf_path))
                await page.evaluate("""(fname) => {
                    const lbl = document.querySelector('label.custom-file-label[for="cv"], .custom-file-label');
                    if (lbl) lbl.innerText = fname;
                }""", pdf_path.name)
        except Exception:
            pass

    # 5. Preguntas filtro dinámicas (Killer Questions)
    injected_answers: dict[str, str] = {}
    precomp = precomputed_answers or {}
    norm_answers = {k.strip().lower(): v for k, v in precomp.items()}

    try:
        labels = await page.eval_on_selector_all(
            'input[name$="_label"]',
            """els => els.map(e => ({
                field_name: e.name.replace('_label', ''),
                label: e.value.trim()
            }))""",
        )
    except Exception:
        labels = []

    for idx_q, q in enumerate(labels):
        fn = q["field_name"]
        lbl = q["label"].strip()
        lbl_norm = lbl.lower()
        ans = (
            norm_answers.get(lbl_norm)
            or norm_answers.get(f"p{idx_q+1}")
            or norm_answers.get(f"q{idx_q}")
            or norm_answers.get(fn.lower())
            or answer_question_deterministically(lbl, salary_clp)
            or synthesize_ai_answer_dynamic(
                question_text=lbl,
                offer_title=offer_title,
                company=company,
                salary_clp=salary_clp,
                offer_desc=offer_desc,
            )
        )
        chk = validate_text_truthfulness(ans)
        if not chk.valid:
            prof = get_candidate_profile()
            edu = prof.get("education", {})
            ans = (
                f"{edu.get('degree', 'Ingeniero en Informática')} ({edu.get('status', 'Titulado')}) en {edu.get('institution', 'Universidad')} "
                f"con {prof.get('total_experience_years', 2)} años de experiencia profesional comprobable. "
                f"Residencia en {prof.get('residence', 'Remoto')}. "
                f"Pretensión de renta líquida: ${salary_clp:,}".replace(",", ".") + " (conversable y abierta al mercado)."
            )

        # Truncado seguro a 480 caracteres respetando límite de campo
        if len(ans) > 480:
            clean_trimmed = ans[:480]
            last_period = clean_trimmed.rfind(".")
            if last_period > 250:
                ans = clean_trimmed[:last_period + 1]
            else:
                ans = clean_trimmed

        try:
            target_inp = await page.query_selector(f'#{fn}, textarea[name="{fn}"], input[name="{fn}"]')
            if target_inp:
                await target_inp.fill(ans)
                injected_answers[lbl] = ans
        except Exception:
            pass

    # 6. Inspeccionar textareas adicionales que no tengan _label asociado
    try:
        other_textareas = await page.query_selector_all("textarea:not(#carta):not([name='app_letter'])")
        for ta in other_textareas:
            ta_name = await ta.get_attribute("name") or ""
            if any(q["field_name"] == ta_name for q in labels):
                continue
            lbl_text = await ta.evaluate("""el => {
                const p = el.closest('.form-group, .form_group, div');
                if (p) {
                    const lbl = p.querySelector('label, h3, h4, p, span');
                    if (lbl && lbl !== el) return lbl.innerText.trim();
                }
                return '';
            }""")
            clean_lbl = (lbl_text or "").strip()
            if clean_lbl and clean_lbl not in injected_answers:
                ans = (
                    norm_answers.get(clean_lbl.lower())
                    or answer_question_deterministically(clean_lbl, salary_clp)
                    or synthesize_ai_answer_dynamic(
                        question_text=clean_lbl,
                        offer_title=offer_title,
                        company=company,
                        salary_clp=salary_clp,
                        offer_desc=offer_desc,
                    )
                )
                chk = validate_text_truthfulness(ans)
                if not chk.valid:
                    prof = get_candidate_profile()
                    edu = prof.get("education", {})
                    ans = (
                        f"{edu.get('degree', 'Ingeniero en Informática')} ({edu.get('status', 'Titulado')}) en {edu.get('institution', 'Universidad')} "
                        f"con {prof.get('total_experience_years', 2)} años de experiencia profesional comprobable. "
                        f"Residencia en {prof.get('residence', 'Remoto')}. Pretensión de renta: ${salary_clp:,}".replace(",", ".") + " líquidos."
                    )
                if len(ans) > 480:
                    clean_trimmed = ans[:480]
                    last_period = clean_trimmed.rfind(".")
                    if last_period > 250:
                        ans = clean_trimmed[:last_period + 1]
                    else:
                        ans = clean_trimmed
                try:
                    await ta.fill(ans)
                    injected_answers[clean_lbl] = ans
                except Exception:
                    pass
    except Exception:
        pass

    return injected_answers


class ChiletrabajosFlow(BasePortalFlow):
    """Flujo interactivo y de simulación específico para Chiletrabajos."""

    portal_name: str = "chiletrabajos"

    async def simulate(
        self,
        page: Page,
        offer: dict[str, Any],
        repo: JobRepository,
        dossier_dir: Path,
    ) -> dict[str, Any]:
        """Modo 'simular' en Chiletrabajos: recolecta datos y guarda expediente sin pulsar el botón de envío."""
        app_id = int(offer["id"])

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

        cv_sel = select_cv_and_salary(
            title=offer["title"],
            description=offer.get("description_text") or "",
            salary_strategy="FAST_HIRE",
        )
        cv_sel.src_pdf = ats_result.pdf_path
        cv_sel.target_name = ats_result.pdf_path.stem

        salary_clp = int(offer.get("expected_salary_clp") or cv_sel.salary_clp)
        cover_letter = build_enhanced_cover_letter(offer["company_name"], offer["title"], salary_clp)
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
            reason="Simulación completada en Chiletrabajos. Datos recolectados y listos para postulación asistida.",
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
        """
        Modo 'postula' en Chiletrabajos:
        1. Genera el CV personalizado ATS de 1 página con detención estricta.
        2. Navega a la oferta.
        3. Rellena los formularios con la información recolectada en la simulación.
        4. Muestra banner en pantalla y espera que el usuario revise y pulse manualmente el botón.
        5. Al detectar la confirmación de envío en el DOM, guarda captura, actualiza SQLite/Markdown y salta a la siguiente.
        """
        app_id = int(offer["id"])
        raw_url = offer.get("apply_url") or offer["canonical_url"]
        apply_url = resolve_chiletrabajos_postular_url(raw_url, external_job_id=offer.get("external_job_id"))

        # 1. Generar CV personalizado ATS con detención estricta ante fallos
        clean_folder = clean_folder_name(offer["company_name"], offer["title"], app_id=app_id)
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

        cv_sel = select_cv_and_salary(
            title=offer["title"],
            description=offer.get("description_text") or "",
            salary_strategy="FAST_HIRE",
        )
        cv_sel.src_pdf = ats_result.pdf_path
        cv_sel.target_name = ats_result.pdf_path.stem

        salary_clp = int(offer.get("expected_salary_clp") or cv_sel.salary_clp)
        cover_letter = offer.get("cover_letter_text") or build_enhanced_cover_letter(
            offer["company_name"], offer["title"], salary_clp
        )
        precomputed_answers = json.loads(offer.get("killer_answers_json") or "{}")

        # Expediente
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
            status_label="Listo para revisión y envío manual",
            cv_selection_reason=f"CV Personalizado ATS compilado (Score: {ats_result.ats_score}%, Keywords: {', '.join(ats_result.matched_keywords[:6])})",
            workspace_root=target_dossier.parent,
            app_id=app_id,
        )
        dossier_dir: Path = dossier_info["folder_path"]

        repo.transition_application_state(
            app_id,
            "APPLYING",
            reason=f"Preparando postulación con CV ATS personalizado ({ats_result.pdf_path.name})",
            last_step_reached="CV_GENERATION",
            extra_updates={
                "folder_name": dossier_info["folder_path"].name,
                "folder_path": str(dossier_info["folder_path"]),
                "selected_cv_base": ats_result.pdf_path.stem,
                "selected_cv_target": ats_result.pdf_path.stem,
            },
        )

        # 2. Navegación a la oferta en la pestaña activa
        await page.goto(apply_url, wait_until="domcontentloaded", timeout=20000)
        await page.bring_to_front()
        await page.wait_for_timeout(800)

        # 3. Relleno de campos y Killer Questions con la lógica centralizada
        injected_answers = await fill_chiletrabajos_form(
            page=page,
            cover_letter=cover_letter,
            salary_clp=salary_clp,
            ats_pdf_path=dossier_info["pdf_path"],
            precomputed_answers=precomputed_answers,
            offer_title=offer["title"],
            company=offer["company_name"],
            offer_desc=offer.get("description_text") or "",
        )

        # Captura de pantalla previa usando safe_page_screenshot
        form_shot = dossier_dir / "pantallazo_respuestas_formulario.png"
        await safe_page_screenshot(page, form_shot)

        # 4. Banner visual para el usuario
        banner_text = (
            f"👉 [{current_idx}/{total_offers}] CHILETRABAJOS #{app_id}: {offer['company_name']} - {offer['title']} | "
            f"Formulario listo y CV adjunto ({dossier_info['pdf_path'].name}). Revisa y pulsa 'Enviar postulación'."
        )
        await page.evaluate("""(txt) => {
            let b = document.getElementById('antigravity-flow-banner');
            if (!b) {
                b = document.createElement('div');
                b.id = 'antigravity-flow-banner';
                b.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:999999;background:#1d4ed8;color:#ffffff;font-size:15px;font-weight:bold;text-align:center;padding:12px;box-shadow:0 4px 14px rgba(0,0,0,0.35);font-family:sans-serif;';
                document.body.prepend(b);
            }
            b.style.background = '#1d4ed8';
            b.innerText = txt;
        }""", banner_text)

        # 5. Bucle de escucha activo: espera a que el usuario presione el botón y confirma en el DOM
        start_time = asyncio.get_event_loop().time()
        confirmed = False

        while (asyncio.get_event_loop().time() - start_time) < max_wait_seconds:
            await asyncio.sleep(1.0)
            if page.is_closed():
                break
            if session_id:
                cur_ses = repo.get_session(session_id)
                if cur_ses and cur_ses["status"] in ("STOP_REQUESTED", "PAUSED", "STOPPED"):
                    break
            cur_app = repo.get_application(app_id)
            if cur_app and cur_app["state"] != "APPLYING":
                break
            try:
                body_text = await page.inner_text("body")
                verdict = verify_post_submit_confirmation(self.portal_name, page.url, body_text)
                if verdict.ok:
                    confirmed = True
                    conf_shot = dossier_dir / "confirmacion_postulacion.png"
                    await safe_page_screenshot(page, conf_shot)

                    # Registrar en base de datos SQLite
                    repo.transition_application_state(
                        app_id,
                        "CONFIRMED",
                        reason=f"Confirmada manualmente por el usuario. {verdict.reason}",
                        last_step_reached="POST_VERIFY",
                        extra_updates={
                            "killer_answers_json": json.dumps(injected_answers, ensure_ascii=False),
                        },
                    )
                    repo.add_evidence(app_id, "CONFIRMATION_SCREENSHOT", conf_shot, verified_valid=True)

                    # Actualizar casilla [X] en POSTULACIONES_SIMULADAS_MANUALES.md si existe
                    md_path = get_output_dir() / "POSTULACIONES_SIMULADAS_MANUALES.md"
                    if md_path.exists():
                        content = md_path.read_text(encoding="utf-8")
                        pat = rf"(###\s*<a id=\"oferta-{app_id}\">.*?-\s*\[)\s*(\]\s*\*\*Postulado manualmente\*\*)"
                        sub_res, count = re.subn(pat, r"\g<1>X\g<2>", content, flags=re.DOTALL)
                        if count > 0:
                            md_path.write_text(sub_res, encoding="utf-8")

                    # Actualizar informe consolidado Markdown
                    build_consolidated_report(repo=repo, write_markdown=True)

                    # Banner de éxito visual inmediato
                    await page.evaluate("""() => {
                        let b = document.getElementById('antigravity-flow-banner');
                        if (b) {
                            b.style.background = '#16a34a';
                            b.innerHTML = '🎉 <b>¡POSTULACIÓN ENVIADA CON ÉXITO!</b> Cargando la siguiente de inmediato...';
                        }
                    }""")
                    await asyncio.sleep(1.5)
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
            "salary_clp": salary_clp,
            "dossier_dir": str(dossier_dir),
            "injected_answers": injected_answers,
        }
