"""Adaptador especializado para Get on Board."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from job_automator.adapters.base import BasePortalAdapter, DiscoveredOffer, SubmissionOutcome
from job_automator.browser.guards import (
    run_preflight_check_on_page,
    verify_post_submit_confirmation,
    verify_pre_apply_offer_state,
)

if TYPE_CHECKING:
    from playwright.async_api import Page


class GetOnBoardAdapter(BasePortalAdapter):
    portal_name = "getonboard"

    async def check_session(self, page: Page) -> tuple[bool, str]:
        verdict = await run_preflight_check_on_page(page, self.portal_name)
        return verdict.ok, verdict.reason

    async def search_offers(
        self,
        page: Page,
        queries: list[str],
        locations: list[str],
        max_results: int = 15,
        is_known_offer_fn: Any = None,
    ) -> list[DiscoveredOffer]:
        discovered: list[DiscoveredOffer] = []
        seen_urls: set[str] = set()
        for q in queries[:4]:
            if len(discovered) >= max_results:
                break
            url = f"https://www.getonbrd.com/empleos-{q.replace(' ', '-')}"
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await page.wait_for_timeout(1200)
                items = await page.eval_on_selector_all(
                    "a.gb-results-list__item",
                    """els => els.map(a => ({
                        href: a.href,
                        title: (a.querySelector('strong') || a).innerText.trim(),
                        text: a.innerText.trim()
                    }))""",
                )
                for it in items:
                    href = it["href"]
                    if href in seen_urls:
                        continue
                    seen_urls.add(href)
                    slug = href.rstrip("/").split("/")[-1]
                    discovered.append(
                        DiscoveredOffer(
                            portal=self.portal_name,
                            external_job_id=slug,
                            title=it["title"].split("\n")[0],
                            company="Empresa Get on Board",
                            url=href,
                            apply_url=href,
                            location="Chile / Remoto",
                            description=it["text"],
                        )
                    )
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
        await page.goto(offer_url, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(1500)

        body_text = await page.inner_text("body")
        pre_verdict = verify_pre_apply_offer_state(self.portal_name, page.url, body_text)
        if not pre_verdict.ok:
            return SubmissionOutcome(
                status_code="NEEDS_HUMAN_REVIEW" if pre_verdict.status_code == "CAPTCHA_OR_EXTERNAL" else "FAILED_VALIDATION",
                reason=pre_verdict.reason,
                final_url=page.url,
            )

        cv_input = await page.query_selector("input[type=file][name='applicant[resume]'], input[type=file]")
        if cv_input and cv_pdf_path.exists():
            await cv_input.set_input_files(str(cv_pdf_path))

        msg_el = await page.query_selector("textarea[name='applicant[message]']")
        if msg_el:
            await msg_el.fill(cover_letter)

        sal_el = await page.query_selector("input[name='applicant[expected_salary]']")
        if sal_el:
            await sal_el.fill(str(salary_clp))

        dossier_dir.mkdir(parents=True, exist_ok=True)
        form_shot = dossier_dir / "pantallazo_respuestas_formulario.png"
        await page.screenshot(path=str(form_shot))

        if dry_run:
            return SubmissionOutcome(
                status_code="SIMULATED_READY",
                reason="Simulación completada en Get on Board.",
                form_screenshot_path=form_shot,
                final_url=page.url,
            )

        btn_sub = await page.query_selector("button[type=submit]:has-text('Enviar postulación'), button[type=submit]:has-text('Apply')")
        if not btn_sub:
            return SubmissionOutcome(
                status_code="NEEDS_HUMAN_REVIEW",
                reason="Formulario externo o botón de envío no estándar en Get on Board.",
                form_screenshot_path=form_shot,
                final_url=page.url,
            )

        await btn_sub.click()
        await page.wait_for_timeout(3000)
        post_body = await page.inner_text("body")
        post_verdict = verify_post_submit_confirmation(self.portal_name, page.url, post_body)
        if post_verdict.ok:
            conf_shot = dossier_dir / "confirmacion_postulacion.png"
            await page.screenshot(path=str(conf_shot))
            return SubmissionOutcome(
                status_code="CONFIRMED",
                reason=post_verdict.reason,
                form_screenshot_path=form_shot,
                final_screenshot_path=conf_shot,
                final_url=page.url,
            )

        unc_shot = dossier_dir / "estado_incierto.png"
        await page.screenshot(path=str(unc_shot))
        return SubmissionOutcome(
            status_code="SUBMITTED_UNVERIFIED",
            reason=post_verdict.reason,
            form_screenshot_path=form_shot,
            final_screenshot_path=unc_shot,
            final_url=page.url,
        )
