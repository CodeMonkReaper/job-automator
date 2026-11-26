"""Flujo interactivo continuo: prepara la oferta, espera el clic del usuario y avanza inmediatamente."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from playwright.async_api import Page, async_playwright

from job_automator.adapters.chiletrabajos import ChiletrabajosAdapter
from job_automator.adapters.computrabajo import ComputrabajoAdapter
from job_automator.adapters.getonboard import GetOnBoardAdapter
from job_automator.adapters.indeed import IndeedAdapter
from job_automator.adapters.laborum import LaborumAdapter
from job_automator.browser.cdp_manager import discover_cdp_endpoint
from job_automator.browser.guards import verify_post_submit_confirmation
from job_automator.config import get_candidate_profile, get_cvs_dir, get_output_dir
from job_automator.db.repository import JobRepository
from job_automator.intelligence.answer_generator import build_cover_letter
from job_automator.intelligence.cv_selector import select_cv_and_salary
from job_automator.reporting.dossier_writer import prepare_dossier_files

ADAPTERS = {
    "chiletrabajos": ChiletrabajosAdapter(),
    "computrabajo": ComputrabajoAdapter(),
    "laborum": LaborumAdapter(),
    "indeed": IndeedAdapter(),
    "getonboard": GetOnBoardAdapter(),
}


def build_enhanced_cover_letter(company: str, title: str, salary_clp: int) -> str:
    """Genera una carta de presentación altamente adaptada a la especialidad técnica."""
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
        parts = base.split("Mi pretensión de renta")
        if len(parts) == 2:
            return f"{parts[0].strip()}\n\n{tech_highlight}\n\nMi pretensión de renta{parts[1]}"
    return base


async def prepare_single_offer(page: Page, app: dict[str, Any], current_idx: int, total_offers: int) -> dict[str, Any]:
    """Rellena todos los campos de la oferta en la pestaña activa sin pulsar el botón de envío definitivo."""
    app_id = int(app["id"])
    portal_name = app["portal"].lower()
    adapter = ADAPTERS.get(portal_name)
    if not adapter:
        return {"ok": False, "error": f"Portal {portal_name} no soportado."}

    apply_url = app.get("apply_url") or app["canonical_url"]

    # Selección de CV y salario
    cv_sel = select_cv_and_salary(
        title=app["title"],
        description=app.get("description_text") or "",
        salary_strategy="FAST_HIRE",
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

    salary_clp = int(app.get("expected_salary_clp") or cv_sel.salary_clp)
    cover_letter = build_enhanced_cover_letter(app["company_name"], app["title"], salary_clp)
    precomputed_answers = json.loads(app.get("killer_answers_json") or "{}")

    # Carpeta y expediente
    dossier_info = prepare_dossier_files(
        company=app["company_name"],
        title=app["title"],
        portal=portal_name,
        offer_url=app["canonical_url"],
        external_job_id=app.get("external_job_id"),
        location=app.get("location") or "Chile",
        salary_clp=salary_clp,
        src_docx=cv_sel.src_docx,
        src_pdf=cv_sel.src_pdf,
        cv_target_name=cv_sel.target_name,
        cover_letter=cover_letter,
        answered_questions=precomputed_answers,
        status_label="Listo para envío asistido",
        cv_selection_reason=cv_sel.selection_reason,
        app_id=app_id,
    )
    dossier_dir: Path = dossier_info["folder_path"]

    # --- PORTAL: CHILETRABAJOS ---
    if portal_name == "chiletrabajos":
        await page.goto(apply_url, wait_until="domcontentloaded", timeout=15000)
        await page.bring_to_front()
        await page.wait_for_timeout(800)

        carta_el = await page.query_selector('#carta, textarea[name="app_letter"]')
        if carta_el:
            await carta_el.fill(cover_letter)

        sal_el = await page.query_selector('input[name="salary"]')
        if sal_el:
            await sal_el.fill(str(salary_clp))

        dispo_cb = await page.query_selector("#dispoIn")
        if dispo_cb:
            await dispo_cb.check()

        labels = await page.eval_on_selector_all(
            'input[name$="_label"]',
            """els => els.map(e => ({
                field_name: e.name.replace('_label', ''),
                label: e.value.trim()
            }))"""
        )
        norm_answers = {k.strip().lower(): v for k, v in precomputed_answers.items()}
        for idx_q, q in enumerate(labels):
            fn = q["field_name"]
            lbl_norm = q["label"].strip().lower()
            ans = norm_answers.get(lbl_norm) or norm_answers.get(f"p{idx_q+1}") or norm_answers.get(f"q{idx_q}") or norm_answers.get(fn.lower())
            if ans:
                target_inp = await page.query_selector(f'#{fn}, textarea[name="{fn}"], input[name="{fn}"]')
                if target_inp:
                    await target_inp.fill(ans)

        await page.evaluate("""() => {
            document.querySelectorAll('.oculto_cv').forEach(el => el.classList.remove('d-none'));
        }""")
        cv_input = await page.query_selector('#cv, input[name="att1"], input[type="file"]')
        if cv_input and dossier_info["pdf_path"].exists():
            await cv_input.set_input_files(str(dossier_info["pdf_path"]))
            await page.evaluate("""(fname) => {
                const lbl = document.querySelector('label.custom-file-label[for="cv"], .custom-file-label');
                if (lbl) lbl.innerText = fname;
            }""", dossier_info["pdf_path"].name)

        banner_text = f"👉 [{current_idx}/{total_offers}] CHILETRABAJOS #{app_id}: {app['company_name']} - {app['title']} | Formulario listo y CV adjunto. Revisa y pulsa 'Enviar postulación'."

    # --- PORTAL: COMPUTRABAJO ---
    elif portal_name == "computrabajo":
        # Sincronizar CV en Computrabajo si es necesario
        cp_adapter: ComputrabajoAdapter = adapter
        if dossier_info["pdf_path"].exists():
            if cp_adapter._last_uploaded_cv_name != dossier_info["pdf_path"].name:
                await cp_adapter.upload_profile_cv(
                    page,
                    dossier_info["pdf_path"],
                    dossier_dir=dossier_dir,
                    offer_title=app["title"],
                    company=app["company_name"],
                )
                cp_adapter._last_uploaded_cv_name = dossier_info["pdf_path"].name

        # Ir a la oferta
        await page.goto(apply_url, wait_until="domcontentloaded", timeout=15000)
        await page.bring_to_front()
        await page.wait_for_timeout(1000)

        banner_text = f"👉 [{current_idx}/{total_offers}] COMPUTRABAJO #{app_id}: {app['company_name']} - {app['title']} | CV sincronizado ({dossier_info['pdf_path'].name}). Revisa y pulsa 'Postularme'."

    else:
        # Fallback para otros portales
        await page.goto(apply_url, wait_until="domcontentloaded", timeout=15000)
        await page.bring_to_front()
        banner_text = f"👉 [{current_idx}/{total_offers}] #{app_id}: {app['company_name']} - {app['title']} | Revisa y pulsa postular."

    # Banner visual arriba
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

    form_shot = dossier_dir / "pantallazo_respuestas_formulario.png"
    await page.screenshot(path=str(form_shot))

    return {
        "ok": True,
        "app_id": app_id,
        "title": app["title"],
        "company": app["company_name"],
        "cv_attached": dossier_info["pdf_path"].name,
        "salary_clp": salary_clp,
        "dossier_dir": str(dossier_dir),
    }


async def run_interactive_pipeline(target_ids: list[int], max_wait_sec_per_offer: int = 600):
    """Ejecuta el flujo 1 a 1: prepara, escucha el clic del usuario y avanza inmediatamente al siguiente."""
    cdp_info = discover_cdp_endpoint()
    if not cdp_info.available or not cdp_info.ws_url:
        print(json.dumps({"ok": False, "error": "Chrome CDP no disponible."}))
        return

    repo = JobRepository()
    total = len(target_ids)

    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp(cdp_info.ws_url, timeout=20000)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        page = context.pages[0] if context.pages else await context.new_page()

        for idx, app_id in enumerate(target_ids, 1):
            app = repo.get_application(app_id)
            if not app:
                continue

            if app["state"] == "CONFIRMED":
                print(json.dumps({"event": "already_confirmed", "app_id": app_id, "idx": idx, "total": total}, ensure_ascii=False), flush=True)
                continue

            print(json.dumps({"event": "preparing_offer", "app_id": app_id, "idx": idx, "total": total, "title": app["title"], "company": app["company_name"]}, ensure_ascii=False), flush=True)

            prep_res = await prepare_single_offer(page, app, idx, total)
            if not prep_res.get("ok"):
                print(json.dumps({"event": "prep_failed", "app_id": app_id, "error": prep_res.get("error")}, ensure_ascii=False), flush=True)
                continue

            print(json.dumps({"event": "offer_ready_waiting_click", "app_id": app_id, "title": app["title"], "company": app["company_name"]}, ensure_ascii=False), flush=True)

            # Bucle de escucha activo: detectar cuándo el usuario pulsa enviar y aparece la confirmación
            submitted = False
            start_time = asyncio.get_event_loop().time()
            portal_name = app["portal"].lower()
            dossier_dir = Path(prep_res["dossier_dir"])

            while (asyncio.get_event_loop().time() - start_time) < max_wait_sec_per_offer:
                await asyncio.sleep(1.0)
                try:
                    # En Computrabajo: si se abrió un modal con preguntas filtro tras pulsar Postularme, rellenarlo automáticamente
                    if portal_name == "computrabajo":
                        has_textareas = await page.query_selector_all("textarea")
                        if has_textareas:
                            prof = get_candidate_profile()
                            edu = prof.get("education", {})
                            fallback_ans = (
                                f"Cuento con {prof.get('total_experience_years', 2)} años de experiencia profesional como "
                                f"{edu.get('degree', 'Ingeniero en Informática')} ({edu.get('institution', 'Universidad')}), "
                                f"trabajando en desarrollo de software, integración de APIs y bases de datos con disponibilidad inmediata."
                            )
                            for ta in has_textareas:
                                val = await ta.input_value()
                                if not val.strip():
                                    await ta.fill(fallback_ans)
                            await page.evaluate("""() => {
                                document.querySelectorAll('label').forEach(l => {
                                    const txt = l.innerText.trim().toLowerCase();
                                    if (txt === 'sí' || txt === 'si' || txt === 'avanzado') l.click();
                                });
                            }""")

                    body_text = await page.inner_text("body")
                    verdict = verify_post_submit_confirmation(portal_name, page.url, body_text)
                    if verdict.ok:
                        submitted = True
                        conf_shot = dossier_dir / "confirmacion_postulacion.png"
                        await page.screenshot(path=str(conf_shot))

                        # Registrar en DB
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
                            pat = rf"(###\s*<a id=\"oferta-{app_id}\">.*?-\s*\[)\s*(\]\s*\*\*Postulado manualmente\*\*)"
                            sub_res, count = re.subn(pat, r"\g<1>X\g<2>", content, flags=re.DOTALL)
                            if count > 0:
                                md_path.write_text(sub_res, encoding="utf-8")

                        # Mostrar banner de éxito antes de pasar a la siguiente
                        await page.evaluate("""() => {
                            let b = document.getElementById('antigravity-flow-banner');
                            if (b) {
                                b.style.background = '#16a34a';
                                b.innerHTML = '🎉 <b>¡POSTULACIÓN ENVIADA CON ÉXITO!</b> Cargando la siguiente de inmediato...';
                            }
                        }""")
                        await asyncio.sleep(1.5)

                        print(json.dumps({"event": "offer_submitted_confirmed", "app_id": app_id, "reason": verdict.reason}, ensure_ascii=False), flush=True)
                        break
                except Exception:
                    await asyncio.sleep(1.0)

            if not submitted:
                print(json.dumps({"event": "timeout_waiting_click", "app_id": app_id}, ensure_ascii=False), flush=True)
                break

        print(json.dumps({"event": "batch_completed"}, ensure_ascii=False), flush=True)
