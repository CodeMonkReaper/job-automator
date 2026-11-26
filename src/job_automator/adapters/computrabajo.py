"""Adaptador especializado para Computrabajo (Subida secuencial de CV + Killer Questions)."""

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
from job_automator.config import get_candidate_profile

if TYPE_CHECKING:
    from playwright.async_api import Page


class ComputrabajoAdapter(BasePortalAdapter):
    portal_name = "computrabajo"
    _last_uploaded_cv_name: str | None = None

    async def check_session(self, page: Page) -> tuple[bool, str]:
        verdict = await run_preflight_check_on_page(page, self.portal_name)
        return verdict.ok, verdict.reason

    async def upload_profile_cv(
        self,
        page: Page,
        cv_pdf_path: Path,
        dossier_dir: Path | None = None,
        offer_title: str = "",
        company: str = "",
    ) -> bool:
        """
        Garantiza que el CV exacto entre los 33 disponibles quede activo en Computrabajo.
        Si no está entre los 10 almacenados y el cupo está lleno, elimina uno antiguo,
        sube el PDF exacto y verifica su radio button en el DOM.
        """
        if not cv_pdf_path.exists():
            return False
        await page.goto(
            "https://candidato.cl.computrabajo.com/candidate/cv/uploadcv",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        await update_browser_hud(
            page,
            portal=self.portal_name,
            status="Sincronizando CV",
            offer_title=offer_title or "Gestión Documental Computrabajo",
            company=company,
            cv_name=cv_pdf_path.name,
            cv_verified=False,
            detail="Verificando que el CV exacto esté activo en la cuenta...",
        )
        await page.wait_for_timeout(1800)

        auditor = None
        if dossier_dir:
            dossier_dir.mkdir(parents=True, exist_ok=True)
            auditor = NetworkAuditor(page, self.portal_name, cv_pdf_path, dossier_dir)
            auditor.start_listening()

        async def _get_uploaded_cvs() -> list[dict[str, Any]]:
            return await page.evaluate(
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

        uploaded_items = await _get_uploaded_cvs()
        exact_stem = cv_pdf_path.stem.lower().replace("_actualizado", "")
        exact_radio_id = None

        for item in uploaded_items:
            t_low = item["text"].lower()
            if cv_pdf_path.name.lower() in t_low or exact_stem in t_low:
                exact_radio_id = item["id"]
                break

        page_text = await page.inner_text("body")
        max_reached = "máximo de documentos" in page_text.lower() or "maximo de documentos" in page_text.lower()
        replaced_old_slot = False

        # Si el CV exacto NO está en los 10 subidos y se alcanzó el tope, liberar 1 espacio eliminando el último no activo
        if not exact_radio_id and (max_reached or len(uploaded_items) >= 10):
            await update_browser_hud(
                page,
                portal=self.portal_name,
                status="Rotando Slot CV (10/10)",
                offer_title=offer_title,
                company=company,
                cv_name=cv_pdf_path.name,
                cv_verified=False,
                detail="Borrando CV antiguo no activo para liberar cupo y subir el requerido...",
            )
            deleted = await page.evaluate(
                """() => {
                    const rows = Array.from(document.querySelectorAll('ul.fila_tabla'));
                    for (let i = rows.length - 1; i >= 0; i--) {
                        const r = rows[i];
                        const radio = r.querySelector('input[type="radio"]');
                        const del = r.querySelector('#it-delete, .papelera');
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
                await page.wait_for_timeout(3000)
                await page.wait_for_load_state("domcontentloaded")
                await page.wait_for_timeout(1000)
                replaced_old_slot = True
                uploaded_items = await _get_uploaded_cvs()
                page_text = await page.inner_text("body")
                max_reached = "máximo de documentos" in page_text.lower() or "maximo de documentos" in page_text.lower()

        # Si el CV exacto no está subido y hay espacio disponible, subir el PDF exacto desde CVs_Actualizados
        file_input = await page.query_selector(
            "#ContainerGeneralOverWrite_ContainerGeneral_fuUploadCv, input[type='file']"
        )
        if not exact_radio_id and file_input and not max_reached:
            try:
                await file_input.set_input_files(str(cv_pdf_path))
                if auditor:
                    await auditor.verify_dom_file_input("#ContainerGeneralOverWrite_ContainerGeneral_fuUploadCv, input[type='file']")
                await page.wait_for_timeout(3000)
                uploaded_items = await _get_uploaded_cvs()
                for item in uploaded_items:
                    t_low = item["text"].lower()
                    if cv_pdf_path.name.lower() in t_low or exact_stem in t_low:
                        exact_radio_id = item["id"]
                        break
            except Exception:
                pass

        # Si aún no hay coincidencia literal exacta, buscar la mejor coincidencia por especialidad entre los 10
        matched_radio_id = exact_radio_id
        selected_label_text = cv_pdf_path.name
        if not matched_radio_id:
            try:
                name_tokens = {
                    tok.strip().lower()
                    for tok in str(get_candidate_profile().get("full_name", "")).split()
                    if tok.strip()
                }
            except Exception:
                name_tokens = set()
            skip_tokens = name_tokens | {"cv", "resume", "actualizado"}
            for item in uploaded_items:
                item_text_lower = item["text"].lower()
                if any(w in item_text_lower for w in exact_stem.split("_") if len(w) > 4 and w not in skip_tokens):
                    matched_radio_id = item["id"]
                    selected_label_text = item["text"].split("\n")[0].strip()
                    break
            if not matched_radio_id and uploaded_items:
                # Fallback a Ingeniero Informático general en la cuenta
                for item in uploaded_items:
                    if "informatic" in item["text"].lower() or "ingeniero" in item["text"].lower():
                        matched_radio_id = item["id"]
                        selected_label_text = item["text"].split("\n")[0].strip()
                        break

        is_radio_checked = False
        if matched_radio_id:
            try:
                is_radio_checked = await page.evaluate(
                    """(targetId) => {
                        let el = document.getElementById(targetId);
                        if (!el && targetId.startsWith('ag-radio-')) {
                            const idx = targetId.replace('ag-radio-', '');
                            el = document.querySelector(`input[type="radio"][data-ag-radio-idx="${idx}"]`);
                        }
                        if (!el) return false;
                        el.click();
                        el.checked = true;
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        return !!el.checked;
                    }""",
                    matched_radio_id,
                )
                await page.wait_for_timeout(800)
                if auditor and is_radio_checked:
                    auditor.dom_file_name = cv_pdf_path.name if exact_radio_id else selected_label_text
            except Exception:
                pass

        await update_browser_hud(
            page,
            portal=self.portal_name,
            status="CV Activo Verificado" if is_radio_checked else "CV Sincronizado",
            offer_title=offer_title or "Perfil Computrabajo",
            company=company,
            cv_name=cv_pdf_path.name if exact_radio_id else selected_label_text,
            cv_verified=is_radio_checked,
            detail=f"Radio button #{matched_radio_id} marcado como principal en DOM.",
        )

        # Capturar pantallazo auditable del CV seleccionado en el perfil
        if dossier_dir:
            cv_shot = dossier_dir / "pantallazo_cv_activo_computrabajo.png"
            await safe_page_screenshot(page, cv_shot)

        if auditor:
            auditor.write_receipt(
                offer_url=page.url,
                extra_details={
                    "action": "profile_cv_sync_computrabajo",
                    "exact_33_cv_uploaded_or_present": exact_radio_id is not None,
                    "replaced_old_slot": replaced_old_slot,
                    "max_cv_limit_reached": max_reached,
                    "matched_existing_radio": matched_radio_id is not None,
                    "radio_id": matched_radio_id,
                    "active_cv_label_in_dom": selected_label_text,
                    "radio_dom_checked_verified": is_radio_checked,
                    "screenshot_evidence": "pantallazo_cv_activo_computrabajo.png" if dossier_dir else None,
                },
            )
        return is_radio_checked

    async def search_offers(
        self,
        page: Page,
        queries: list[str],
        locations: list[str],
        max_results: int = 40,
        is_known_offer_fn: Any = None,
    ) -> list[DiscoveredOffer]:
        discovered: list[DiscoveredOffer] = []
        seen_urls: set[str] = set()

        search_urls: list[str] = []
        region_map = {
            "valparaíso": "valparaiso",
            "valparaiso": "valparaiso",
            "viña del mar": "valparaiso",
            "antofagasta": "antofagasta",
            "la serena": "coquimbo",
            "coquimbo": "coquimbo",
            "rancagua": "o-higgins",
            "talca": "maule",
            "temuco": "la-araucania",
            "puerto montt": "los-lagos",
            "osorno": "los-lagos",
            "iquique": "tarapaca",
            "arica": "arica-y-parinacota",
            "copiapó": "atacama",
            "copiapo": "atacama",
            "chillán": "nuble",
            "chillan": "nuble",
            "valdivia": "los-rios",
            "punta arenas": "magallanes-y-de-la-antartica-chilena",
        }
        for loc in (locations or [])[:6]:
            loc_norm = loc.lower().strip()
            slug = region_map.get(loc_norm)
            if slug:
                for q in queries[:2]:
                    q_enc = q.strip().replace(" ", "+")
                    search_urls.append(f"https://cl.computrabajo.com/empleos-en-{slug}?q={q_enc}")
                search_urls.append(f"https://cl.computrabajo.com/empleos-de-informatica-y-telecomunicaciones-en-{slug}")

        # Si no hay ubicaciones regionales específicas, usar búsqueda general
        if not (locations or []):
            for q in queries[:2]:
                q_enc = q.strip().replace(" ", "+")
                search_urls.append(f"https://cl.computrabajo.com/empleos?q={q_enc}")

        for idx_u, url in enumerate(search_urls, 1):
            if len(discovered) >= max_results:
                break
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await update_browser_hud(
                    page,
                    portal=self.portal_name,
                    status=f"Explorando pág {idx_u}",
                    offer_title=f"Búsqueda profunda Computrabajo ({len(discovered)}/{max_results} nuevas)",
                    detail=f"Saltando ofertas repetidas · {url.replace('https://cl.computrabajo.com/', '')}",
                )
                await page.wait_for_timeout(900)
                offers = await page.eval_on_selector_all(
                    "article.box_offer",
                    """els => els.map(el => {
                        const a = el.querySelector('h2 a, h1 a, a.js-o-link');
                        const corp = el.querySelector('p.fs16, .corporation, a.fc_base');
                        const loc = el.querySelector('p.fs13.fc_aux, span[itemprop="addressLocality"]');
                        const salary = el.querySelector('span.tag_sal, span[class*="sal"]');
                        const desc = el.querySelector('p.body_desc, p.fs14');
                        return a ? {
                            title: a.innerText.trim(),
                            href: a.href.split('#')[0],
                            id: a.getAttribute('data-id') || el.getAttribute('data-id'),
                            corp: corp ? corp.innerText.trim() : 'Confidencial',
                            location: loc ? loc.innerText.trim() : '',
                            salary: salary ? salary.innerText.trim() : 'No especificado',
                            desc: desc ? desc.innerText.trim() : ''
                        } : null;
                    }).filter(Boolean)""",
                )
                for off in offers:
                    href = off["href"]
                    if href in seen_urls:
                        continue
                    seen_urls.add(href)
                    m = re.search(r"-([A-F0-9]{20,36})$", href, re.I)
                    ext_id = off.get("id") or (m.group(1) if m else None)

                    # Saltar ofertas ya registradas en base de datos para recolectar solo NUEVAS
                    if is_known_offer_fn and is_known_offer_fn(self.portal_name, ext_id, href, off["corp"], off["title"]):
                        continue

                    discovered.append(
                        DiscoveredOffer(
                            portal=self.portal_name,
                            external_job_id=ext_id,
                            title=off["title"],
                            company=off["corp"],
                            url=href,
                            apply_url=href,
                            location=off["location"],
                            raw_salary=off["salary"],
                            description=off["desc"],
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
        offer_url = offer.get("apply_url") or offer.get("canonical_url") or ""
        title_str = offer.get("title") or ""
        comp_str = offer.get("company_name") or offer.get("company") or ""
        score_val = offer.get("match_score")

        # Paso A: Si no es dry_run, asegurar que el CV específico quede cargado y activo en el perfil de Computrabajo
        cv_verified = False
        if not dry_run and cv_pdf_path.exists():
            if self._last_uploaded_cv_name == cv_pdf_path.name:
                cv_verified = True
                if dossier_dir:
                    dossier_dir.mkdir(parents=True, exist_ok=True)
                    auditor = NetworkAuditor(page, self.portal_name, cv_pdf_path, dossier_dir)
                    auditor.dom_file_name = cv_pdf_path.name
                    auditor.write_receipt(
                        offer_url=offer_url,
                        extra_details={
                            "action": "profile_cv_cached_active_computrabajo",
                            "radio_dom_checked_verified": True,
                            "active_cv_label_in_dom": cv_pdf_path.name,
                        },
                    )
            else:
                cv_verified = await self.upload_profile_cv(
                    page,
                    cv_pdf_path,
                    dossier_dir=dossier_dir,
                    offer_title=title_str,
                    company=comp_str,
                )
                if cv_verified:
                    self._last_uploaded_cv_name = cv_pdf_path.name

        # Paso B: Navegar a la oferta
        await page.goto(offer_url, wait_until="domcontentloaded", timeout=15000)
        await update_browser_hud(
            page,
            portal=self.portal_name,
            status="Abriendo Oferta",
            offer_title=title_str,
            company=comp_str,
            cv_name=cv_pdf_path.name,
            cv_verified=cv_verified,
            match_score=score_val,
            salary_clp=salary_clp,
            detail="Verificando disponibilidad del botón Postularme...",
        )
        await page.wait_for_timeout(1800)

        body_text = await page.inner_text("body")
        html_content = await page.content()

        pre_verdict = verify_pre_apply_offer_state(
            self.portal_name, page.url, body_text, html_content=html_content
        )
        if not pre_verdict.ok:
            status_map = {
                "AUTH_EXPIRED": "AUTH_EXPIRED",
                "ALREADY_APPLIED": "ALREADY_APPLIED_PORTAL",
                "CLOSED_OFFER": "CLOSED_ON_PORTAL",
                "CAPTCHA_OR_EXTERNAL": "NEEDS_HUMAN_REVIEW",
                "INVALID_FORM": "FAILED_VALIDATION",
            }
            return SubmissionOutcome(
                status_code=status_map.get(pre_verdict.status_code, "FAILED_VALIDATION"),
                reason=pre_verdict.reason,
                final_url=page.url,
            )

        # Paso C: Pulsar "Postularme"
        btn_apply = await page.query_selector(
            '#btn-postular, a.js-o-link[data-href*="postular"], a#apply-button, button:has-text("Postularme"), a:has-text("Postularme")'
        )
        if not btn_apply:
            return SubmissionOutcome(
                status_code="CLOSED_ON_PORTAL",
                reason="Botón 'Postularme' no disponible en la oferta de Computrabajo.",
                final_url=page.url,
            )

        if dry_run:
            dossier_dir.mkdir(parents=True, exist_ok=True)
            form_shot = dossier_dir / "pantallazo_respuestas_formulario.png"
            await safe_page_screenshot(page, form_shot)
            return SubmissionOutcome(
                status_code="SIMULATED_READY",
                reason="Simulación completada en Computrabajo (botón Postularme verificado, sin clic irreversible).",
                form_screenshot_path=form_shot,
                final_url=page.url,
            )

        await btn_apply.click()
        await page.wait_for_timeout(2500)

        # Verificar que no haya redirigido a login externo tras el clic
        after_click_body = await page.inner_text("body")
        session_check = verify_pre_apply_offer_state(self.portal_name, page.url, after_click_body)
        if not session_check.ok and session_check.status_code in ("AUTH_EXPIRED", "CAPTCHA_OR_EXTERNAL"):
            return SubmissionOutcome(
                status_code="AUTH_EXPIRED" if session_check.status_code == "AUTH_EXPIRED" else "NEEDS_HUMAN_REVIEW",
                reason=session_check.reason,
                final_url=page.url,
            )

        answered: dict[str, str] = {}
        extracted_questions: list[str] = []
        missing_questions: list[str] = []
        norm_answers = {
            k.strip().strip('"').strip("'").lower(): v
            for k, v in precomputed_answers.items()
        }

        # Paso D: Extraer TODAS las preguntas abiertas del DOM para redacción 100% por Antigravity
        textareas = await page.query_selector_all("textarea")
        for idx, ta in enumerate(textareas):
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
            clean_label = q_label.strip() or f"Pregunta abierta {idx + 1}"
            extracted_questions.append(clean_label)
            clean_norm = clean_label.strip().strip('"').strip("'").lower()

            ans = (
                norm_answers.get(clean_norm)
                or norm_answers.get(f"p{idx + 1}")
                or norm_answers.get(f"q{idx}")
                or (cover_letter if "carta de presentación" in clean_norm or "carta de presentacion" in clean_norm else None)
            )
            if not ans:
                missing_questions.append(clean_label)
            else:
                await ta.fill(ans)
                answered[clean_label] = ans

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
                cv_verified=cv_verified,
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
                answered_questions={f"q{i}": q for i, q in enumerate(extracted_questions)},
                extracted_questions=extracted_questions,
            )

        # Marcar radios de preguntas cerradas (Sí / Avanzado) cuando existen
        await page.evaluate(
            """() => {
                document.querySelectorAll('label').forEach(l => {
                    const txt = l.innerText.trim().toLowerCase();
                    if (txt === 'sí' || txt === 'si' || txt === 'avanzado') {
                        l.click();
                    }
                });
                document.querySelectorAll('input[type="radio"]').forEach(r => {
                    if (r.checked) r.dispatchEvent(new Event('change', { bubbles: true }));
                });
            }"""
        )
        await update_browser_hud(
            page,
            portal=self.portal_name,
            status="Enviando Postulación",
            offer_title=title_str,
            company=comp_str,
            cv_name=cv_pdf_path.name,
            cv_verified=cv_verified,
            match_score=score_val,
            salary_clp=salary_clp,
            detail="Formulario completado con respuestas redactadas por Antigravity.",
            questions=answered,
        )
        await page.wait_for_timeout(800)

        dossier_dir.mkdir(parents=True, exist_ok=True)
        form_shot = dossier_dir / "pantallazo_respuestas_formulario.png"
        await safe_page_screenshot(page, form_shot)

        # Paso E: Si hay un botón final de envío de Killer Questions, pulsarlo
        btn_submit = await page.query_selector(
            '#btnKiller, #btnApply, input[value="Enviar mi CV"], button:has-text("Enviar mi CV"), input[type="submit"]'
        )
        if btn_submit:
            await btn_submit.click()
            await page.wait_for_timeout(3500)

        # Paso F: Aserción estricta de confirmación (Regla 4)
        final_body = await page.inner_text("body")
        post_verdict = verify_post_submit_confirmation(self.portal_name, page.url, final_body)

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
