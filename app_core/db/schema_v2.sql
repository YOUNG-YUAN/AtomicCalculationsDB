-- ===========================================================================
-- AtomicCalculationDB V2.0 core schema
-- SQLite 3 (WAL) + FTS5 (porter unicode61)
-- ===========================================================================

-- 1. system metadata / embedding space lock --------------------------------
CREATE TABLE IF NOT EXISTS system_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 2. documents ledger --------------------------------------------------------
CREATE TABLE IF NOT EXISTS documents (
    doc_id            TEXT PRIMARY KEY,
    doi               TEXT UNIQUE,
    arxiv_id          TEXT,
    title             TEXT NOT NULL,
    authors           TEXT NOT NULL DEFAULT '[]',      -- JSON array
    journal           TEXT NOT NULL DEFAULT '',
    pub_year          INTEGER,
    doc_type          TEXT NOT NULL DEFAULT 'PAP'
                      CHECK (doc_type IN ('PAP','BOK','REV','MAN')),
    gatekeeper_score  INTEGER NOT NULL DEFAULT 0,
    authority_weight  REAL NOT NULL DEFAULT 1.0,
    download_status   TEXT NOT NULL DEFAULT 'QUEUED'
                      CHECK (download_status IN ('DISCOVERED','QUEUED','DOWNLOADING',
                                                 'DOWNLOADED','MANUAL_REQUIRED',
                                                 'REJECTED','FAILED')),
    parse_status      TEXT NOT NULL DEFAULT 'PENDING'
                      CHECK (parse_status IN ('PENDING','LIGHT_PARSED',
                                              'LLM_EXTRACTED','FAILED')),
    source_url        TEXT,
    local_pdf_path    TEXT,
    local_md_path     TEXT,
    pdf_sha256        TEXT,
    abstract          TEXT NOT NULL DEFAULT '',
    keywords_json     TEXT NOT NULL DEFAULT '[]',      -- JSON array
    input_tokens      INTEGER NOT NULL DEFAULT 0,
    output_tokens     INTEGER NOT NULL DEFAULT 0,
    metadata_json     TEXT NOT NULL DEFAULT '{}',
    error_log         TEXT,
    parse_error_code  TEXT NOT NULL DEFAULT '',
    card_count        INTEGER NOT NULL DEFAULT 0,
    has_master        INTEGER NOT NULL DEFAULT 0,
    pipeline_step     INTEGER NOT NULL DEFAULT 1,
    pipeline_glow     TEXT NOT NULL DEFAULT 'NONE',
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_docs_status ON documents(download_status, parse_status);
CREATE INDEX IF NOT EXISTS idx_docs_year ON documents(pub_year);
CREATE INDEX IF NOT EXISTS idx_docs_doi ON documents(doi);
CREATE UNIQUE INDEX IF NOT EXISTS ux_docs_pdf_sha
    ON documents(pdf_sha256) WHERE pdf_sha256 IS NOT NULL AND pdf_sha256 <> '';

-- 3. L1 / L2 knowledge cards ------------------------------------------------
CREATE TABLE IF NOT EXISTS knowledge_cards (
    knw_id                  TEXT PRIMARY KEY,
    card_level              TEXT NOT NULL DEFAULT 'L2'
                            CHECK (card_level IN ('L1','L2')),
    card_type               TEXT NOT NULL
                            CHECK (card_type IN ('STRAT','MODEL','CODE',
                                                 'PITFALL','CONC','MASTER')),

    -- four-dimensional physics coordinates (metadata pre-filtering)
    element                 TEXT,
    z_num                   INTEGER,
    ion_stage               TEXT,
    isoelectronic           TEXT,
    shell_type              TEXT,
    method                  TEXT,
    code_name               TEXT,
    physical_process        TEXT,

    -- six-dimensional conclusion type bitmask
    conclusion_flags        INTEGER NOT NULL DEFAULT 0,

    title_zh                TEXT NOT NULL,
    title_en                TEXT NOT NULL DEFAULT '',
    premises_and_conditions TEXT NOT NULL DEFAULT '',
    summary_zh              TEXT NOT NULL DEFAULT '',
    structured_details_json TEXT NOT NULL DEFAULT '{}',
    formulas_latex          TEXT NOT NULL DEFAULT '',
    code_snippet            TEXT NOT NULL DEFAULT '',
    pitfall_warning         TEXT NOT NULL DEFAULT '',

    search_payload          TEXT NOT NULL,
    consensus_count         INTEGER NOT NULL DEFAULT 1,
    authority_score         REAL NOT NULL DEFAULT 1.0,
    recency_year            INTEGER,
    card_year               INTEGER,
    has_conflict            INTEGER NOT NULL DEFAULT 0,
    conflict_with_knw_ids   TEXT NOT NULL DEFAULT '[]',
    supporting_doc_ids      TEXT NOT NULL DEFAULT '[]',
    is_vectorized           INTEGER NOT NULL DEFAULT 0,
    is_user_verified        INTEGER NOT NULL DEFAULT 0,
    source_section          TEXT NOT NULL DEFAULT '',
    page_num                INTEGER,

    -- V2.4.0 cross-AI audit state -------------------------------------------
    content_hash             TEXT NOT NULL DEFAULT '',
    source_kind              TEXT NOT NULL DEFAULT 'EXTRACTION',
    audit_status             TEXT NOT NULL DEFAULT 'NORMAL',
    audit_delete_suggested   INTEGER NOT NULL DEFAULT 0,
    audit_hidden             INTEGER NOT NULL DEFAULT 0,
    audit_delete_reason      TEXT NOT NULL DEFAULT '',
    audit_delete_by          TEXT NOT NULL DEFAULT '',
    audit_delete_at          TEXT,
    audit_review_count       INTEGER NOT NULL DEFAULT 0,
    audit_positive_count     INTEGER NOT NULL DEFAULT 0,
    audit_negative_count     INTEGER NOT NULL DEFAULT 0,
    audit_score_min          INTEGER,
    audit_score_max          INTEGER,
    audit_divergence         INTEGER NOT NULL DEFAULT 0,
    needs_vector_update      INTEGER NOT NULL DEFAULT 0,
    master_stale             INTEGER NOT NULL DEFAULT 0,

    created_at              TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at              TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_knw_coords
    ON knowledge_cards(card_level, card_type, isoelectronic, z_num,
                       method, physical_process);
CREATE INDEX IF NOT EXISTS idx_knw_element ON knowledge_cards(element, z_num);
CREATE INDEX IF NOT EXISTS idx_knw_conflict ON knowledge_cards(has_conflict);
CREATE INDEX IF NOT EXISTS idx_knw_updated ON knowledge_cards(updated_at DESC);
-- Audit indexes are created after _ensure_columns() so legacy databases
-- gain the new columns before SQLite evaluates the index expressions.

-- 4. FTS5 external-content index + triggers ---------------------------------
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    knw_id UNINDEXED,
    title_zh,
    search_payload,
    formulas_latex,
    code_snippet,
    content='knowledge_cards',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS knowledge_cards_ai
AFTER INSERT ON knowledge_cards BEGIN
    INSERT INTO knowledge_fts(rowid, knw_id, title_zh, search_payload,
                              formulas_latex, code_snippet)
    VALUES (new.rowid, new.knw_id, new.title_zh, new.search_payload,
            new.formulas_latex, new.code_snippet);
END;

CREATE TRIGGER IF NOT EXISTS knowledge_cards_ad
AFTER DELETE ON knowledge_cards BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, knw_id, title_zh,
                              search_payload, formulas_latex, code_snippet)
    VALUES ('delete', old.rowid, old.knw_id, old.title_zh, old.search_payload,
            old.formulas_latex, old.code_snippet);
END;

CREATE TRIGGER IF NOT EXISTS knowledge_cards_au
AFTER UPDATE ON knowledge_cards BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, knw_id, title_zh,
                              search_payload, formulas_latex, code_snippet)
    VALUES ('delete', old.rowid, old.knw_id, old.title_zh, old.search_payload,
            old.formulas_latex, old.code_snippet);
    INSERT INTO knowledge_fts(rowid, knw_id, title_zh, search_payload,
                              formulas_latex, code_snippet)
    VALUES (new.rowid, new.knw_id, new.title_zh, new.search_payload,
            new.formulas_latex, new.code_snippet);
END;

-- 5. L3 raw evidence chunks --------------------------------------------------
CREATE TABLE IF NOT EXISTS raw_evidence_chunks (
    chunk_id          TEXT PRIMARY KEY,
    parent_knw_id     TEXT NOT NULL,
    doc_id            TEXT NOT NULL,
    section_title     TEXT NOT NULL DEFAULT '',
    page_num          INTEGER,
    context_bundle_md TEXT NOT NULL,
    bundle_hash       TEXT NOT NULL DEFAULT '',
    locator_json      TEXT NOT NULL DEFAULT '{}',
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(parent_knw_id) REFERENCES knowledge_cards(knw_id) ON DELETE CASCADE,
    FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_evidence_parent ON raw_evidence_chunks(parent_knw_id);
CREATE INDEX IF NOT EXISTS idx_evidence_doc ON raw_evidence_chunks(doc_id);
-- bundle_hash index is created after _ensure_columns() so legacy databases
-- are migrated before SQLite evaluates the column reference.

-- 6. self-bootstrapping controlled vocabulary -------------------------------
CREATE TABLE IF NOT EXISTS controlled_keywords (
    kw_id         TEXT PRIMARY KEY,
    term_en       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    term_zh       TEXT NOT NULL DEFAULT '',
    synonyms_json TEXT NOT NULL DEFAULT '[]',
    category      TEXT NOT NULL DEFAULT '',
    hit_count     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS card_keywords (
    knw_id TEXT NOT NULL REFERENCES knowledge_cards(knw_id) ON DELETE CASCADE,
    kw_id  TEXT NOT NULL REFERENCES controlled_keywords(kw_id) ON DELETE CASCADE,
    weight REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (knw_id, kw_id)
);

CREATE INDEX IF NOT EXISTS idx_card_keywords_kw ON card_keywords(kw_id);

-- 7. notation normalization --------------------------------------------------
CREATE TABLE IF NOT EXISTS notation_map (
    raw_symbol       TEXT PRIMARY KEY,
    canonical_symbol TEXT NOT NULL,
    category         TEXT NOT NULL DEFAULT ''
);

-- 8. background task ledger --------------------------------------------------
CREATE TABLE IF NOT EXISTS tasks (
    task_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type     TEXT NOT NULL,
    doc_id        TEXT,
    payload_json  TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'PENDING'
                  CHECK (status IN ('PENDING','RUNNING','DONE','FAILED','SKIPPED')),
    priority      INTEGER NOT NULL DEFAULT 0,
    attempts      INTEGER NOT NULL DEFAULT 0,
    last_error    TEXT,
    scheduled_for TEXT,
    started_at    TEXT,
    finished_at   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tasks_status
    ON tasks(status, priority DESC, task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_doc ON tasks(doc_id);

-- 9. event ledger ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    level       TEXT NOT NULL DEFAULT 'INFO',
    category    TEXT NOT NULL DEFAULT 'app',
    doc_id      TEXT,
    message     TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts DESC);

-- 10. sync state -------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_state (
    path      TEXT PRIMARY KEY,
    sha256    TEXT NOT NULL,
    size      INTEGER NOT NULL DEFAULT 0,
    mtime     REAL NOT NULL DEFAULT 0,
    synced_at TEXT,
    remote    TEXT NOT NULL DEFAULT ''
);

-- 11. model provenance per card ---------------------------------------------
CREATE TABLE IF NOT EXISTS card_model_provenance (
    provenance_id INTEGER PRIMARY KEY AUTOINCREMENT,
    knw_id        TEXT NOT NULL REFERENCES knowledge_cards(knw_id) ON DELETE CASCADE,
    role          TEXT NOT NULL,
    node_id       TEXT NOT NULL DEFAULT '',
    node_name     TEXT NOT NULL DEFAULT '',
    node_type     TEXT NOT NULL DEFAULT '',
    provider      TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT '',
    web_preset    TEXT NOT NULL DEFAULT '',
    web_model     TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT 'extraction',
    recorded_at   TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(knw_id, role)
);

CREATE INDEX IF NOT EXISTS idx_provenance_card
    ON card_model_provenance(knw_id);

-- 12. cross-AI audit reviews -------------------------------------------------
CREATE TABLE IF NOT EXISTS card_audit_reviews (
    review_id          TEXT PRIMARY KEY,
    knw_id             TEXT NOT NULL REFERENCES knowledge_cards(knw_id) ON DELETE CASCADE,
    doc_id             TEXT NOT NULL DEFAULT '',
    report_id          TEXT NOT NULL DEFAULT '',
    report_hash        TEXT NOT NULL DEFAULT '',
    batch_id           TEXT NOT NULL DEFAULT '',
    auditor_name       TEXT NOT NULL DEFAULT '',
    auditor_model      TEXT NOT NULL DEFAULT '',
    auditor_provider   TEXT NOT NULL DEFAULT '',
    auditor_node_id    TEXT NOT NULL DEFAULT '',
    audit_source       TEXT NOT NULL DEFAULT 'manual_import',
    overall_score      INTEGER,
    verdict            TEXT NOT NULL DEFAULT '',
    confidence         REAL,
    needs_manual_review INTEGER NOT NULL DEFAULT 0,
    suggested_action   TEXT NOT NULL DEFAULT 'keep',
    issues_json        TEXT NOT NULL DEFAULT '[]',
    revised_card_json  TEXT NOT NULL DEFAULT '{}',
    delete_reason      TEXT NOT NULL DEFAULT '',
    raw_json           TEXT NOT NULL DEFAULT '{}',
    imported_at        TEXT NOT NULL DEFAULT (datetime('now')),
    applied_at         TEXT,
    status             TEXT NOT NULL DEFAULT 'IMPORTED',
    validation_warning TEXT NOT NULL DEFAULT '',
    UNIQUE(report_hash, knw_id, auditor_name)
);

CREATE INDEX IF NOT EXISTS idx_audit_card
    ON card_audit_reviews(knw_id, imported_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_report
    ON card_audit_reviews(report_id);

-- 13. card revisions ---------------------------------------------------------
CREATE TABLE IF NOT EXISTS card_revisions (
    revision_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    knw_id         TEXT NOT NULL REFERENCES knowledge_cards(knw_id) ON DELETE CASCADE,
    audit_review_id TEXT NOT NULL DEFAULT '',
    before_json    TEXT NOT NULL DEFAULT '{}',
    after_json     TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    applied_by     TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_revisions_card
    ON card_revisions(knw_id, created_at DESC);

-- 14. exported audit bundles -------------------------------------------------
CREATE TABLE IF NOT EXISTS document_audit_exports (
    report_id     TEXT PRIMARY KEY,
    doc_id        TEXT NOT NULL,
    report_hash   TEXT NOT NULL DEFAULT '',
    schema        TEXT NOT NULL DEFAULT 'acdb.document_audit_report/v1',
    card_count    INTEGER NOT NULL DEFAULT 0,
    part_count    INTEGER NOT NULL DEFAULT 1,
    exported_at   TEXT NOT NULL DEFAULT (datetime('now')),
    export_dir    TEXT NOT NULL DEFAULT '',
    manifest_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_audit_exports_doc
    ON document_audit_exports(doc_id, exported_at DESC);

-- 15. audit import batches ---------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_import_batches (
    import_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL DEFAULT '',
    report_id     TEXT NOT NULL DEFAULT '',
    report_hash   TEXT NOT NULL DEFAULT '',
    auditor_name  TEXT NOT NULL DEFAULT '',
    file_name     TEXT NOT NULL DEFAULT '',
    file_hash     TEXT NOT NULL DEFAULT '',
    imported_at   TEXT NOT NULL DEFAULT (datetime('now')),
    applied_at    TEXT,
    stats_json    TEXT NOT NULL DEFAULT '{}',
    UNIQUE(report_hash, batch_id, auditor_name, file_hash)
);

CREATE INDEX IF NOT EXISTS idx_audit_import_report
    ON audit_import_batches(report_id, imported_at DESC);

-- 16. non-destructive card relations ----------------------------------------
CREATE TABLE IF NOT EXISTS card_relations (
    relation_id   TEXT PRIMARY KEY,
    source_knw_id TEXT NOT NULL REFERENCES knowledge_cards(knw_id) ON DELETE CASCADE,
    target_knw_id TEXT NOT NULL REFERENCES knowledge_cards(knw_id) ON DELETE CASCADE,
    relation_type TEXT NOT NULL
                  CHECK (relation_type IN ('SAME_CLAIM','SUPPORTS','CONTRADICTS','RELATED')),
    confidence    REAL NOT NULL DEFAULT 0,
    reason        TEXT NOT NULL DEFAULT '',
    evidence_json TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(source_knw_id, target_knw_id, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_relations_source
    ON card_relations(source_knw_id, relation_type);
CREATE INDEX IF NOT EXISTS idx_relations_target
    ON card_relations(target_knw_id, relation_type);

-- 17. document collections / hierarchical folders (Zotero-style) -----------
CREATE TABLE IF NOT EXISTS collections (
    collection_id TEXT PRIMARY KEY,
    parent_id     TEXT REFERENCES collections(collection_id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    icon          TEXT NOT NULL DEFAULT 'folder',
    sort_order    INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS collection_documents (
    collection_id TEXT NOT NULL REFERENCES collections(collection_id) ON DELETE CASCADE,
    doc_id        TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    added_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (collection_id, doc_id)
);

CREATE INDEX IF NOT EXISTS idx_coll_docs_doc ON collection_documents(doc_id);
CREATE INDEX IF NOT EXISTS idx_coll_parent ON collections(parent_id);

INSERT OR IGNORE INTO system_meta(key, value)
VALUES ('schema_version', '3.2');
INSERT OR IGNORE INTO system_meta(key, value)
VALUES ('embedding_signature', 'BAAI/bge-m3::dim=1024');
INSERT OR IGNORE INTO system_meta(key, value)
VALUES ('created_at', datetime('now'));
