"""Guardias de seguridad, Pre-Flight Check de sesión y verificación post-envío (Reglas de Oro 1 a 5)."""

from __future__ import annotations

from pathlib import Path
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page


@dataclass
class GuardVerdict:
    ok: bool
    status_code: str  # 'OK' | 'AUTH_EXPIRED' | 'CLOSED_OFFER' | 'ALREADY_APPLIED' | 'CAPTCHA_OR_EXTERNAL' | 'INVALID_FORM' | 'VALIDATION_ERROR' | 'CONFIRMED' | 'UNCERTAIN'
    reason: str


EXTERNAL_REDIRECT_DOMAINS = (
    "linkedin.com",
    "accounts.google.com",
    "facebook.com",
    "login.microsoftonline.com",
    "claveunica.gob.cl",
    "myworkdayjobs.com",
    "buk.cl",
    "airh.cl",
    "hiringroom.com",
    "teamtailor.com",
)

CAPTCHA_MARKERS = (
    "cloudflare",
    "verify you are human",
    "verifica que eres un ser humano",
    "un momento...",
    "just a moment...",
    "g-recaptcha",
    "cf-turnstile",
)


def inspect_html_for_security_barriers(url: str, body_text: str, html_content: str = "") -> GuardVerdict | None:
    """Detecta redirecciones externas o CAPTCHAs sin intentar eludirlos."""
    url_low = (url or "").lower()
    body_low = (body_text or "").lower()
    html_low = (html_content or "").lower()

    for dom in EXTERNAL_REDIRECT_DOMAINS:
        if dom in url_low:
            return GuardVerdict(
                False,
                "CAPTCHA_OR_EXTERNAL",
                f"Redirección a dominio o plataforma externa detectada ({dom})",
            )

    for marker in CAPTCHA_MARKERS:
        if marker in body_low or marker in html_low:
            # Evitar falso positivo si solo hay script pasivo de recaptcha en chiletrabajos autenticado
            if marker == "g-recaptcha" and "chiletrabajos.cl/trabajo/postular/" in url_low and 'name="salary"' in html_low:
                continue
            return GuardVerdict(
                False,
                "CAPTCHA_OR_EXTERNAL",
                f"Control de acceso o CAPTCHA detectado ({marker}); requiere intervención humana",
            )

    return None


def verify_session_state(portal: str, url: str, body_text: str) -> GuardVerdict:
    """
    Evalúa de forma pura si la sesión en el portal está autenticada (Regla 2).
    """
    p = portal.lower()
    url_low = (url or "").lower()
    body_low = (body_text or "").lower()

    barrier = inspect_html_for_security_barriers(url, body_text)
    if barrier:
        return barrier

    if p == "chiletrabajos":
        if "preingreso" in url_low or "chtlogin" in url_low or "/login" in url_low:
            return GuardVerdict(False, "AUTH_EXPIRED", f"Redirección a pantalla de login en Chiletrabajos ({url})")
        if "ingresa a tu cuenta" in body_low and "crea cuenta" in body_low and "mi cuenta" not in body_low and "salir" not in body_low:
            return GuardVerdict(False, "AUTH_EXPIRED", "Sesión no iniciada en Chiletrabajos (aparece 'Ingresa a tu cuenta')")
        return GuardVerdict(True, "OK", "Sesión activa en Chiletrabajos verificada")

    if p == "computrabajo":
        if "secure.computrabajo.com/account/login" in url_low or "/login" in url_low:
            return GuardVerdict(False, "AUTH_EXPIRED", f"Redirección a login en Computrabajo ({url})")
        if "iniciar sesión" in body_low and "mi cv" not in body_low and "mis postulaciones" not in body_low and "luis" not in body_low:
            return GuardVerdict(False, "AUTH_EXPIRED", "Sesión no iniciada en Computrabajo")
        return GuardVerdict(True, "OK", "Sesión activa en Computrabajo verificada")

    if p == "getonboard":
        if "/persons/sign_in" in url_low or "inicia sesión" in body_low:
            return GuardVerdict(False, "AUTH_EXPIRED", "Sesión no iniciada en Get on Board")
        return GuardVerdict(True, "OK", "Sesión activa en Get on Board verificada")

    return GuardVerdict(True, "OK", f"Verificación base completada para {portal}")


def verify_pre_apply_offer_state(portal: str, url: str, body_text: str, html_content: str = "", expected_job_id: str | None = None) -> GuardVerdict:
    """
    Verifica si una oferta ya fue postulada, está cerrada o el formulario es legítimo (Regla 3).
    """
    p = portal.lower()
    url_low = (url or "").lower()
    body_low = (body_text or "").lower()
    html_low = (html_content or "").lower()

    # 1. Revisar barreras o login
    session_check = verify_session_state(portal, url, body_text)
    if not session_check.ok:
        return session_check

    # 2. Revisar si ya se postuló previamente en el portal
    already_markers = (
        "ya has postulado",
        "ya te has postulado",
        "te postulaste el",
        "ya aplicaste a esta oferta",
        "postulación enviada anteriormente",
    )
    for m in already_markers:
        if m in body_low:
            return GuardVerdict(False, "ALREADY_APPLIED", f"El portal informa que ya se postuló previamente ('{m}')")

    # 3. Revisar si la oferta está cerrada o expirada
    closed_markers = (
        "página no encontrada",
        "pagina no encontrada",
        "este aviso ya no está disponible",
        "este aviso no está disponible",
        "oferta finalizada",
        "convocatoria cerrada",
        "el proceso de selección ha finalizado",
        "aviso desactivado",
    )
    for m in closed_markers:
        if m in body_low:
            return GuardVerdict(False, "CLOSED_OFFER", f"Oferta cerrada o expirada en el portal ('{m}')")

    # 4. Aserción estricta de formulario real según portal
    if p == "chiletrabajos":
        if expected_job_id and f"/trabajo/postular/{expected_job_id}" not in url_low:
            return GuardVerdict(
                False,
                "INVALID_FORM",
                f"La URL actual ({url}) no coincide con /trabajo/postular/{expected_job_id}",
            )
        # Prohibir formularios de registro general
        if "repetir clave" in body_low or ("nombres" in body_low and "apellidos" in body_low and "crear cuenta" in body_low):
            return GuardVerdict(
                False,
                "AUTH_EXPIRED",
                "Se detectó formulario de registro de cuenta en lugar del formulario de postulación",
            )
        if html_low and ('id="carta"' not in html_low and 'name="app_letter"' not in html_low and 'name="salary"' not in html_low):
            return GuardVerdict(
                False,
                "INVALID_FORM",
                "Los selectores obligatorios (#carta, input[name='salary']) no existen en el DOM",
            )

    return GuardVerdict(True, "OK", "Formulario legítimo de postulación verificado")


def verify_post_submit_confirmation(portal: str, url: str, body_text: str) -> GuardVerdict:
    """
    Aserción estricta post-envío (Regla 4): Nunca marcar CONFIRMED sin evidencia inequívoca en el DOM.
    """
    p = portal.lower()
    url_low = (url or "").lower()
    body_low = (body_text or "").lower()

    # 1. Filtro anti-redirección a login
    if "preingreso" in url_low or "login" in url_low or "welcome back" in body_low or "inicia sesión" in body_low or "sign in" in body_low:
        return GuardVerdict(
            False,
            "AUTH_EXPIRED",
            "Redirección a login detectada tras el intento de envío",
        )

    # 2. Filtro anti-errores de validación de campos (con límites de palabra para evitar falsos positivos con 'digitales requeridos')
    validation_error_patterns = (
        r"\bes requerido\b",
        r"\bcampo obligatorio\b",
        r"\brevisa los campos\b",
        r"\bdebe completar\b",
        r"\bformato inválido\b",
        r"\berror al enviar\b",
        r"\bha ocurrido un error\b",
    )
    for pat in validation_error_patterns:
        if re.search(pat, body_low):
            return GuardVerdict(
                False,
                "VALIDATION_ERROR",
                f"El formulario rechazó el envío por validación ('{pat}')",
            )

    # 3. Marcadores positivos inequívocos por portal
    if p == "chiletrabajos":
        ct_positive = (
            "postulacion exitosa",
            "postulación exitosa",
            "ya has postulado a este aviso",
            "ya te has postulado",
            "postulación enviada",
            "postulacion enviada",
            "tu postulación ha sido enviada",
            "has postulado exitosamente",
        )
        for pos in ct_positive:
            if pos in body_low:
                return GuardVerdict(True, "CONFIRMED", f"Confirmación verificada en Chiletrabajos ('{pos}')")

    elif p == "computrabajo":
        if "/candidate/postapply" in url_low or "postapply?oi=" in url_low:
            return GuardVerdict(True, "CONFIRMED", f"Confirmación verificada en Computrabajo por URL oficial post-envío ({url})")
        cp_positive = (
            "¡te has postulado!",
            "te has postulado",
            "tu postulación fue enviada",
            "postulación enviada",
            "postulacion enviada",
            "postulación realizada",
            "postulacion realizada",
            "cv enviado",
            "ya aplicaste",
            "ya te postulaste",
            "ya estás postulado",
            "ya estas postulado",
            "gracias por postular",
            "tu cv ha sido enviado",
        )
        for pos in cp_positive:
            if pos in body_low:
                # Cuidado con falsos positivos: asegurarnos de que no sea el botón "Postularme" sin confirmar
                if pos == "te has postulado" and "no te has postulado" in body_low:
                    continue
                return GuardVerdict(True, "CONFIRMED", f"Confirmación verificada en Computrabajo ('{pos}')")

    elif p == "getonboard":
        gob_positive = (
            "postulación enviada",
            "application sent",
            "ya postulaste",
            "you already applied",
        )
        for pos in gob_positive:
            if pos in body_low:
                return GuardVerdict(True, "CONFIRMED", f"Confirmación verificada en Get on Board ('{pos}')")

    return GuardVerdict(
        False,
        "UNCERTAIN",
        "No se encontró marcador inequívoco de éxito ni de error tras el envío (estado pendiente de verificación)",
    )


async def safe_page_screenshot(page: Page, path: Path) -> bool:
    try:
        if page.is_closed():
            return False
        await page.bring_to_front()
        vp = page.viewport_size
        if not vp or vp.get("width", 0) <= 0:
            await page.set_viewport_size({"width": 1280, "height": 800})
        await page.wait_for_load_state("domcontentloaded", timeout=4000)
        path.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(path), timeout=5000)
        return True
    except Exception:
        return False


async def run_preflight_check_on_page(page: Page, portal: str) -> GuardVerdict:
    """Navega al portal indicado y ejecuta la verificación activa de sesión en el navegador."""
    p = portal.lower()
    current_url = (page.url or "").lower()

    # Evitar destruir la pestaña activa del usuario: no forzar goto("candidate/home") si ya está navegando una oferta
    portal_domains = {
        "chiletrabajos": "chiletrabajos.cl",
        "computrabajo": "computrabajo.com",
        "getonboard": "getonbrd.com",
    }
    expected_domain = portal_domains.get(p)
    if expected_domain and expected_domain in current_url:
        try:
            body = await page.inner_text("body")
            return verify_session_state(portal, page.url, body)
        except Exception:
            pass

    if p == "chiletrabajos":
        target = "https://www.chiletrabajos.cl/encuentra-un-empleo"
    elif p == "computrabajo":
        target = "https://candidato.cl.computrabajo.com/candidate/home"
    elif p == "getonboard":
        target = "https://www.getonbrd.com/mis-empleos"
    else:
        return GuardVerdict(True, "OK", f"Portal {portal} sin preflight dedicado")

    await page.goto(target, wait_until="domcontentloaded", timeout=15000)
    await page.wait_for_timeout(1200)
    body = await page.inner_text("body")
    return verify_session_state(portal, page.url, body)
