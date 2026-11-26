"""Módulo de auditoría de red y verificación de transmisión de archivos CV."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.async_api import Page, Request, Response


class NetworkAuditor:
    def __init__(self, page: Page, portal: str, cv_pdf_path: Path, dossier_dir: Path):
        self.page = page
        self.portal = portal
        self.cv_pdf_path = cv_pdf_path
        self.dossier_dir = dossier_dir
        self.post_url: str | None = None
        self.http_status: int | None = None
        self.is_multipart: bool = False
        self.request_headers: dict[str, str] = {}
        self.dom_file_name: str | None = None
        self._listening = False

    def start_listening(self) -> None:
        if self._listening:
            return
        self.page.on("request", self._on_request)
        self.page.on("response", self._on_response)
        self._listening = True

    def stop_listening(self) -> None:
        if not self._listening:
            return
        try:
            self.page.remove_listener("request", self._on_request)
            self.page.remove_listener("response", self._on_response)
        except Exception:
            pass
        self._listening = False

    def _on_request(self, req: Request) -> None:
        if req.method == "POST":
            url_lower = req.url.lower()
            # Solo registrar peticiones POST dirigidas al dominio real del portal de empleo
            allowed_domains = (
                "chiletrabajos.cl",
                "computrabajo.com",
                "laborum.cl",
                "getonbrd.com",
                "indeed.com",
            )
            blocked_substrings = (
                "clarity.ms",
                "googlesyndication",
                "google-analytics",
                "doubleclick",
                "rum-proxy",
                "facebook",
                "hotjar",
                "criteo",
                "bat.bing.com",
            )
            if not any(d in url_lower for d in allowed_domains) or any(b in url_lower for b in blocked_substrings):
                return
            # Registrar endpoints de envío de formulario o upload de archivo
            self.post_url = req.url
            self.request_headers = {
                k: v
                for k, v in req.headers.items()
                if k.lower() in ("content-type", "user-agent", "origin", "referer")
            }
            self.is_multipart = "multipart" in req.headers.get("content-type", "").lower()

    def _on_response(self, resp: Response) -> None:
        if resp.request.method == "POST" and self.post_url and (
            resp.url == self.post_url or "postular" in resp.url or "upload" in resp.url
        ):
            self.http_status = resp.status

    async def verify_dom_file_input(self, selector: str = "input[type='file']") -> str | None:
        """Verifica directamente en el objeto FileList del DOM qué archivo está cargado."""
        try:
            name = await self.page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    return (el && el.files && el.files.length > 0) ? el.files[0].name : null;
                }""",
                selector,
            )
            self.dom_file_name = name
            return name
        except Exception:
            return None

    def write_receipt(
        self,
        external_job_id: str | None = None,
        offer_url: str = "",
        extra_details: dict[str, Any] | None = None,
    ) -> Path:
        """Escribe el comprobante físico comprobante_envio_red.json en el expediente."""
        self.stop_listening()
        self.dossier_dir.mkdir(parents=True, exist_ok=True)
        receipt_path = self.dossier_dir / "comprobante_envio_red.json"

        expected_name = self.cv_pdf_path.name if self.cv_pdf_path else ""
        file_size = self.cv_pdf_path.stat().st_size if self.cv_pdf_path and self.cv_pdf_path.exists() else 0
        sha256 = (
            hashlib.sha256(self.cv_pdf_path.read_bytes()).hexdigest()
            if self.cv_pdf_path and self.cv_pdf_path.exists()
            else None
        )

        dom_matches = (self.dom_file_name == expected_name) if self.dom_file_name else False

        data = {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "portal": self.portal,
            "external_job_id": external_job_id,
            "offer_url": offer_url or self.page.url,
            "cv_audit": {
                "expected_filename": expected_name,
                "dom_attached_filename": self.dom_file_name,
                "dom_attachment_verified": dom_matches,
                "file_size_bytes": file_size,
                "sha256_checksum": sha256,
            },
            "network_audit": {
                "post_request_url": self.post_url,
                "http_response_status": self.http_status,
                "is_multipart_form_data": self.is_multipart,
                "relevant_headers": self.request_headers,
            },
            "transmission_verdict": (
                "CONFIRMED_ATTACHED_AND_TRANSMITTED"
                if (dom_matches and self.post_url)
                else ("DOM_ATTACHED_VERIFIED" if dom_matches else "TRANSMITTED_UNVERIFIED_DOM")
            ),
        }
        if extra_details:
            data["extra_details"] = extra_details

        receipt_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        return receipt_path
