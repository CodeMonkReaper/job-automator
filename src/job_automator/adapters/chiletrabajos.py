"""Adaptador especializado para Chiletrabajos (Playwright sobre Chrome CDP)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from job_automator.adapters.base import BasePortalAdapter, DiscoveredOffer, SubmissionOutcome
from job_automator.browser.guards import (
    run_preflight_check_on_page,
    safe_page_screenshot,
    verify_post_submit_confirmation,
    verify_pre_apply_offer_state,
)
from job_automator.browser.hud import update_browser_hud
from job_automator.browser.network_auditor import NetworkAuditor

if TYPE_CHECKING:
    from playwright.async_api import Page


class ChiletrabajosAdapter(BasePortalAdapter):
    portal_name = "chiletrabajos"

    async def check_session(self, page: Page) -> tuple[bool, str]:
        verdict = await run_preflight_check_on_page(page, self.portal_name)
        return verdict.ok, verdict.reason

    async def search_offers(
        self,
        page: Page,
        queries: list[str],
        locations: list[str],
        max_results: int = 50,
        is_known_offer_fn: Any = None,
    ) -> list[DiscoveredOffer]:
        """
        Búsqueda profunda en Chiletrabajos con paginación máxima, saltando ofertas ya vistas
        hasta recolectar max_results ofertas 100% nuevas.
        """
        discovered: list[DiscoveredOffer] = []
        seen_ids: set[str] = set()

        # 1. Búsquedas combinadas por ubicación regional y palabras clave TI
        search_urls: list[str] = []
        for loc in (locations or [])[:7]:
            loc_enc = loc.strip().replace(" ", "+")
            for q in queries[:2]:
                q_enc = q.strip().replace(" ", "+")
                search_urls.append(f"https://www.chiletrabajos.cl/encuentra-un-empleo?2={q_enc}&13={loc_enc}")

        # 2. Agregar búsquedas generales solo si no se especificaron ubicaciones
        if not (locations or []):
            for q in queries[:2]:
                q_enc = q.strip().replace(" ", "+")
                search_urls.append(f"https://www.chiletrabajos.cl/encuentra-un-empleo?2={q_enc}")
            search_urls.append("https://www.chiletrabajos.cl/categoria/informatica-telecomunicaciones")

        for idx_u, url in enumerate(search_urls, 1):
            if len(discovered) >= max_results:
                break
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=12000)
                if "#google_vignette" in page.url:
                    await page.keyboard.press("Escape")
                    await page.wait_for_timeout(500)
                await update_browser_hud(
                    page,
                    portal=self.portal_name,
                    status=f"Explorando pág {idx_u}",
                    offer_title=f"Búsqueda profunda Chiletrabajos ({len(discovered)}/{max_results} nuevas)",
                    detail=f"URL: {url.replace('https://www.chiletrabajos.cl/', '')}",
                )
                await page.wait_for_timeout(900)

                items = await page.eval_on_selector_all(
                    ".job-item",
                    """els => els.map(el => {
                        const a = el.querySelector('h2 a, h3 a');
                        const text = el.innerText.trim();
                        return a ? { title: a.innerText.trim(), href: a.href, text: text } : null;
                    }).filter(Boolean)""",
                )
                for it in items:
                    href = it["href"]
                    m = re.search(r"-(\d{6,8})(?:$|/|\?)", href)
                    jid = m.group(1) if m else None
                    if not jid or jid in seen_ids:
                        continue
                    seen_ids.add(jid)

                    lines = [ln.strip() for ln in it["text"].split("\n") if ln.strip()]
                    corp = lines[1] if len(lines) > 1 else "Empresa"
                    loc = lines[2] if len(lines) > 2 else "Chile"

                    # Saltar ofertas que ya existen en la base de datos para recolectar solo NUEVAS
                    if is_known_offer_fn and is_known_offer_fn(self.portal_name, jid, href, corp, it["title"]):
                        continue

                    discovered.append(
                        DiscoveredOffer(
                            portal=self.portal_name,
                            external_job_id=jid,
                            title=it["title"],
                            company=corp,
                            url=href,
                            apply_url=f"https://www.chiletrabajos.cl/trabajo/postular/{jid}",
                            location=loc,
                            description=it["text"],
                        )
                    )
                    if len(discovered) >= max_results:
                        break
            except Exception:
                continue

        return discovered

    async def apply_to_offer(
        self,
        page: Page,
        offer: dict[str, Any],
        cv_pdf_path: Path,
        salary_clp: int,
        cover_letter: str,
        precomputed_answers: dict[str, str],
        dossier_dir: Path,
        dry_run: bool = False,
    ) -> SubmissionOutcome:
        jid = str(offer.get("external_job_id") or "")
        apply_url = offer.get("apply_url") or f"https://www.chiletrabajos.cl/trabajo/postular/{jid}"
        title_str = offer.get("title") or ""
        comp_str = offer.get("company_name") or offer.get("company") or ""
        score_val = offer.get("match_score")

        await page.goto(apply_url, wait_until="domcontentloaded", timeout=15000)
        await update_browser_hud(
            page,
            portal=self.portal_name,
            status="Abriendo Oferta",
            offer_title=title_str,
            company=comp_str,
            cv_name=cv_pdf_path.name,
            match_score=score_val,
            salary_clp=salary_clp,
            detail="Verificando sesión y estado del formulario...",
        )
        await page.wait_for_timeout(1200)

        body_text = await page.inner_text("body")
        html_content = await page.content()

        # Reglas 2 y 3: Verificar sesión activa, estado de oferta y legitimidad del formulario
        pre_verdict = verify_pre_apply_offer_state(
            self.portal_name,
            page.url,
            body_text,
            html_content=html_content,
            expected_job_id=jid if jid else None,
        )
        if not pre_verdict.ok:
            status_map = {
                "AUTH_EXPIRED": "AUTH_EXPIRED",
                "ALREADY_APPLIED": "ALREADY_APPLIED_PORTAL",
                "CLOSED_OFFER": "CLOSED_ON_PORTAL",
                "CAPTCHA_OR_EXTERNAL": "NEEDS_HUMAN_REVIEW",
                "INVALID_FORM": "FAILED_VALIDATION",
            }
            mapped = status_map.get(pre_verdict.status_code, "FAILED_VALIDATION")
            return SubmissionOutcome(
                status_code=mapped,
                reason=pre_verdict.reason,
                final_url=page.url,
            )

        # 1. Extraer título y empresa real de la tabla de detalles en la página si están disponibles y regenerar carta limpia
        page_meta = await page.evaluate(
            """() => {
                let realTitle = '';
                const h1 = document.querySelector('h1.title, .page-header h1, h1');
                if (h1) {
                    realTitle = h1.innerText.replace(/^Postular a\\s+/i, '').trim();
                }
                let realCompany = '';
                const rows = document.querySelectorAll('table tr');
                for (const r of rows) {
                    const tds = r.querySelectorAll('td');
                    if (tds.length >= 2) {
                        const k = tds[0].innerText.trim().toLowerCase();
                        if (k.includes('buscado') || k.includes('empresa')) {
                            realCompany = tds[1].innerText.trim();
                            break;
                        }
                    }
                }
                return { realTitle, realCompany };
            }"""
        )
        if page_meta.get("realTitle") and ("…" in title_str or len(page_meta["realTitle"]) > len(title_str)):
            title_str = page_meta["realTitle"]
        if page_meta.get("realCompany"):
            comp_str = page_meta["realCompany"]

        from job_automator.intelligence.answer_generator import build_cover_letter
        cover_letter = build_cover_letter(comp_str, title_str, salary_clp)

        carta_el = await page.query_selector('#carta, textarea[name="app_letter"]')
        if carta_el:
            await carta_el.fill(cover_letter)

        # 2. Rellenar Pretensión de Renta (entero numérico)
        sal_el = await page.query_selector('input[name="salary"]')
        if sal_el:
            await sal_el.fill(str(salary_clp))

        # 3. Marcar Disponibilidad Inmediata
        dispo_cb = await page.query_selector("#dispoIn")
        if dispo_cb:
            await dispo_cb.check()

        # 4. Extraer Killer Questions para que sean respondidas 100% por Antigravity (sin plantillas enlatadas)
        labels = await page.eval_on_selector_all(
            'input[name$="_label"]',
            """els => els.map(e => ({
                field_name: e.name.replace('_label', ''),
                label: e.value.trim()
            }))""",
        )
        extracted_questions: list[str] = [q["label"] for q in labels if q.get("label")]
        missing_questions: list[str] = []
        answered: dict[str, str] = {}

        norm_answers = {
            k.strip().strip('"').strip("'").lower(): v
            for k, v in precomputed_answers.items()
        }
        is_zero_indexed = "q0" in norm_answers

        for idx, q in enumerate(labels):
            fn = q["field_name"]
            lbl = q["label"]
            lbl_norm = lbl.strip().strip('"').strip("'").lower()
            ans = (
                norm_answers.get(lbl_norm)
                or norm_answers.get(f"p{idx + 1}")
                or (norm_answers.get(f"q{idx}") if is_zero_indexed else (norm_answers.get(fn.lower()) or norm_answers.get(f"q{idx}")))
            )
            if not ans:
                missing_questions.append(lbl)
            else:
                target_input = await page.query_selector(f'#{fn}, textarea[name="{fn}"], input[name="{fn}"]')
                if target_input:
                    await target_input.fill(ans)
                    answered[lbl] = ans

        if missing_questions:
            dossier_dir.mkdir(parents=True, exist_ok=True)
            form_shot = dossier_dir / "pantallazo_respuestas_formulario.png"
            await update_browser_hud(
                page,
                portal=self.portal_name,
                status="Esperando Respuestas IA",
                offer_title=title_str,
                company=comp_str,
                cv_name=cv_pdf_path.name,
                match_score=score_val,
                salary_clp=salary_clp,
                detail="Preguntas extraídas para redacción a medida por Antigravity.",
                questions=extracted_questions,
            )
            await safe_page_screenshot(page, form_shot)
            return SubmissionOutcome(
                status_code="NEEDS_HUMAN_REVIEW",
                reason=f"WAITING_AI_ANSWERS: {', '.join(missing_questions)}",
                form_screenshot_path=form_shot,
                final_url=page.url,
                answered_questions={q["field_name"]: q["label"] for q in labels},
                extracted_questions=extracted_questions,
            )

        # 5. Desplegar el contenedor oculto (.oculto_cv) y adjuntar el CV PDF específico con auditoría
        dossier_dir.mkdir(parents=True, exist_ok=True)
        auditor = NetworkAuditor(page, self.portal_name, cv_pdf_path, dossier_dir)

        await page.evaluate(
            """() => {
                document.querySelectorAll('.oculto_cv').forEach(el => el.classList.remove('d-none'));
            }"""
        )

        cv_verified = False
        cv_input = await page.query_selector('#cv, input[name="att1"], input[type="file"]')
        if cv_input and cv_pdf_path.exists():
            await cv_input.set_input_files(str(cv_pdf_path))
            await page.evaluate(
                """(fname) => {
                    const lbl = document.querySelector('label.custom-file-label[for="cv"], .custom-file-label');
                    if (lbl) lbl.innerText = fname;
                }""",
                cv_pdf_path.name,
            )
            dom_name = await auditor.verify_dom_file_input('#cv, input[name="att1"], input[type="file"]')
            cv_verified = dom_name == cv_pdf_path.name

        await update_browser_hud(
            page,
            portal=self.portal_name,
            status="Formulario Listo",
            offer_title=title_str,
            company=comp_str,
            cv_name=cv_pdf_path.name,
            cv_verified=cv_verified,
            match_score=score_val,
            salary_clp=salary_clp,
            detail="CV físico cargado en input#cv y verificado en DOM.",
            questions=answered,
        )
        await page.wait_for_timeout(800)

        # 6. Captura obligatoria previa al envío
        form_shot = dossier_dir / "pantallazo_respuestas_formulario.png"
        await safe_page_screenshot(page, form_shot)

        if dry_run:
            auditor.write_receipt(external_job_id=jid, offer_url=page.url, extra_details={"mode": "DRY_RUN"})
            return SubmissionOutcome(
                status_code="SIMULATED_READY",
                reason="Simulación completada: formulario rellenado, archivo verificado y captura previa tomada sin enviar.",
                form_screenshot_path=form_shot,
                final_url=page.url,
                answered_questions=answered,
            )

        # 7. Clic en Enviar con escucha activa de tráfico de red
        btn_sub = await page.query_selector(
            'button:has-text("Enviar postulación"), input[value*="Enviar postulación"], input[name="apply"], #btn_postular'
        )
        if not btn_sub:
            auditor.write_receipt(external_job_id=jid, offer_url=page.url, extra_details={"error": "submit_button_missing"})
            return SubmissionOutcome(
                status_code="FAILED_VALIDATION",
                reason="Botón de envío no encontrado en el formulario de Chiletrabajos.",
                form_screenshot_path=form_shot,
                final_url=page.url,
                answered_questions=answered,
            )

        auditor.start_listening()
        await btn_sub.click()
        await page.wait_for_timeout(3500)
        auditor.write_receipt(external_job_id=jid, offer_url=page.url)

        # 8. Regla 4: Verificación estricta post-envío antes de guardar confirmacion_postulacion.png
        post_body = await page.inner_text("body")
        post_verdict = verify_post_submit_confirmation(self.portal_name, page.url, post_body)

        if post_verdict.ok and post_verdict.status_code == "CONFIRMED":
            conf_shot = dossier_dir / "confirmacion_postulacion.png"
            await safe_page_screenshot(page, conf_shot)
            return SubmissionOutcome(
                status_code="CONFIRMED",
                reason=post_verdict.reason,
                form_screenshot_path=form_shot,
                final_screenshot_path=conf_shot,
                final_url=page.url,
                answered_questions=answered,
            )

        if post_verdict.status_code == "VALIDATION_ERROR":
            err_shot = dossier_dir / "error_validacion.png"
            await safe_page_screenshot(page, err_shot)
            return SubmissionOutcome(
                status_code="FAILED_VALIDATION",
                reason=post_verdict.reason,
                form_screenshot_path=form_shot,
                final_screenshot_path=err_shot,
                final_url=page.url,
                answered_questions=answered,
            )

        if post_verdict.status_code == "AUTH_EXPIRED":
            err_shot = dossier_dir / "error_sesion.png"
            await safe_page_screenshot(page, err_shot)
            return SubmissionOutcome(
                status_code="AUTH_EXPIRED",
                reason=post_verdict.reason,
                form_screenshot_path=form_shot,
                final_screenshot_path=err_shot,
                final_url=page.url,
                answered_questions=answered,
            )

        # Estado incierto: guardar evidencia diagnóstica separada sin llamar confirmacion_postulacion.png
        unc_shot = dossier_dir / "estado_incierto.png"
        await safe_page_screenshot(page, unc_shot)
        return SubmissionOutcome(
            status_code="SUBMITTED_UNVERIFIED",
            reason=post_verdict.reason,
            form_screenshot_path=form_shot,
            final_screenshot_path=unc_shot,
            final_url=page.url,
            answered_questions=answered,
        )
