PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;

CREATE TABLE IF NOT EXISTS work_sessions (
    session_id TEXT PRIMARY KEY,
    mode TEXT NOT NULL CHECK (mode IN ('DRY_RUN', 'LIVE')),
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'PAUSED', 'PAUSED_AUTH_REQUIRED', 'STOP_REQUESTED', 'STOPPED', 'COMPLETED', 'ABORTED')),
    salary_strategy TEXT NOT NULL DEFAULT 'FAST_HIRE',
    worker_pid INTEGER,
    heartbeat_at TEXT,
    max_applications INTEGER NOT NULL DEFAULT 10,
    max_ai_calls INTEGER NOT NULL DEFAULT 20,
    ai_calls_used INTEGER NOT NULL DEFAULT 0,
    portals_json TEXT NOT NULL DEFAULT '["chiletrabajos", "computrabajo"]',
    locations_json TEXT NOT NULL DEFAULT '["Remote"]',
    notes TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS job_offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portal TEXT NOT NULL,
    external_job_id TEXT,
    canonical_url TEXT NOT NULL UNIQUE,
    apply_url TEXT,
    company_name TEXT NOT NULL,
    title TEXT NOT NULL,
    fingerprint_hash TEXT NOT NULL,
    location TEXT,
    modality TEXT,
    raw_salary_text TEXT,
    description_text TEXT,
    description_hash TEXT,
    match_score REAL DEFAULT 0.0,
    match_decision TEXT NOT NULL DEFAULT 'PENDING' CHECK (match_decision IN ('PENDING', 'ELIGIBLE', 'REJECTED_FILTER', 'REJECTED_PROFILE', 'CLOSED_OFFER')),
    rejection_reason TEXT,
    discovered_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_job_offers_portal_extid ON job_offers(portal, external_job_id);
CREATE INDEX IF NOT EXISTS idx_job_offers_fingerprint ON job_offers(fingerprint_hash);

CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_offer_id INTEGER NOT NULL REFERENCES job_offers(id) ON DELETE CASCADE,
    session_id TEXT REFERENCES work_sessions(session_id),
    state TEXT NOT NULL CHECK (state IN (
        'DISCOVERED',
        'EVALUATED',
        'READY_FOR_AI_ANSWERS',
        'READY_TO_APPLY',
        'SIMULATED_READY',
        'APPLYING',
        'SUBMITTED_UNVERIFIED',
        'CONFIRMED',
        'ALREADY_APPLIED_PORTAL',
        'CLOSED_ON_PORTAL',
        'NEEDS_HUMAN_REVIEW',
        'FAILED_VALIDATION',
        'FAILED_RETRYABLE',
        'FAILED_FATAL',
        'SKIPPED_FILTER'
    )),
    selected_cv_base TEXT,
    selected_cv_target TEXT,
    folder_name TEXT,
    folder_path TEXT,
    expected_salary_clp INTEGER NOT NULL DEFAULT 800000,
    cover_letter_text TEXT,
    killer_questions_json TEXT DEFAULT '[]',
    killer_answers_json TEXT DEFAULT '{}',
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    last_step_reached TEXT,
    last_error_code TEXT,
    last_error_message TEXT,
    is_legacy_imported INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    confirmed_at TEXT,
    UNIQUE(job_offer_id)
);

CREATE INDEX IF NOT EXISTS idx_applications_state ON applications(state);
CREATE INDEX IF NOT EXISTS idx_applications_session ON applications(session_id);

CREATE TABLE IF NOT EXISTS application_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    step_reached TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('IN_PROGRESS', 'SUCCESS', 'ERROR', 'UNCERTAIN')),
    error_type TEXT,
    diagnostic_json TEXT,
    current_url_at_end TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS state_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    from_state TEXT,
    to_state TEXT NOT NULL,
    reason TEXT,
    transitioned_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS application_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL CHECK (evidence_type IN (
        'FORM_SCREENSHOT',
        'CONFIRMATION_SCREENSHOT',
        'ERROR_SCREENSHOT',
        'UNCERTAIN_SCREENSHOT',
        'DOM_SNIPPET',
        'DOSSIER_MD',
        'ANSWERS_TXT',
        'CV_PDF',
        'CV_DOCX'
    )),
    file_path TEXT NOT NULL,
    file_sha256 TEXT,
    verified_valid INTEGER NOT NULL DEFAULT 1,
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_cache_entries (
    input_hash TEXT PRIMARY KEY,
    task_type TEXT NOT NULL CHECK (task_type IN ('OFFER_EVAL', 'KILLER_ANSWER')),
    normalized_input TEXT NOT NULL,
    output_json TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'DETERMINISTIC',
    created_at TEXT NOT NULL
);
