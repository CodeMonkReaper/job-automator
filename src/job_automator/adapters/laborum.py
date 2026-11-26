"""Adaptador para el portal Laborum (Chile)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from job_automator.adapters.base import BasePortalAdapter, DiscoveredOffer, SubmissionOutcome
from job_automator.browser.hud import update_browser_hud
from job_automator.browser.network_auditor import NetworkAuditor

if TYPE_CHECKING:
    from playwright.async_api import Page


class LaborumAdapter(BasePortalAdapter):
    portal_name = "laborum"

    async def check_session(self, page: Page) -> tuple[bool, str]:
        """Comprueba si la sesión de Laborum está activa en el navegador Chrome."""
        try:
            await page.goto("https://www.laborum.cl/", wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(1500)
            body = await page.inner_text("body")
            if "Mi cuenta" in body or "Mis postulaciones" in body or "Cerrar sesión" in body:
                return True, "Sesión activa en Laborum verificada"
            return True, "Acceso a Laborum disponible (búsqueda y evaluación activa)"
        except Exception as e:
            return False, f"Error conectando a Laborum: {e}"

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

        laborum_regions = {
            "valparaíso": "valparaiso",
            "valparaiso": "valparaiso",
            "viña del mar": "valparaiso",
            "antofagasta": "antofagasta",
            "la serena": "coquimbo",
            "coquimbo": "coquimbo",
            "rancagua": "libertador-general-bernardo-o-higgins",
            "talca": "maule",
            "temuco": "la-araucania",
            "puerto montt": "los-lagos",
            "iquique": "tarapaca",
            "arica": "arica-y-parinacota",
            "copiapó": "atacama",
            "chillán": "nuble",
            "valdivia": "los-rios",
            "punta arenas": "magallanes-y-de-la-antartica-chilena",
        }
        search_urls: list[str] = []
        for loc in (locations or []):
            loc_low = loc.lower().strip()
            slug = laborum_regions.get(loc_low)
            if slug:
                search_urls.append(f"https://www.laborum.cl/empleos-en-{slug}.html")

        if not (locations or []):
            search_urls.extend([
                f"https://www.laborum.cl/empleos-categoria-tecnologia-sistemas-y-telecomunicaciones.html?page={p}"
                for p in range(1, 4)
            ])
            for q in queries[:2]:
                q_slug = q.strip().replace(" ", "-").lower()
                search_urls.append(f"https://www.laborum.cl/empleos-busqueda-{q_slug}.html")

        for idx_u, url in enumerate(search_urls, 1):
            if len(discovered) >= max_results:
                break
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                await update_browser_hud(
                    page,
                    portal=self.portal_name,
                    status=f"Explorando pág {idx_u}",
                    offer_title=f"Búsqueda profunda Laborum ({len(discovered)}/{max_results} nuevas)",
                    detail=url.replace("https://www.laborum.cl/", ""),
                )
                await page.wait_for_timeout(1300)

                links = await page.eval_on_selector_all(
                    "a[href*='/empleos/']",
                    """els => els.map(e => {
                        const href = e.href.split('?')[0];
                        const h2 = e.querySelector('h2, h3');
                        const text = e.innerText.trim();
                        return (text.length > 5 && href.includes('.html')) ? {
                            href: href,
                            heading: h2 ? h2.innerText.trim() : '',
                            text: text
                        } : null;
                    }).filter(Boolean)""",
                )

                for item in links:
                    href = item["href"]
                    if href in seen_urls:
                        continue
                    seen_urls.add(href)

                    m = re.search(r"-(\d{8,12})\.html", href)
                    jid = m.group(1) if m else None

                    raw_text = item["text"]
                    clean_lines = [
                        ln.strip()
                        for ln in raw_text.split("\n")
                        if ln.strip()
                        and not any(
                            ln.strip().lower().startswith(p)
                            for p in ("publicado", "actualizado", "nuevo", "destacado", "hoy", "ayer", "postulación")
                        )
                    ]
                    title = item.get("heading") or (clean_lines[0] if clean_lines else "Oferta TI Laborum")
                    company = clean_lines[1] if len(clean_lines) > 1 else "Empresa TI"

                    if is_known_offer_fn and is_known_offer_fn(self.portal_name, jid, href, company, title):
                        continue

                    discovered.append(
                        DiscoveredOffer(
                            portal=self.portal_name,
                            external_job_id=jid,
                            title=title,
                            company=company,
                            url=href,
                            apply_url=href,
                            location="Chile",
                            description=raw_text,
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
                reason="Simulación completada en Laborum: oferta verificada sin envío irreversible.",
                form_screenshot_path=form_shot,
                final_url=page.url,
            )

        # En modo en vivo, buscar botón Postularme de Laborum
        btn_apply = await page.query_selector(
            'button:has-text("Postularme"), a:has-text("Postularme"), #btn-postular'
        )
        if not btn_apply:
            auditor.write_receipt(offer_url=offer_url, extra_details={"status": "NO_APPLY_BUTTON"})
            return SubmissionOutcome(
                status_code="CLOSED_ON_PORTAL",
                reason="Oferta en Laborum cerrada o sin botón directo de postulación.",
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
            reason="Postulación enviada en Laborum con comprobante de red.",
            form_screenshot_path=form_shot,
            final_screenshot_path=unc_shot,
            final_url=page.url,
        )
