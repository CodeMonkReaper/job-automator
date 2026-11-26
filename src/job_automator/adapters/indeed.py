"""Adaptador para el portal Indeed (Chile)."""

from __future__ import annotations

import re
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any

from job_automator.adapters.base import BasePortalAdapter, DiscoveredOffer, SubmissionOutcome
from job_automator.browser.hud import update_browser_hud
from job_automator.browser.network_auditor import NetworkAuditor

if TYPE_CHECKING:
    from playwright.async_api import Page


class IndeedAdapter(BasePortalAdapter):
    portal_name = "indeed"

    async def check_session(self, page: Page) -> tuple[bool, str]:
        """Comprueba disponibilidad de Indeed Chile."""
        try:
            await page.goto("https://cl.indeed.com/", wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(1000)
            return True, "Acceso a Indeed Chile activo y disponible"
        except Exception as e:
            return False, f"Error conectando a Indeed: {e}"

    async def search_offers(
        self,
        page: Page,
        queries: list[str],
        locations: list[str],
        max_results: int = 40,
        is_known_offer_fn: Any = None,
    ) -> list[DiscoveredOffer]:
        discovered: list[DiscoveredOffer] = []
        seen_jks: set[str] = set()

        for loc in (locations or [])[:5]:
            loc_enc = urllib.parse.quote_plus(loc)
            for q in queries[:4]:
                if len(discovered) >= max_results:
                    break
                query_enc = urllib.parse.quote_plus(q)
                url = f"https://cl.indeed.com/jobs?q={query_enc}&l={loc_enc}&fromage=14"
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    await update_browser_hud(
                        page,
                        portal=self.portal_name,
                        status=f"Buscando '{q}'",
                        offer_title=f"Búsqueda profunda Indeed ({len(discovered)}/{max_results} nuevas)",
                        detail=f"Página offset={page_idx}",
                    )
                    await page.wait_for_timeout(1500)

                    cards = await page.eval_on_selector_all(
                        ".job_seen_beacon",
                        """nodes => nodes.map(n => {
                            const titleEl = n.querySelector('.jcs-JobTitle, .jobTitle');
                            const compEl = n.querySelector('[data-testid="company-name"]');
                            const locEl = n.querySelector('[data-testid="text-location"]');
                            const salaryEl = n.querySelector('[data-testid="attribute_snippet_testid"], .salary-snippet-container');
                            const linkEl = n.querySelector('a.jcs-JobTitle, h3.jobTitle a, h2.jobTitle a');
                            const snippetEl = n.querySelector('.underShelfFooter, .job-snippet');
                            let jk = '';
                            let href = '';
                            if (linkEl) {
                                jk = linkEl.getAttribute('data-jk') || '';
                                href = linkEl.href;
                            }
                            return {
                                title: titleEl ? titleEl.innerText.trim() : '',
                                company: compEl ? compEl.innerText.trim() : 'Empresa Destacada',
                                location: locEl ? locEl.innerText.trim() : 'Santiago, Chile',
                                salary: salaryEl ? salaryEl.innerText.trim() : '',
                                jk: jk,
                                url: href,
                                snippet: snippetEl ? snippetEl.innerText.trim() : ''
                            };
                        }).filter(x => x.title && x.jk)""",
                    )

                    for c in cards:
                        jk = c["jk"]
                        if not jk or jk in seen_jks:
                            continue
                        seen_jks.add(jk)

                        if is_known_offer_fn and is_known_offer_fn(
                            self.portal_name, jk, c["url"], c["company"], c["title"]
                        ):
                            continue

                        discovered.append(
                            DiscoveredOffer(
                                portal=self.portal_name,
                                external_job_id=jk,
                                title=c["title"],
                                company=c["company"],
                                url=c["url"],
                                apply_url=f"https://cl.indeed.com/viewjob?jk={jk}",
                                location=c["location"],
                                raw_salary=c["salary"],
                                description=f"{c['title']} - {c['snippet']}",
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
        await page.goto(offer_url, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_timeout(1800)

        dossier_dir.mkdir(parents=True, exist_ok=True)
        auditor = NetworkAuditor(page, self.portal_name, cv_pdf_path, dossier_dir)

        form_shot = dossier_dir / "pantallazo_respuestas_formulario.png"
        await page.screenshot(path=str(form_shot))

        if dry_run:
            auditor.write_receipt(offer_url=offer_url, extra_details={"mode": "DRY_RUN"})
            return SubmissionOutcome(
                status_code="SIMULATED_READY",
                reason="Simulación completada en Indeed: oferta verificada sin envío irreversible.",
                form_screenshot_path=form_shot,
                final_url=page.url,
            )

        btn_apply = await page.query_selector(
            '#indeedApplyButton, #applyButtonLinkContainer, .jobsearch-IndeedApplyButton-contentWrapper, button:has-text("Postularse"), a:has-text("Postularse")'
        )
        if not btn_apply:
            auditor.write_receipt(offer_url=offer_url, extra_details={"status": "NO_DIRECT_APPLY_BUTTON"})
            return SubmissionOutcome(
                status_code="NEEDS_HUMAN_REVIEW",
                reason="Oferta en Indeed redirige a portal externo del empleador.",
                form_screenshot_path=form_shot,
                final_url=page.url,
            )

        auditor.start_listening()
        await btn_apply.click()
        await page.wait_for_timeout(3000)

        auditor.write_receipt(offer_url=offer_url, extra_details={"clicked": True})
        unc_shot = dossier_dir / "estado_incierto.png"
        await page.screenshot(path=str(unc_shot))

        return SubmissionOutcome(
            status_code="SUBMITTED_UNVERIFIED",
            reason="Postulación iniciada en Indeed con comprobante de red.",
            form_screenshot_path=form_shot,
            final_screenshot_path=unc_shot,
            final_url=page.url,
        )
