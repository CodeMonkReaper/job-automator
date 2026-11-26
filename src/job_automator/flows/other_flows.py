"""Flujos especializados exclusivos para Laborum e Indeed (Pendientes de adaptación)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from playwright.async_api import Page

from job_automator.db.repository import JobRepository
from job_automator.flows.base_flow import BasePortalFlow


class LaborumFlow(BasePortalFlow):
    """Flujo exclusivo para Laborum."""

    portal_name: str = "laborum"

    async def simulate(
        self,
        page: Page,
        offer: dict[str, Any],
        repo: JobRepository,
        dossier_dir: Path,
    ) -> dict[str, Any]:
        return {"ok": True, "status": "SIMULATED_READY", "portal": self.portal_name}

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
        return {
            "ok": False,
            "status": "PENDING_ADAPTATION",
            "portal": self.portal_name,
            "message": "Laborum pendiente de adaptación al flujo asistido interactivo.",
        }


class IndeedFlow(BasePortalFlow):
    """Flujo exclusivo para Indeed."""

    portal_name: str = "indeed"

    async def simulate(
        self,
        page: Page,
        offer: dict[str, Any],
        repo: JobRepository,
        dossier_dir: Path,
    ) -> dict[str, Any]:
        return {"ok": True, "status": "SIMULATED_READY", "portal": self.portal_name}

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
        return {
            "ok": False,
            "status": "PENDING_ADAPTATION",
            "portal": self.portal_name,
            "message": "Indeed pendiente de adaptación al flujo asistido interactivo.",
        }
