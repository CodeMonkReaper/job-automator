"""Clase base e interfaces estructuradas para adaptadores de portales."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.async_api import Page


@dataclass
class DiscoveredOffer:
    portal: str
    external_job_id: str | None
    title: str
    company: str
    url: str
    apply_url: str
    location: str = ""
    raw_salary: str = ""
    description: str = ""


@dataclass
class FormInspectionResult:
    ok: bool
    status_code: str
    reason: str
    questions: list[dict[str, Any]] = field(default_factory=list)
    page_url: str = ""


@dataclass
class SubmissionOutcome:
    status_code: str  # 'CONFIRMED' | 'SIMULATED_READY' | 'ALREADY_APPLIED_PORTAL' | 'CLOSED_ON_PORTAL' | 'FAILED_VALIDATION' | 'NEEDS_HUMAN_REVIEW' | 'SUBMITTED_UNVERIFIED' | 'AUTH_EXPIRED'
    reason: str
    form_screenshot_path: Path | None = None
    final_screenshot_path: Path | None = None
    final_url: str = ""
    answered_questions: dict[str, str] = field(default_factory=dict)
    extracted_questions: list[str] = field(default_factory=list)


class BasePortalAdapter(ABC):
    portal_name: str = "base"

    @abstractmethod
    async def check_session(self, page: Page) -> tuple[bool, str]:
        """Realiza el Pre-Flight Check de autenticación en el portal."""

    @abstractmethod
    async def search_offers(
        self,
        page: Page,
        queries: list[str],
        locations: list[str],
        max_results: int = 25,
        is_known_offer_fn: Any = None,
    ) -> list[DiscoveredOffer]:
        """Busca ofertas laborales recientes en el portal, saltando ofertas ya registradas."""

    @abstractmethod
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
        """Ejecuta o simula la postulación a una oferta individual con verificaciones estrictas."""
