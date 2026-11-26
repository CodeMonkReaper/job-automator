"""HUD Visual en tiempo real inyectado en el DOM de Chrome para transparencia total ante el usuario."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from playwright.async_api import Page


async def update_browser_hud(
    page: Page,
    *,
    portal: str = "",
    status: str = "",
    offer_title: str = "",
    company: str = "",
    cv_name: str = "",
    cv_verified: bool | None = None,
    match_score: float | int | None = None,
    salary_clp: int | None = None,
    detail: str = "",
    questions: dict[str, str] | list[str] | None = None,
    bring_to_front: bool = True,
) -> None:
    """
    Inyecta o actualiza un panel HUD flotante en la pestaña activa de Chrome
    para que el usuario vea en todo momento qué oferta, qué CV y qué respuestas se están procesando.
    """
    try:
        if bring_to_front:
            await page.bring_to_front()
    except Exception:
        pass

    cv_badge_color = "#10b981" if cv_verified else ("#f59e0b" if cv_verified is False else "#3b82f6")
    cv_status_icon = "✅ VERIFICADO EN DOM" if cv_verified else ("⚠️ PENDIENTE" if cv_verified is False else "📄 SELECCIONADO")

    q_html = ""
    if isinstance(questions, dict) and questions:
        items = []
        for k, v in list(questions.items())[:3]:
            items.append(
                f"<div style='margin-top:4px;font-size:11px;background:rgba(255,255,255,0.06);padding:4px 6px;border-radius:4px;'>"
                f"<b style='color:#93c5fd;'>P: {k[:65]}</b><br/>"
                f"<span style='color:#e2e8f0;'>R: {str(v)[:95]}</span></div>"
            )
        q_html = "".join(items)
    elif isinstance(questions, list) and questions:
        items = [
            f"<div style='margin-top:3px;font-size:11px;color:#fcd34d;'>• {str(q)[:75]}</div>"
            for q in questions[:3]
        ]
        q_html = "".join(items)

    payload = {
        "portal": (portal or "Motor").upper(),
        "status": status or "Activo",
        "offer_title": offer_title or "Explorando ofertas...",
        "company": company or "",
        "cv_name": cv_name or "Evaluando catálogo (33 CVs)...",
        "cv_badge_color": cv_badge_color,
        "cv_status_icon": cv_status_icon,
        "score": f"{match_score} pts" if match_score is not None else "-",
        "salary": f"${salary_clp:,}".replace(",", ".") if salary_clp else "$750.000",
        "detail": detail or "",
        "q_html": q_html,
    }

    js_code = """(data) => {
        let hud = document.getElementById('antigravity-live-hud');
        if (!hud) {
            hud = document.createElement('div');
            hud.id = 'antigravity-live-hud';
            hud.style.cssText = [
                'position: fixed',
                'bottom: 18px',
                'right: 18px',
                'width: 390px',
                'max-width: 92vw',
                'background: rgba(15, 23, 42, 0.94)',
                'color: #f8fafc',
                'border: 2px solid #38bdf8',
                'border-radius: 12px',
                'padding: 12px 14px',
                'font-family: system-ui, -apple-system, sans-serif',
                'font-size: 12px',
                'line-height: 1.4',
                'box-shadow: 0 12px 32px rgba(0,0,0,0.55)',
                'z-index: 2147483647',
                'pointer-events: none',
                'backdrop-filter: blur(8px)',
                'transition: all 0.2s ease'
            ].join(';');
            document.body.appendChild(hud);
        }
        hud.innerHTML = `
            <div style="display:flex;justify-content:space-between;align-items:center;border-bottom:1px solid rgba(255,255,255,0.15);padding-bottom:6px;margin-bottom:8px;">
                <span style="font-weight:800;color:#38bdf8;letter-spacing:0.5px;font-size:11px;">⚡ ANTIGRAVITY COPILOT · ${data.portal}</span>
                <span style="background:#0284c7;color:#fff;padding:2px 7px;border-radius:99px;font-weight:700;font-size:10px;">${data.status}</span>
            </div>
            <div style="margin-bottom:6px;">
                <div style="font-weight:700;font-size:13px;color:#ffffff;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;">${data.offer_title}</div>
                <div style="font-size:11px;color:#94a3b8;">${data.company ? '🏢 ' + data.company + ' · ' : ''}🎯 Calce: <b style="color:#4ade80;">${data.score}</b> · 💰 Renta: <b>${data.salary}</b></div>
            </div>
            <div style="background:rgba(16, 185, 129, 0.12);border:1px solid ${data.cv_badge_color};border-radius:8px;padding:6px 8px;margin-bottom:6px;">
                <div style="font-size:10px;font-weight:700;color:${data.cv_badge_color};">${data.cv_status_icon}</div>
                <div style="font-weight:700;font-size:11.5px;color:#ecfdf5;word-break:break-all;">${data.cv_name}</div>
            </div>
            ${data.detail ? `<div style="font-size:11px;color:#cbd5e1;margin-bottom:4px;">ℹ️ ${data.detail}</div>` : ''}
            ${data.q_html ? `<div style="margin-top:6px;border-top:1px solid rgba(255,255,255,0.1);padding-top:4px;"><div style="font-size:10px;color:#38bdf8;font-weight:700;">✍️ PREGUNTAS / RESPUESTAS IA:</div>${data.q_html}</div>` : ''}
        `;
    }"""

    try:
        await page.evaluate(js_code, payload)
    except Exception:
        pass
