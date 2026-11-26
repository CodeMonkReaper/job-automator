"""Fábrica y registro de flujos especializados por portal."""

from __future__ import annotations

from job_automator.flows.base_flow import BasePortalFlow
from job_automator.flows.chiletrabajos_flow import ChiletrabajosFlow
from job_automator.flows.computrabajo_flow import ComputrabajoFlow
from job_automator.flows.other_flows import IndeedFlow, LaborumFlow

PORTAL_FLOWS: dict[str, BasePortalFlow] = {
    "chiletrabajos": ChiletrabajosFlow(),
    "computrabajo": ComputrabajoFlow(),
    "laborum": LaborumFlow(),
    "indeed": IndeedFlow(),
}


def get_portal_flow(portal_name: str) -> BasePortalFlow | None:
    """Obtiene la instancia de flujo especializada para un portal determinado."""
    return PORTAL_FLOWS.get(portal_name.lower().strip())
