"""Generador de Currículum Vitae Personalizado para filtros ATS (Applicant Tracking Systems).

Compila un CV limpio de 1 página en PDF vectorial mediante Playwright Chromium,
adaptado a cada vacante usando estrictamente los datos verificados de candidate_profile.json.
"""

from __future__ import annotations

import html
import os
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright
from playwright.sync_api import sync_playwright

from job_automator.config import get_candidate_profile, get_output_dir
from job_automator.intelligence.ai_client import call_gemini_api_realtime
from job_automator.intelligence.fact_checker import validate_text_truthfulness


class TruthfulnessViolationError(Exception):
    """Excepción lanzada cuando el contenido del CV viola las reglas de veracidad del perfil."""


class ATSCVGenerationError(Exception):
    """Excepción lanzada cuando ocurre un error crítico en la generación o compilación del CV ATS."""


@dataclass
class ATSCVResult:
    """Resultado del proceso de generación y compilación del CV ATS."""

    ok: bool
    pdf_path: Path | None
    ats_score: int
    matched_keywords: list[str]
    tailored_summary: str
    selected_projects: list[dict[str, Any]]
    error: str | None = None
    html_content: str | None = None


CANONICAL_ATS_VOCABULARY = [
    "Python", "C#", ".NET", ".NET 8", "Java", "TypeScript", "JavaScript", "SQL", "Go", "Golang", "PHP", "Bash", "Shell", "C++",
    "Visual Basic", "VB.NET", "WinForms", "VBA", "HTML5", "CSS3",
    "React", "React Native", "Next.js", "Angular", "Vue", "Node.js", "NestJS", "Spring", "Spring Boot",
    "Django", "FastAPI", "Flask", "Blazor", "ASP.NET", "Express", "Gin", "Fiber", "WordPress", "Laravel",
    "PostgreSQL", "PostGIS", "SQL Server", "Oracle", "MySQL", "MariaDB", "MongoDB", "Redis", "SQLite", "T-SQL", "PL/SQL", "SSMS",
    "Procedimientos Almacenados", "Stored Procedures", "Triggers", "Vistas",
    "Docker", "Docker Compose", "Kubernetes", "CI/CD", "Git", "GitHub Actions", "GitLab",
    "AWS", "Azure", "Azure DevOps", "GCP", "Linux", "Ubuntu", "Windows Server", "Nginx", "RabbitMQ", "Event-Driven",
    "REST", "RESTful", "APIs REST", "Microservicios", "Clean Architecture", "DDD",
    "Domain-Driven Design", "SOLID", "POO", "Patrones de Diseño", "Design Patterns", "MVC",
    "QA", "Unit Testing", "xUnit", "Jest", "PyTest", "TDD", "Automatización", "Selenium", "Playwright",
    "Soporte TI", "Mesa de Ayuda", "Redes", "Telecomunicaciones", "Smart Hands", "Switches", "Cableado Estructurado",
    "ERP", "Odoo", "Odoo ERP", "SAP", "ServiceNow", "Jira", "Confluence", "Continuidad Operativa",
    "Facturación Electrónica", "SII", "O2C", "P2P", "DTE", "CAF", "Scrum",
    "ETL", "Pandas", "Scikit-learn", "Machine Learning", "Power BI", "Minería de Datos",
    "IA Generativa", "Prompt Engineering", "LLMs", "AI Engineer", "Excel",
    "Android", "iOS", "Kotlin", "Swift", "Flutter", "Ionic",
]


def _slugify_candidate_name(candidate_name: str | None) -> str:
    if not candidate_name:
        return "Candidate"
    nfkd = unicodedata.normalize("NFKD", str(candidate_name))
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    parts = [p for p in re.split(r"[^A-Za-z0-9]+", ascii_str) if p]
    if not parts:
        return "Candidate"
    return "_".join(parts[:3])


def get_ats_cv_filename(app_id: int | None = None, candidate_name: str | None = None) -> str:
    """Retorna el nombre estándar portable para el CV ATS personalizado."""
    slug = _slugify_candidate_name(candidate_name)
    if app_id is not None:
        return f"CV_{slug}_ATS_ID{app_id}.pdf"
    return f"CV_{slug}_ATS.pdf"


def _match_keyword_token(kw: str, text: str) -> bool:
    """Verifica si la palabra clave kw está presente respetando límites de token."""
    pattern = r"(?<![a-zA-Z0-9_#+.])" + re.escape(kw.lower()) + r"(?![a-zA-Z0-9_#+.])"
    return bool(re.search(pattern, text.lower()))


def extract_ats_keywords(job_title: str, job_description: str) -> list[str]:
    """Extrae palabras clave técnicas relevantes en título y descripción de la vacante."""
    combined_text = f"{job_title or ''} {job_description or ''}"
    if not combined_text.strip():
        return []

    matched_title: list[str] = []
    matched_desc: list[str] = []

    for kw in CANONICAL_ATS_VOCABULARY:
        if _match_keyword_token(kw, job_title or ""):
            if kw not in matched_title:
                matched_title.append(kw)
        elif _match_keyword_token(kw, job_description or ""):
            if kw not in matched_desc and kw not in matched_title:
                matched_desc.append(kw)

    return matched_title + matched_desc


def calculate_ats_affinity_score(job_title: str, job_description: str, matched_keywords: list[str]) -> int:
    """Calcula un puntaje de afinidad ATS entre 70% y 98% según las coincidencias detectadas."""
    score = 72
    title_clean = (job_title or "").lower()
    desc_clean = (job_description or "").lower()

    for kw in matched_keywords:
        if _match_keyword_token(kw, title_clean):
            score += 5
        elif _match_keyword_token(kw, desc_clean):
            score += 2

    if any(role in title_clean for role in ("desarrollador", "developer", "ingeniero", "programador", "soporte", "analista")):
        score += 4

    return max(70, min(98, score))


def _find_playwright_executable() -> str | None:
    """Localiza un binario Chromium de Playwright instalado en rutas estándar de Windows/macOS/Linux."""
    roots: list[Path] = []
    custom = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if custom and custom != "0":
        roots.append(Path(custom))
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        roots.append(Path(local_app) / "ms-playwright")
    roots.append(Path.home() / ".cache" / "ms-playwright")
    roots.append(Path.home() / "Library" / "Caches" / "ms-playwright")

    patterns = (
        "chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe",
        "chromium_headless_shell-*/chrome-headless-shell-linux*/chrome-headless-shell",
        "chromium_headless_shell-*/chrome-headless-shell-mac*/chrome-headless-shell",
        "chromium-*/chrome-win64/chrome.exe",
        "chromium-*/chrome-linux*/chrome",
        "chromium-*/chrome-mac*/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    )
    for root in roots:
        if not root.exists():
            continue
        for pat in patterns:
            matches = sorted(root.glob(pat), reverse=True)
            if matches:
                return str(matches[0])
    return None


class ATSCVGenerator:
    """Generador modular de CV optimizado para filtros ATS con compilación a PDF de 1 página."""

    def __init__(self, profile: dict[str, Any] | None = None) -> None:
        self.profile = profile or get_candidate_profile()

    def extract_keywords(self, job_title: str, job_description: str) -> list[str]:
        return extract_ats_keywords(job_title, job_description)

    def calculate_score(self, job_title: str, job_description: str, matched_kws: list[str]) -> int:
        return calculate_ats_affinity_score(job_title, job_description, matched_kws)

    def synthesize_tailored_data(
        self,
        job_title: str,
        job_description: str,
        app_id: int | None = None,
    ) -> dict[str, Any]:
        """Sintetiza la estructura del CV usando estrictamente el perfil configurado."""
        matched_kws = self.extract_keywords(job_title, job_description)
        combined_text = f"{job_title} {job_description}".lower()

        summary = self._generate_tailored_summary(job_title, job_description, matched_kws)
        selected_projects = self._select_best_projects(combined_text, matched_kws)
        experiences = self._tailor_experiences(combined_text)
        skills_grouped = self._organize_skills(matched_kws)
        certifications = list(self.profile.get("authorized_certifications", []))[:6]
        education = self.profile.get("education", {})

        cv_data = {
            "full_name": self.profile.get("full_name", "Candidate Name"),
            "target_role": job_title or f"{education.get('degree', 'Software Engineer')}",
            "contact": {
                "location": self.profile.get("residence", "Remote"),
                "phone": self.profile.get("phone", ""),
                "email": self.profile.get("email", ""),
                "linkedin": self.profile.get("linkedin", ""),
                "portfolio": self.profile.get("portfolio", ""),
            },
            "summary": summary,
            "experiences": experiences,
            "projects": selected_projects,
            "skills": skills_grouped,
            "education": {
                "degree": education.get("degree", "Ingeniero en Informática"),
                "institution": education.get("institution", "Universidad"),
                "period": education.get("period", ""),
                "status": education.get("status", "Titulado"),
            },
            "certifications": certifications,
            "matched_keywords": matched_kws,
            "app_id": app_id,
        }

        self._validate_truthfulness(cv_data)
        return cv_data

    def _generate_tailored_summary(self, job_title: str, job_description: str, matched_kws: list[str]) -> str:
        """Genera un resumen profesional conciso basado exclusivamente en el perfil."""
        edu = self.profile.get("education", {})
        degree = edu.get("degree", "Ingeniero en Informática")
        institution = edu.get("institution", "Universidad")
        status = edu.get("status", "Titulado")
        years = int(self.profile.get("total_experience_years", 2))
        residence = self.profile.get("residence", "Remoto")
        availability = self.profile.get("availability", "Inmediata")
        companies = [c.get("name", "") for c in self.profile.get("authorized_companies", []) if c.get("name")]
        companies_str = ", ".join(companies) if companies else "entornos productivos"
        focus_kws = ", ".join(matched_kws[:6]) if matched_kws else "desarrollo de software, APIs y bases de datos"

        prompt = (
            f"Actúa como {self.profile.get('full_name', 'el candidato')}, {degree} ({status}) en {institution} con {years} años "
            f"de experiencia profesional real en producción ({companies_str}). Residencia: {residence}, disponibilidad {availability}. "
            f"No inventes años adicionales ni certificaciones fuera del perfil. "
            f"Redacta un resumen profesional conciso de 3 oraciones (máximo 70 palabras) para un CV ATS enfocado en '{job_title}'. "
            f"Tecnologías clave detectadas: {focus_kws}. "
            f"Responde únicamente con el párrafo del resumen, sin comillas ni encabezados."
        )

        try:
            ai_text = call_gemini_api_realtime(prompt, timeout=5.0)
            if ai_text:
                clean_ai = ai_text.strip().strip('"').strip("'")
                check = validate_text_truthfulness(clean_ai, profile=self.profile)
                if check.valid and len(clean_ai) > 80:
                    return clean_ai
        except Exception:
            pass

        mobility = self.profile.get("strategy_guidelines", {}).get(
            "mobility_directive",
            f"Residencia en {residence} con disponibilidad {availability.lower()}.",
        )
        return (
            f"{degree} ({status}) en {institution} con {years} años de experiencia profesional en entornos de producción "
            f"({companies_str}). Especializado en {focus_kws}, aplicando principios Clean Code, arquitectura modular "
            f"y automatización de procesos operativos. {mobility}"
        )

    def _select_best_projects(self, combined_text: str, matched_kws: list[str]) -> list[dict[str, Any]]:
        """Selecciona y prioriza hasta 2 proyectos autorizados según afinidad con la oferta."""
        all_projects = self.profile.get("authorized_projects", [])
        scored: list[tuple[int, dict[str, Any]]] = []

        for p in all_projects:
            stack = p.get("stack", [])
            desc = str(p.get("description", ""))
            p_score = 0
            for tech in stack:
                if _match_keyword_token(str(tech), combined_text):
                    p_score += 4
                for kw in matched_kws:
                    if _match_keyword_token(kw, str(tech)):
                        p_score += 3
            for kw in matched_kws:
                if _match_keyword_token(kw, desc):
                    p_score += 2
            scored.append((p_score, p))

        scored.sort(key=lambda x: x[0], reverse=True)

        selected: list[dict[str, Any]] = []
        for _, p in scored[:2]:
            stack_list = [str(s) for s in p.get("stack", [])]
            selected.append({
                "name": p.get("name", "Proyecto"),
                "stack": ", ".join(stack_list),
                "description": p.get("description") or f"Proyecto desarrollado con {', '.join(stack_list)}.",
            })
        return selected

    def _tailor_experiences(self, combined_text: str) -> list[dict[str, Any]]:
        """Construye las experiencias laborales autorizadas directamente desde candidate_profile."""
        experiences: list[dict[str, Any]] = []
        for comp in self.profile.get("authorized_companies", []):
            bullets = list(comp.get("bullets") or [])
            if not bullets and comp.get("summary"):
                bullets = [str(comp["summary"])]
            experiences.append({
                "company": comp.get("name", ""),
                "role": comp.get("role", "Ingeniero de Software"),
                "period": comp.get("period", ""),
                "bullets": bullets[:2],
            })
        return experiences

    def _organize_skills(self, matched_kws: list[str]) -> dict[str, list[str]]:
        """Organiza competencias técnicas desde el perfil priorizando coincidencias ATS."""
        raw_skills = self.profile.get("skills")
        if not isinstance(raw_skills, dict) or not raw_skills:
            project_tokens: list[str] = []
            for p in self.profile.get("authorized_projects", []):
                for item in p.get("stack", []):
                    if item not in project_tokens:
                        project_tokens.append(str(item))
            raw_skills = {"Competencias Técnicas": project_tokens or ["Python", "SQL", "Git", "Docker"]}

        def _sort_prioritizing_matches(base_list: list[str]) -> list[str]:
            prioritized: list[str] = []
            remaining: list[str] = []
            for item in base_list:
                item_low = str(item).lower()
                if any(_match_keyword_token(kw, item_low) for kw in matched_kws):
                    prioritized.append(str(item))
                else:
                    remaining.append(str(item))
            return prioritized + remaining

        return {
            str(cat): _sort_prioritizing_matches(list(items))
            for cat, items in raw_skills.items()
            if items
        }

    def _validate_truthfulness(self, cv_data: dict[str, Any]) -> None:
        """Comprueba que el contenido compilado respete las reglas de veracidad del perfil."""
        text_blobs = [
            cv_data.get("summary", ""),
            cv_data.get("target_role", ""),
        ]
        for exp in cv_data.get("experiences", []):
            text_blobs.append(exp.get("company", ""))
            text_blobs.append(exp.get("role", ""))
            text_blobs.extend(exp.get("bullets", []))

        for proj in cv_data.get("projects", []):
            text_blobs.append(proj.get("name", ""))
            text_blobs.append(proj.get("description", ""))

        for skills in cv_data.get("skills", {}).values():
            text_blobs.append(", ".join(skills))

        text_blobs.extend(cv_data.get("certifications", []))

        full_compiled_text = "\n".join(text_blobs)
        check = validate_text_truthfulness(full_compiled_text, profile=self.profile)
        if not check.valid:
            raise TruthfulnessViolationError(
                f"Violación de veracidad en la generación del CV: {'; '.join(check.violations)}"
            )

    def render_html(self, cv_data: dict[str, Any]) -> str:
        """Compila los datos en una plantilla HTML5/CSS3 minimalista de 1 columna estándar ATS."""
        full_name = html.escape(cv_data["full_name"])
        target_role = html.escape(cv_data["target_role"])
        contact = cv_data["contact"]
        contact_parts = [
            contact.get("location", ""),
            contact.get("phone", ""),
            contact.get("email", ""),
            contact.get("linkedin", ""),
            contact.get("portfolio", ""),
        ]
        contact_line = " | ".join(html.escape(p) for p in contact_parts if p)
        if cv_data.get("app_id"):
            contact_line += f' | <span style="white-space: nowrap;">ID: #{cv_data["app_id"]}</span>'
        summary = html.escape(cv_data["summary"])

        exp_html_parts = []
        for exp in cv_data.get("experiences", []):
            c_name = html.escape(exp["company"])
            c_role = html.escape(exp["role"])
            c_period = html.escape(exp["period"])
            bullets_html = "".join(f"<li>{html.escape(b)}</li>" for b in exp.get("bullets", []))
            exp_html_parts.append(f"""
            <div class="item">
              <div class="item-head">
                <span class="item-title">{c_name} &mdash; {c_role}</span>
                <span class="item-date">{c_period}</span>
              </div>
              <ul class="bullets">{bullets_html}</ul>
            </div>""")
        experiences_html = "\n".join(exp_html_parts)

        proj_html_parts = []
        for p in cv_data.get("projects", []):
            p_name = html.escape(p["name"])
            p_stack = html.escape(p["stack"])
            p_desc = html.escape(p["description"])
            proj_html_parts.append(f"""
            <div class="item">
              <div class="item-head">
                <span class="item-title">{p_name}</span>
                <span class="item-date">{p_stack}</span>
              </div>
              <p class="project-desc">{p_desc}</p>
            </div>""")
        projects_html = "\n".join(proj_html_parts)

        skills_html_parts = []
        for cat_name, skill_items in cv_data.get("skills", {}).items():
            skills_html_parts.append(f"""
            <div class="skill-row">
              <span class="skill-category">{html.escape(cat_name)}:</span>
              <span class="skill-values">{html.escape(', '.join(skill_items))}</span>
            </div>""")
        skills_html = "\n".join(skills_html_parts)

        edu = cv_data["education"]
        edu_degree = html.escape(edu["degree"])
        edu_inst = html.escape(edu["institution"])
        edu_period = html.escape(edu["period"])
        edu_status = html.escape(edu["status"])
        certs_str = html.escape(" | ".join(cv_data.get("certifications", [])))

        return f"""<!DOCTYPE html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <title>{full_name} - {target_role}</title>
  <style>
    @page {{
      size: letter;
      margin: 8mm 11mm 8mm 11mm;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
      color: #0f172a;
      font-size: 8.6pt;
      line-height: 1.3;
      background-color: #ffffff;
    }}
    .header {{
      text-align: left;
      border-bottom: 1.5px solid #0f172a;
      padding-bottom: 4px;
      margin-bottom: 6px;
    }}
    .name {{ font-size: 15.5pt; font-weight: 700; color: #0f172a; }}
    .target-role {{ font-size: 9.8pt; font-weight: 600; color: #1e3a8a; margin-top: 1px; }}
    .contact-info {{ font-size: 8pt; color: #334155; margin-top: 2px; }}
    .section {{ margin-top: 6px; page-break-inside: avoid; break-inside: avoid; }}
    .section-heading {{
      font-size: 9pt;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 0.4px;
      color: #0f172a;
      border-bottom: 1px solid #cbd5e1;
      padding-bottom: 1.5px;
      margin-bottom: 3.5px;
    }}
    .item {{ margin-bottom: 3.5px; }}
    .item-head {{
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      font-size: 8.6pt;
      margin-bottom: 1px;
      gap: 8px;
    }}
    .item-title {{ font-weight: 600; color: #0f172a; }}
    .item-date {{ font-size: 7.9pt; color: #475569; }}
    ul.bullets {{ padding-left: 14px; list-style-type: disc; }}
    ul.bullets li {{ font-size: 8.3pt; color: #1e293b; margin-bottom: 1px; }}
    .summary-text, .project-desc {{ font-size: 8.4pt; color: #1e293b; line-height: 1.28; }}
    .skill-row {{ font-size: 8.3pt; line-height: 1.28; color: #1e293b; margin-bottom: 1px; }}
    .skill-category {{ font-weight: 600; color: #0f172a; }}
    .education-block {{ font-size: 8.4pt; }}
    .cert-line {{ font-size: 8pt; color: #334155; margin-top: 2px; }}
  </style>
</head>
<body>
  <div class="header">
    <div class="name">{full_name}</div>
    <div class="target-role">{target_role}</div>
    <div class="contact-info">{contact_line}</div>
  </div>
  <div class="section">
    <div class="section-heading">Resumen Profesional</div>
    <p class="summary-text">{summary}</p>
  </div>
  <div class="section">
    <div class="section-heading">Experiencia Laboral</div>
    {experiences_html}
  </div>
  <div class="section">
    <div class="section-heading">Proyectos Destacados</div>
    {projects_html}
  </div>
  <div class="section">
    <div class="section-heading">Competencias Técnicas</div>
    {skills_html}
  </div>
  <div class="section">
    <div class="section-heading">Educación y Certificaciones</div>
    <div class="education-block">
      <div class="item-head">
        <span class="item-title">{edu_inst} &mdash; {edu_degree} ({edu_status})</span>
        <span class="item-date">{edu_period}</span>
      </div>
      <div class="cert-line"><b>Certificaciones e Idiomas:</b> {certs_str}</div>
    </div>
  </div>
</body>
</html>"""

    def compile_pdf(self, html_content: str, output_path: Path) -> Path:
        """Compila sincrónicamente el HTML a PDF usando Playwright Chromium."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with sync_playwright() as p:
                try:
                    browser = p.chromium.launch(headless=True)
                except Exception:
                    exe = _find_playwright_executable()
                    if not exe:
                        raise
                    browser = p.chromium.launch(headless=True, executable_path=exe)
                page = browser.new_page()
                page.set_content(html_content, wait_until="load")
                page.pdf(
                    path=str(output_path),
                    format="Letter",
                    print_background=True,
                    prefer_css_page_size=True,
                    margin={"top": "8mm", "bottom": "8mm", "left": "11mm", "right": "11mm"},
                )
                browser.close()
        except Exception as exc:
            raise ATSCVGenerationError(f"Error compilando PDF con Playwright: {exc}") from exc

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise ATSCVGenerationError(f"El archivo PDF generado en {output_path} está vacío o no se creó.")

        return output_path

    async def async_compile_pdf(self, html_content: str, output_path: Path) -> Path:
        """Compila asincrónicamente el HTML a PDF usando Playwright Chromium."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            async with async_playwright() as p:
                try:
                    browser = await p.chromium.launch(headless=True)
                except Exception:
                    exe = _find_playwright_executable()
                    if not exe:
                        raise
                    browser = await p.chromium.launch(headless=True, executable_path=exe)
                page = await browser.new_page()
                await page.set_content(html_content, wait_until="load")
                await page.pdf(
                    path=str(output_path),
                    format="Letter",
                    print_background=True,
                    prefer_css_page_size=True,
                    margin={"top": "8mm", "bottom": "8mm", "left": "11mm", "right": "11mm"},
                )
                await browser.close()
        except Exception as exc:
            raise ATSCVGenerationError(f"Error compilando PDF con Playwright asíncrono: {exc}") from exc

        if not output_path.exists() or output_path.stat().st_size == 0:
            raise ATSCVGenerationError(f"El archivo PDF generado en {output_path} está vacío o no se creó.")

        return output_path

    def _resolve_output_path(self, output_path: Path | None, company_name: str, job_title: str, app_id: int | None) -> Path:
        cand_name = self.profile.get("full_name")
        if output_path is None:
            from job_automator.reporting.dossier_writer import clean_folder_name
            folder_name = clean_folder_name(company_name or "Empresa", job_title or "Cargo", app_id=app_id)
            return get_output_dir() / folder_name / get_ats_cv_filename(app_id, candidate_name=cand_name)
        if output_path.is_dir():
            return output_path / get_ats_cv_filename(app_id, candidate_name=cand_name)
        return output_path

    def generate(
        self,
        job_title: str,
        job_description: str,
        output_path: Path | None = None,
        company_name: str = "",
        app_id: int | None = None,
    ) -> ATSCVResult:
        """Orquesta sincrónicamente el flujo completo de generación de CV ATS."""
        try:
            cv_data = self.synthesize_tailored_data(job_title, job_description, app_id=app_id)
            ats_score = self.calculate_score(job_title, job_description, cv_data["matched_keywords"])
            html_content = self.render_html(cv_data)
            target_pdf = self._resolve_output_path(output_path, company_name, job_title, app_id)
            pdf_file = self.compile_pdf(html_content, target_pdf)
            return ATSCVResult(
                ok=True,
                pdf_path=pdf_file,
                ats_score=ats_score,
                matched_keywords=cv_data["matched_keywords"],
                tailored_summary=cv_data["summary"],
                selected_projects=cv_data["projects"],
                html_content=html_content,
            )
        except (TruthfulnessViolationError, ATSCVGenerationError) as err:
            return ATSCVResult(
                ok=False, pdf_path=None, ats_score=0, matched_keywords=[],
                tailored_summary="", selected_projects=[], error=str(err),
            )
        except Exception as exc:
            return ATSCVResult(
                ok=False, pdf_path=None, ats_score=0, matched_keywords=[],
                tailored_summary="", selected_projects=[],
                error=f"Fallo inesperado al generar CV ATS: {exc}",
            )

    async def async_generate(
        self,
        job_title: str,
        job_description: str,
        output_path: Path | None = None,
        company_name: str = "",
        app_id: int | None = None,
    ) -> ATSCVResult:
        """Orquesta asincrónicamente el flujo completo de generación de CV ATS."""
        try:
            cv_data = self.synthesize_tailored_data(job_title, job_description, app_id=app_id)
            ats_score = self.calculate_score(job_title, job_description, cv_data["matched_keywords"])
            html_content = self.render_html(cv_data)
            target_pdf = self._resolve_output_path(output_path, company_name, job_title, app_id)
            pdf_file = await self.async_compile_pdf(html_content, target_pdf)
            return ATSCVResult(
                ok=True,
                pdf_path=pdf_file,
                ats_score=ats_score,
                matched_keywords=cv_data["matched_keywords"],
                tailored_summary=cv_data["summary"],
                selected_projects=cv_data["projects"],
                html_content=html_content,
            )
        except (TruthfulnessViolationError, ATSCVGenerationError) as err:
            return ATSCVResult(
                ok=False, pdf_path=None, ats_score=0, matched_keywords=[],
                tailored_summary="", selected_projects=[], error=str(err),
            )
        except Exception as exc:
            return ATSCVResult(
                ok=False, pdf_path=None, ats_score=0, matched_keywords=[],
                tailored_summary="", selected_projects=[],
                error=f"Fallo inesperado al generar CV ATS: {exc}",
            )


def generate_ats_cv_for_job(
    job_title: str,
    job_description: str,
    output_path: Path | None = None,
    company_name: str = "",
    app_id: int | None = None,
    profile: dict[str, Any] | None = None,
) -> ATSCVResult:
    """Genera de forma sincrónica el CV personalizado ATS para una oferta."""
    return ATSCVGenerator(profile=profile).generate(
        job_title=job_title,
        job_description=job_description,
        output_path=output_path,
        company_name=company_name,
        app_id=app_id,
    )


async def async_generate_ats_cv_for_job(
    job_title: str,
    job_description: str,
    output_path: Path | None = None,
    company_name: str = "",
    app_id: int | None = None,
    profile: dict[str, Any] | None = None,
) -> ATSCVResult:
    """Genera de forma asincrónica el CV personalizado ATS para una oferta."""
    return await ATSCVGenerator(profile=profile).async_generate(
        job_title=job_title,
        job_description=job_description,
        output_path=output_path,
        company_name=company_name,
        app_id=app_id,
    )


def synthesize_tailored_cv_data(
    job_title: str,
    job_description: str,
    profile: dict[str, Any] | None = None,
    app_id: int | None = None,
) -> dict[str, Any]:
    """Sintetiza la estructura de datos del CV adaptada a la oferta."""
    return ATSCVGenerator(profile=profile).synthesize_tailored_data(
        job_title, job_description, app_id=app_id
    )


def render_html(cv_data: dict[str, Any]) -> str:
    """Renderiza el HTML ATS para un diccionario de datos del CV."""
    return ATSCVGenerator().render_html(cv_data)


def compile_pdf(html_content: str, output_path: Path) -> Path:
    """Compila el HTML a PDF usando Playwright Chromium."""
    return ATSCVGenerator().compile_pdf(html_content, output_path)


async def async_compile_pdf(html_content: str, output_path: Path) -> Path:
    """Compila asincrónicamente el HTML a PDF usando Playwright Chromium."""
    return await ATSCVGenerator().async_compile_pdf(html_content, output_path)
