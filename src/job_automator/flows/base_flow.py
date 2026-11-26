"""Arquitectura de flujos especializados independientes por portal de empleo."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from playwright.async_api import Page

from job_automator.db.repository import JobRepository


class BasePortalFlow(ABC):
    """Interfaz base para el flujo de trabajo de cada portal de empleo."""

    portal_name: str = ""

    @abstractmethod
    async def simulate(
        self,
        page: Page,
        offer: dict[str, Any],
        repo: JobRepository,
        dossier_dir: Path,
    ) -> dict[str, Any]:
        """Modo 'simular': evalúa la oferta, recolecta datos, precalcula respuestas y guarda expediente sin enviar."""
        raise NotImplementedError

    @abstractmethod
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
        """Modo 'postula': navega a la oferta, inyecta la información recolectada, espera el clic manual del usuario y avanza inmediatamente al confirmar."""
        raise NotImplementedError
