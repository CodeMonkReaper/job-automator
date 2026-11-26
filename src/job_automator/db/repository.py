"""Repositorio SQLite transaccional para ofertas, sesiones, postulaciones y caché."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from job_automator.config import (
    SCHEMA_PATH,
    ensure_directories,
    get_data_dir,
    get_db_path,
    get_search_preferences,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_slug(text: str) -> str:
    """Normaliza un texto para comparación y huella de deduplicación."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    cleaned = re.sub(r"[^a-z0-9]+", "-", ascii_str.lower()).strip("-")
    return cleaned


def compute_fingerprint(portal: str, external_job_id: str | None, company: str, title: str) -> str:
    """Genera una huella determinista basada en (company_slug, title_slug) o external_id."""
    c_slug = normalize_slug(company)
    t_slug = normalize_slug(title)
    if c_slug and c_slug not in ("confidencial", "importante-empresa", "empresa") and t_slug:
        raw = f"role:{c_slug}::{t_slug}"
    elif external_job_id:
        raw = f"id:{portal.lower()}::{external_job_id}"
    else:
        raw = f"fallback:{portal.lower()}::{c_slug}::{t_slug}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def canonicalize_url(url: str) -> str:
    """Limpia fragmentos y parámetros de tracking de una URL."""
    if not url:
        return ""
    base = url.split("#")[0].strip()
    if "chiletrabajos.cl" in base:
        m = re.search(r"(\d{6,8})(?:$|/|\?)", base)
        if m:
            return f"https://www.chiletrabajos.cl/trabajo/postular/{m.group(1)}"
    return base.rstrip("/")


class JobRepository:
    """Capa de acceso a datos sobre SQLite con soporte WAL y respaldo espejo."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        ensure_directories()
        self.db_path = Path(db_path) if db_path else get_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
        with self.connect() as conn:
            conn.executescript(schema_sql)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA busy_timeout = 10000;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def backup_to_workspace(self) -> Path:
        """Realiza una copia segura de jobs.db hacia data/jobs_backup.db."""
        ensure_directories()
        backup_path = get_data_dir() / "jobs_backup.db"
        if self.db_path.exists() and self.db_path.resolve() != backup_path.resolve():
            shutil.copy2(self.db_path, backup_path)
        return backup_path

    # -------------------------------------------------------------------------
    # Sesiones de Trabajo (WORK_SESSIONS)
    # -------------------------------------------------------------------------
    def create_session(
        self,
        session_id: str,
        mode: str = "LIVE",
        salary_strategy: str = "FAST_HIRE",
        max_applications: int = 10,
        max_ai_calls: int = 20,
        portals: list[str] | None = None,
        locations: list[str] | None = None,
        worker_pid: int | None = None,
    ) -> dict[str, Any]:
        now = utc_now_iso()
        portals_json = json.dumps(portals or ["chiletrabajos", "computrabajo"], ensure_ascii=False)
        prefs = get_search_preferences()
        fallback_locs = prefs.get("regions", {}).get("target_locations") or ["Valparaíso", "Antofagasta", "La Serena", "Rancagua", "Talca", "Temuco", "Puerto Montt"]
        locations_json = json.dumps(locations or fallback_locs, ensure_ascii=False)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO work_sessions (
                    session_id, mode, status, salary_strategy, worker_pid,
                    heartbeat_at, max_applications, max_ai_calls, ai_calls_used,
                    portals_json, locations_json, started_at
                ) VALUES (?, ?, 'RUNNING', ?, ?, ?, ?, ?, 0, ?, ?, ?)
                """,
                (
                    session_id,
                    mode,
                    salary_strategy,
                    worker_pid,
                    now,
                    max_applications,
                    max_ai_calls,
                    portals_json,
                    locations_json,
                    now,
                ),
            )
        return self.get_session(session_id)  # type: ignore[return-value]

    def get_session(self, session_id: str | None = None) -> dict[str, Any] | None:
        with self.connect() as conn:
            if session_id:
                row = conn.execute(
                    "SELECT * FROM work_sessions WHERE session_id = ?", (session_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM work_sessions ORDER BY started_at DESC LIMIT 1"
                ).fetchone()
            return dict(row) if row else None

    def get_active_session(self) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM work_sessions
                WHERE status IN ('RUNNING', 'PAUSED', 'PAUSED_AUTH_REQUIRED', 'STOP_REQUESTED')
                ORDER BY started_at DESC LIMIT 1
                """
            ).fetchone()
            return dict(row) if row else None

    def update_session_status(
        self,
        session_id: str,
        status: str,
        notes: str | None = None,
        worker_pid: int | None = None,
        mode: str | None = None,
    ) -> dict[str, Any] | None:
        now = utc_now_iso()
        ended_at = now if status in ("STOPPED", "COMPLETED", "ABORTED") else None
        with self.connect() as conn:
            if ended_at:
                conn.execute(
                    """
                    UPDATE work_sessions
                    SET status = ?, notes = COALESCE(?, notes), ended_at = ?, heartbeat_at = ?
                    WHERE session_id = ?
                    """,
                    (status, notes, ended_at, now, session_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE work_sessions
                    SET status = ?,
                        notes = COALESCE(?, notes),
                        worker_pid = COALESCE(?, worker_pid),
                        mode = COALESCE(?, mode),
                        heartbeat_at = ?
                    WHERE session_id = ?
                    """,
                    (status, notes, worker_pid, mode, now, session_id),
                )
        return self.get_session(session_id)

    def touch_heartbeat(self, session_id: str, worker_pid: int | None = None) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            if worker_pid is not None:
                conn.execute(
                    "UPDATE work_sessions SET heartbeat_at = ?, worker_pid = ? WHERE session_id = ?",
                    (now, worker_pid, session_id),
                )
            else:
                conn.execute(
                    "UPDATE work_sessions SET heartbeat_at = ? WHERE session_id = ?",
                    (now, session_id),
                )

    # -------------------------------------------------------------------------
    # Ofertas y Deduplicación (JOB_OFFERS & APPLICATIONS)
    # -------------------------------------------------------------------------
    def is_offer_duplicate(
        self,
        portal: str,
        external_job_id: str | None,
        url: str,
        company: str,
        title: str,
    ) -> tuple[bool, str | None]:
        """Verifica si una oferta ya fue procesada por ID externo, URL canónica o huella."""
        canon_url = canonicalize_url(url)
        fp = compute_fingerprint(portal, external_job_id, company, title)
        with self.connect() as conn:
            if external_job_id:
                row = conn.execute(
                    "SELECT id FROM job_offers WHERE portal = ? AND external_job_id = ?",
                    (portal.lower(), str(external_job_id)),
                ).fetchone()
                if row:
                    return True, f"external_job_id duplicado ({portal}:{external_job_id})"

            if canon_url:
                row = conn.execute(
                    "SELECT id FROM job_offers WHERE canonical_url = ?",
                    (canon_url,),
                ).fetchone()
                if row:
                    return True, f"canonical_url duplicada ({canon_url})"

            # Verificar huella solo si ya existe una postulación confirmada o en curso
            row = conn.execute(
                """
                SELECT jo.id, a.state
                FROM job_offers jo
                LEFT JOIN applications a ON a.job_offer_id = jo.id
                WHERE jo.fingerprint_hash = ?
                  AND a.state IN ('CONFIRMED', 'SUBMITTED_UNVERIFIED', 'ALREADY_APPLIED_PORTAL', 'APPLYING', 'SIMULATED_READY', 'READY_TO_APPLY')
                """,
                (fp,),
            ).fetchone()
            if row:
                return True, f"huella empresa+cargo ya procesada en estado {row['state']}"

        return False, None

    def register_offer_and_application(
        self,
        portal: str,
        url: str,
        company_name: str,
        title: str,
        external_job_id: str | None = None,
        apply_url: str | None = None,
        location: str | None = None,
        modality: str | None = None,
        raw_salary_text: str | None = None,
        description_text: str | None = None,
        match_score: float = 0.0,
        match_decision: str = "PENDING",
        rejection_reason: str | None = None,
        session_id: str | None = None,
        initial_state: str = "DISCOVERED",
        selected_cv_base: str | None = None,
        selected_cv_target: str | None = None,
        folder_name: str | None = None,
        folder_path: str | None = None,
        expected_salary_clp: int = 800000,
        cover_letter_text: str | None = None,
        killer_questions: list[dict[str, Any]] | None = None,
        killer_answers: dict[str, str] | None = None,
        is_legacy_imported: bool = False,
        confirmed_at: str | None = None,
    ) -> tuple[int, int]:
        """Inserta o recupera una oferta y su registro en applications."""
        now = utc_now_iso()
        canon_url = canonicalize_url(url)
        fp = compute_fingerprint(portal, external_job_id, company_name, title)
        desc_hash = (
            hashlib.sha256(description_text.strip().encode("utf-8")).hexdigest()
            if description_text
            else None
        )

        with self.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM job_offers WHERE canonical_url = ?",
                (canon_url,),
            ).fetchone()

            if existing:
                offer_id = int(existing["id"])
            else:
                cur = conn.execute(
                    """
                    INSERT INTO job_offers (
                        portal, external_job_id, canonical_url, apply_url,
                        company_name, title, fingerprint_hash, location, modality,
                        raw_salary_text, description_text, description_hash,
                        match_score, match_decision, rejection_reason, discovered_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        portal.lower(),
                        str(external_job_id) if external_job_id else None,
                        canon_url,
                        apply_url or canon_url,
                        company_name,
                        title,
                        fp,
                        location,
                        modality,
                        raw_salary_text,
                        description_text,
                        desc_hash,
                        match_score,
                        match_decision,
                        rejection_reason,
                        now,
                    ),
                )
                offer_id = int(cur.lastrowid)

            existing_app = conn.execute(
                "SELECT id FROM applications WHERE job_offer_id = ?",
                (offer_id,),
            ).fetchone()

            if existing_app:
                app_id = int(existing_app["id"])
            else:
                cur_app = conn.execute(
                    """
                    INSERT INTO applications (
                        job_offer_id, session_id, state, selected_cv_base,
                        selected_cv_target, folder_name, folder_path,
                        expected_salary_clp, cover_letter_text,
                        killer_questions_json, killer_answers_json,
                        is_legacy_imported, created_at, updated_at, confirmed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        offer_id,
                        session_id,
                        initial_state,
                        selected_cv_base,
                        selected_cv_target,
                        folder_name,
                        folder_path,
                        expected_salary_clp,
                        cover_letter_text,
                        json.dumps(killer_questions or [], ensure_ascii=False),
                        json.dumps(killer_answers or {}, ensure_ascii=False),
                        1 if is_legacy_imported else 0,
                        now,
                        now,
                        confirmed_at,
                    ),
                )
                app_id = int(cur_app.lastrowid)
                conn.execute(
                    """
                    INSERT INTO state_transitions (application_id, from_state, to_state, reason, transitioned_at)
                    VALUES (?, NULL, ?, ?, ?)
                    """,
                    (app_id, initial_state, "Creación inicial", now),
                )

        return offer_id, app_id

    def transition_application_state(
        self,
        application_id: int,
        new_state: str,
        reason: str = "",
        last_step_reached: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        increment_attempt: bool = False,
        extra_updates: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Actualiza el estado de una postulación registrando su transición histórica."""
        now = utc_now_iso()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT state, attempt_count FROM applications WHERE id = ?",
                (application_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"Application {application_id} no existe")

            old_state = row["state"]
            attempts = int(row["attempt_count"]) + (1 if increment_attempt else 0)
            confirmed_at = now if new_state == "CONFIRMED" else None

            fields = [
                "state = ?",
                "attempt_count = ?",
                "updated_at = ?",
            ]
            params: list[Any] = [new_state, attempts, now]

            if last_step_reached is not None:
                fields.append("last_step_reached = ?")
                params.append(last_step_reached)
            if error_code is not None:
                fields.append("last_error_code = ?")
                params.append(error_code)
            if error_message is not None:
                fields.append("last_error_message = ?")
                params.append(error_message)
            if confirmed_at is not None:
                fields.append("confirmed_at = ?")
                params.append(confirmed_at)

            if extra_updates:
                for k, v in extra_updates.items():
                    fields.append(f"{k} = ?")
                    params.append(v)

            params.append(application_id)
            conn.execute(
                f"UPDATE applications SET {', '.join(fields)} WHERE id = ?",
                params,
            )
            conn.execute(
                """
                INSERT INTO state_transitions (application_id, from_state, to_state, reason, transitioned_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (application_id, old_state, new_state, reason, now),
            )
        return self.get_application(application_id)  # type: ignore[return-value]

    def record_attempt(
        self,
        application_id: int,
        attempt_number: int,
        step_reached: str,
        outcome: str,
        error_type: str | None = None,
        diagnostic: dict[str, Any] | None = None,
        current_url: str | None = None,
    ) -> int:
        now = utc_now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO application_attempts (
                    application_id, attempt_number, step_reached, outcome,
                    error_type, diagnostic_json, current_url_at_end, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    application_id,
                    attempt_number,
                    step_reached,
                    outcome,
                    error_type,
                    json.dumps(diagnostic or {}, ensure_ascii=False),
                    current_url,
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def add_evidence(
        self,
        application_id: int,
        evidence_type: str,
        file_path: str | Path,
        verified_valid: bool = True,
    ) -> int:
        now = utc_now_iso()
        p = Path(file_path)
        sha = None
        if p.exists() and p.is_file():
            sha = hashlib.sha256(p.read_bytes()).hexdigest()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO application_evidence (
                    application_id, evidence_type, file_path, file_sha256, verified_valid, captured_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    application_id,
                    evidence_type,
                    str(p),
                    sha,
                    1 if verified_valid else 0,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def get_application(self, application_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT a.*, jo.portal, jo.external_job_id, jo.canonical_url, jo.apply_url,
                       jo.company_name, jo.title, jo.location, jo.modality,
                       jo.raw_salary_text, jo.description_text, jo.match_score
                FROM applications a
                JOIN job_offers jo ON jo.id = a.job_offer_id
                WHERE a.id = ?
                """,
                (application_id,),
            ).fetchone()
            return dict(row) if row else None

    def list_applications(
        self,
        session_id: str | None = None,
        states: list[str] | None = None,
        portals: list[str] | None = None,
        include_legacy: bool = True,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if session_id:
            clauses.append("a.session_id = ?")
            params.append(session_id)
        if not include_legacy:
            clauses.append("a.is_legacy_imported = 0")
        if states:
            placeholders = ",".join("?" for _ in states)
            clauses.append(f"a.state IN ({placeholders})")
            params.extend(states)
        if portals:
            p_placeholders = ",".join("?" for _ in portals)
            clauses.append(f"LOWER(jo.portal) IN ({p_placeholders})")
            params.extend([p.lower() for p in portals])
        params.append(limit)
        query = f"""
            SELECT a.*, jo.portal, jo.external_job_id, jo.canonical_url, jo.apply_url,
                   jo.company_name, jo.title, jo.location, jo.modality,
                   jo.raw_salary_text, jo.description_text, jo.match_score
            FROM applications a
            JOIN job_offers jo ON jo.id = a.job_offer_id
            WHERE {' AND '.join(clauses)}
            ORDER BY
                CASE WHEN a.killer_answers_json IS NOT NULL AND a.killer_answers_json != '{{}}' THEN 0 ELSE 1 END ASC,
                CASE jo.portal
                    WHEN 'chiletrabajos' THEN 1
                    WHEN 'computrabajo' THEN 2
                    WHEN 'laborum' THEN 3
                    ELSE 4
                END ASC,
                jo.match_score DESC,
                a.updated_at DESC
            LIMIT ?
        """
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
            return [dict(r) for r in rows]

    def get_counts_by_state(self, session_id: str | None = None, include_legacy: bool = True) -> dict[str, int]:
        clauses = ["1=1"]
        params: list[Any] = []
        if session_id:
            clauses.append("session_id = ?")
            params.append(session_id)
        if not include_legacy:
            clauses.append("is_legacy_imported = 0")
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT state, COUNT(*) AS cnt FROM applications WHERE {' AND '.join(clauses)} GROUP BY state",
                params,
            ).fetchall()
            return {r["state"]: int(r["cnt"]) for r in rows}

    def get_counts_by_portal(self, confirmed_only: bool = False) -> dict[str, int]:
        where = "WHERE a.state = 'CONFIRMED'" if confirmed_only else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT jo.portal, COUNT(*) AS cnt
                FROM applications a
                JOIN job_offers jo ON jo.id = a.job_offer_id
                {where}
                GROUP BY jo.portal
                ORDER BY cnt DESC
                """
            ).fetchall()
            return {r["portal"]: int(r["cnt"]) for r in rows}

    # -------------------------------------------------------------------------
    # Caché Determinista / IA (AI_CACHE_ENTRIES)
    # -------------------------------------------------------------------------
    def get_cached_answer(self, task_type: str, normalized_input: str) -> dict[str, Any] | None:
        key = hashlib.sha256(f"{task_type}::{normalized_input.strip().lower()}".encode("utf-8")).hexdigest()
        with self.connect() as conn:
            row = conn.execute(
                "SELECT output_json, source FROM ai_cache_entries WHERE input_hash = ?",
                (key,),
            ).fetchone()
            if row:
                return {"output": json.loads(row["output_json"]), "source": row["source"]}
            return None

    def set_cached_answer(
        self,
        task_type: str,
        normalized_input: str,
        output: Any,
        source: str = "MCP_AGENT",
    ) -> str:
        key = hashlib.sha256(f"{task_type}::{normalized_input.strip().lower()}".encode("utf-8")).hexdigest()
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO ai_cache_entries (input_hash, task_type, normalized_input, output_json, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(input_hash) DO UPDATE SET
                    output_json = excluded.output_json,
                    source = excluded.source,
                    created_at = excluded.created_at
                """,
                (key, task_type, normalized_input.strip().lower(), json.dumps(output, ensure_ascii=False), source, now),
            )
        return key
