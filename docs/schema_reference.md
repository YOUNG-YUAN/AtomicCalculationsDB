# SQLite Schema Reference

> 本文件由 `scripts/export_schema_reference.py` 自动生成，请勿手工编辑。
> 运行时唯一 schema 源是 `app_core/db/schema_v2.sql`；本文件只是便于检索和代码记忆的派生文档。

- Schema 文件：`app_core/db/schema_v2.sql`
- SHA256：`9a7b3dd164c28a241908267d1b365699746a01d6dfd9904372d08a7771c5078f`
- 用户表：**17**
- 列：**202**
- 索引：**39**（含 SQLite 自动索引）
- 触发器：**3**
- FTS5 虚拟表：`knowledge_fts`

## 关系概览

- `documents` 1 → N `raw_evidence_chunks` / `tasks`
- `knowledge_cards` 1 → N `raw_evidence_chunks` / `card_keywords` / `card_model_provenance` / `card_audit_reviews` / `card_revisions`
- `controlled_keywords` 1 → N `card_keywords`
- `knowledge_fts` 是 `knowledge_cards` 的外部内容 FTS5 索引，由 3 个触发器维护

## 表结构

### `audit_import_batches`

| # | 列名 | 类型 | NOT NULL | 默认值 | 主键 |
|---:|---|---|:---:|---|---|
| 0 | `import_id` | INTEGER | 否 | — | 1 |
| 1 | `batch_id` | TEXT | 是 | `''` | — |
| 2 | `report_id` | TEXT | 是 | `''` | — |
| 3 | `report_hash` | TEXT | 是 | `''` | — |
| 4 | `auditor_name` | TEXT | 是 | `''` | — |
| 5 | `file_name` | TEXT | 是 | `''` | — |
| 6 | `file_hash` | TEXT | 是 | `''` | — |
| 7 | `imported_at` | TEXT | 是 | `datetime('now')` | — |
| 8 | `applied_at` | TEXT | 否 | — | — |
| 9 | `stats_json` | TEXT | 是 | `'{}'` | — |

**索引**

| 索引 | 列 | 唯一 | 来源 | 部分索引 |
|---|---|:---:|---|---|
| `idx_audit_import_report` | `report_id`, `imported_at` | 否 | CREATE INDEX | 否 |
| `sqlite_autoindex_audit_import_batches_1` | `report_hash`, `batch_id`, `auditor_name`, `file_hash` | 是 | UNIQUE 约束 | 否 |

<details><summary>原始 DDL</summary>

```sql
CREATE TABLE audit_import_batches (
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
)
```

</details>

### `card_audit_reviews`

| # | 列名 | 类型 | NOT NULL | 默认值 | 主键 |
|---:|---|---|:---:|---|---|
| 0 | `review_id` | TEXT | 否 | — | 1 |
| 1 | `knw_id` | TEXT | 是 | — | — |
| 2 | `doc_id` | TEXT | 是 | `''` | — |
| 3 | `report_id` | TEXT | 是 | `''` | — |
| 4 | `report_hash` | TEXT | 是 | `''` | — |
| 5 | `batch_id` | TEXT | 是 | `''` | — |
| 6 | `auditor_name` | TEXT | 是 | `''` | — |
| 7 | `auditor_model` | TEXT | 是 | `''` | — |
| 8 | `auditor_provider` | TEXT | 是 | `''` | — |
| 9 | `auditor_node_id` | TEXT | 是 | `''` | — |
| 10 | `audit_source` | TEXT | 是 | `'manual_import'` | — |
| 11 | `overall_score` | INTEGER | 否 | — | — |
| 12 | `verdict` | TEXT | 是 | `''` | — |
| 13 | `confidence` | REAL | 否 | — | — |
| 14 | `needs_manual_review` | INTEGER | 是 | `0` | — |
| 15 | `suggested_action` | TEXT | 是 | `'keep'` | — |
| 16 | `issues_json` | TEXT | 是 | `'[]'` | — |
| 17 | `revised_card_json` | TEXT | 是 | `'{}'` | — |
| 18 | `delete_reason` | TEXT | 是 | `''` | — |
| 19 | `raw_json` | TEXT | 是 | `'{}'` | — |
| 20 | `imported_at` | TEXT | 是 | `datetime('now')` | — |
| 21 | `applied_at` | TEXT | 否 | — | — |
| 22 | `status` | TEXT | 是 | `'IMPORTED'` | — |
| 23 | `validation_warning` | TEXT | 是 | `''` | — |

**外键**

| 列 | 引用表 | 引用列 | ON DELETE | ON UPDATE |
|---|---|---|---|---|
| `knw_id` | `knowledge_cards` | `knw_id` | CASCADE | NO ACTION |

**索引**

| 索引 | 列 | 唯一 | 来源 | 部分索引 |
|---|---|:---:|---|---|
| `idx_audit_report` | `report_id` | 否 | CREATE INDEX | 否 |
| `idx_audit_card` | `knw_id`, `imported_at` | 否 | CREATE INDEX | 否 |
| `sqlite_autoindex_card_audit_reviews_2` | `report_hash`, `knw_id`, `auditor_name` | 是 | UNIQUE 约束 | 否 |
| `sqlite_autoindex_card_audit_reviews_1` | `review_id` | 是 | 主键 | 否 |

<details><summary>原始 DDL</summary>

```sql
CREATE TABLE card_audit_reviews (
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
)
```

</details>

### `card_keywords`

| # | 列名 | 类型 | NOT NULL | 默认值 | 主键 |
|---:|---|---|:---:|---|---|
| 0 | `knw_id` | TEXT | 是 | — | 1 |
| 1 | `kw_id` | TEXT | 是 | — | 2 |
| 2 | `weight` | REAL | 是 | `1.0` | — |

**外键**

| 列 | 引用表 | 引用列 | ON DELETE | ON UPDATE |
|---|---|---|---|---|
| `kw_id` | `controlled_keywords` | `kw_id` | CASCADE | NO ACTION |
| `knw_id` | `knowledge_cards` | `knw_id` | CASCADE | NO ACTION |

**索引**

| 索引 | 列 | 唯一 | 来源 | 部分索引 |
|---|---|:---:|---|---|
| `idx_card_keywords_kw` | `kw_id` | 否 | CREATE INDEX | 否 |
| `sqlite_autoindex_card_keywords_1` | `knw_id`, `kw_id` | 是 | 主键 | 否 |

<details><summary>原始 DDL</summary>

```sql
CREATE TABLE card_keywords (
    knw_id TEXT NOT NULL REFERENCES knowledge_cards(knw_id) ON DELETE CASCADE,
    kw_id  TEXT NOT NULL REFERENCES controlled_keywords(kw_id) ON DELETE CASCADE,
    weight REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (knw_id, kw_id)
)
```

</details>

### `card_model_provenance`

| # | 列名 | 类型 | NOT NULL | 默认值 | 主键 |
|---:|---|---|:---:|---|---|
| 0 | `provenance_id` | INTEGER | 否 | — | 1 |
| 1 | `knw_id` | TEXT | 是 | — | — |
| 2 | `role` | TEXT | 是 | — | — |
| 3 | `node_id` | TEXT | 是 | `''` | — |
| 4 | `node_name` | TEXT | 是 | `''` | — |
| 5 | `node_type` | TEXT | 是 | `''` | — |
| 6 | `provider` | TEXT | 是 | `''` | — |
| 7 | `model` | TEXT | 是 | `''` | — |
| 8 | `web_preset` | TEXT | 是 | `''` | — |
| 9 | `web_model` | TEXT | 是 | `''` | — |
| 10 | `source` | TEXT | 是 | `'extraction'` | — |
| 11 | `recorded_at` | TEXT | 是 | `datetime('now')` | — |

**外键**

| 列 | 引用表 | 引用列 | ON DELETE | ON UPDATE |
|---|---|---|---|---|
| `knw_id` | `knowledge_cards` | `knw_id` | CASCADE | NO ACTION |

**索引**

| 索引 | 列 | 唯一 | 来源 | 部分索引 |
|---|---|:---:|---|---|
| `idx_provenance_card` | `knw_id` | 否 | CREATE INDEX | 否 |
| `sqlite_autoindex_card_model_provenance_1` | `knw_id`, `role` | 是 | UNIQUE 约束 | 否 |

<details><summary>原始 DDL</summary>

```sql
CREATE TABLE card_model_provenance (
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
)
```

</details>

### `card_relations`

| # | 列名 | 类型 | NOT NULL | 默认值 | 主键 |
|---:|---|---|:---:|---|---|
| 0 | `relation_id` | TEXT | 否 | — | 1 |
| 1 | `source_knw_id` | TEXT | 是 | — | — |
| 2 | `target_knw_id` | TEXT | 是 | — | — |
| 3 | `relation_type` | TEXT | 是 | — | — |
| 4 | `confidence` | REAL | 是 | `0` | — |
| 5 | `reason` | TEXT | 是 | `''` | — |
| 6 | `evidence_json` | TEXT | 是 | `'{}'` | — |
| 7 | `created_at` | TEXT | 是 | `datetime('now')` | — |
| 8 | `updated_at` | TEXT | 是 | `datetime('now')` | — |

**外键**

| 列 | 引用表 | 引用列 | ON DELETE | ON UPDATE |
|---|---|---|---|---|
| `target_knw_id` | `knowledge_cards` | `knw_id` | CASCADE | NO ACTION |
| `source_knw_id` | `knowledge_cards` | `knw_id` | CASCADE | NO ACTION |

**索引**

| 索引 | 列 | 唯一 | 来源 | 部分索引 |
|---|---|:---:|---|---|
| `idx_relations_target` | `target_knw_id`, `relation_type` | 否 | CREATE INDEX | 否 |
| `idx_relations_source` | `source_knw_id`, `relation_type` | 否 | CREATE INDEX | 否 |
| `sqlite_autoindex_card_relations_2` | `source_knw_id`, `target_knw_id`, `relation_type` | 是 | UNIQUE 约束 | 否 |
| `sqlite_autoindex_card_relations_1` | `relation_id` | 是 | 主键 | 否 |

<details><summary>原始 DDL</summary>

```sql
CREATE TABLE card_relations (
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
)
```

</details>

### `card_revisions`

| # | 列名 | 类型 | NOT NULL | 默认值 | 主键 |
|---:|---|---|:---:|---|---|
| 0 | `revision_id` | INTEGER | 否 | — | 1 |
| 1 | `knw_id` | TEXT | 是 | — | — |
| 2 | `audit_review_id` | TEXT | 是 | `''` | — |
| 3 | `before_json` | TEXT | 是 | `'{}'` | — |
| 4 | `after_json` | TEXT | 是 | `'{}'` | — |
| 5 | `created_at` | TEXT | 是 | `datetime('now')` | — |
| 6 | `applied_by` | TEXT | 是 | `''` | — |

**外键**

| 列 | 引用表 | 引用列 | ON DELETE | ON UPDATE |
|---|---|---|---|---|
| `knw_id` | `knowledge_cards` | `knw_id` | CASCADE | NO ACTION |

**索引**

| 索引 | 列 | 唯一 | 来源 | 部分索引 |
|---|---|:---:|---|---|
| `idx_revisions_card` | `knw_id`, `created_at` | 否 | CREATE INDEX | 否 |

<details><summary>原始 DDL</summary>

```sql
CREATE TABLE card_revisions (
    revision_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    knw_id         TEXT NOT NULL REFERENCES knowledge_cards(knw_id) ON DELETE CASCADE,
    audit_review_id TEXT NOT NULL DEFAULT '',
    before_json    TEXT NOT NULL DEFAULT '{}',
    after_json     TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    applied_by     TEXT NOT NULL DEFAULT ''
)
```

</details>

### `controlled_keywords`

| # | 列名 | 类型 | NOT NULL | 默认值 | 主键 |
|---:|---|---|:---:|---|---|
| 0 | `kw_id` | TEXT | 否 | — | 1 |
| 1 | `term_en` | TEXT | 是 | — | — |
| 2 | `term_zh` | TEXT | 是 | `''` | — |
| 3 | `synonyms_json` | TEXT | 是 | `'[]'` | — |
| 4 | `category` | TEXT | 是 | `''` | — |
| 5 | `hit_count` | INTEGER | 是 | `1` | — |
| 6 | `created_at` | TEXT | 是 | `datetime('now')` | — |
| 7 | `updated_at` | TEXT | 是 | `datetime('now')` | — |

**索引**

| 索引 | 列 | 唯一 | 来源 | 部分索引 |
|---|---|:---:|---|---|
| `sqlite_autoindex_controlled_keywords_2` | `term_en` | 是 | UNIQUE 约束 | 否 |
| `sqlite_autoindex_controlled_keywords_1` | `kw_id` | 是 | 主键 | 否 |

<details><summary>原始 DDL</summary>

```sql
CREATE TABLE controlled_keywords (
    kw_id         TEXT PRIMARY KEY,
    term_en       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    term_zh       TEXT NOT NULL DEFAULT '',
    synonyms_json TEXT NOT NULL DEFAULT '[]',
    category      TEXT NOT NULL DEFAULT '',
    hit_count     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
)
```

</details>

### `document_audit_exports`

| # | 列名 | 类型 | NOT NULL | 默认值 | 主键 |
|---:|---|---|:---:|---|---|
| 0 | `report_id` | TEXT | 否 | — | 1 |
| 1 | `doc_id` | TEXT | 是 | — | — |
| 2 | `report_hash` | TEXT | 是 | `''` | — |
| 3 | `schema` | TEXT | 是 | `'acdb.document_audit_report/v1'` | — |
| 4 | `card_count` | INTEGER | 是 | `0` | — |
| 5 | `part_count` | INTEGER | 是 | `1` | — |
| 6 | `exported_at` | TEXT | 是 | `datetime('now')` | — |
| 7 | `export_dir` | TEXT | 是 | `''` | — |
| 8 | `manifest_json` | TEXT | 是 | `'{}'` | — |

**索引**

| 索引 | 列 | 唯一 | 来源 | 部分索引 |
|---|---|:---:|---|---|
| `idx_audit_exports_doc` | `doc_id`, `exported_at` | 否 | CREATE INDEX | 否 |
| `sqlite_autoindex_document_audit_exports_1` | `report_id` | 是 | 主键 | 否 |

<details><summary>原始 DDL</summary>

```sql
CREATE TABLE document_audit_exports (
    report_id     TEXT PRIMARY KEY,
    doc_id        TEXT NOT NULL,
    report_hash   TEXT NOT NULL DEFAULT '',
    schema        TEXT NOT NULL DEFAULT 'acdb.document_audit_report/v1',
    card_count    INTEGER NOT NULL DEFAULT 0,
    part_count    INTEGER NOT NULL DEFAULT 1,
    exported_at   TEXT NOT NULL DEFAULT (datetime('now')),
    export_dir    TEXT NOT NULL DEFAULT '',
    manifest_json TEXT NOT NULL DEFAULT '{}'
)
```

</details>

### `documents`

| # | 列名 | 类型 | NOT NULL | 默认值 | 主键 |
|---:|---|---|:---:|---|---|
| 0 | `doc_id` | TEXT | 否 | — | 1 |
| 1 | `doi` | TEXT | 否 | — | — |
| 2 | `arxiv_id` | TEXT | 否 | — | — |
| 3 | `title` | TEXT | 是 | — | — |
| 4 | `authors` | TEXT | 是 | `'[]'` | — |
| 5 | `journal` | TEXT | 是 | `''` | — |
| 6 | `pub_year` | INTEGER | 否 | — | — |
| 7 | `doc_type` | TEXT | 是 | `'PAP'` | — |
| 8 | `gatekeeper_score` | INTEGER | 是 | `0` | — |
| 9 | `authority_weight` | REAL | 是 | `1.0` | — |
| 10 | `download_status` | TEXT | 是 | `'QUEUED'` | — |
| 11 | `parse_status` | TEXT | 是 | `'PENDING'` | — |
| 12 | `source_url` | TEXT | 否 | — | — |
| 13 | `local_pdf_path` | TEXT | 否 | — | — |
| 14 | `local_md_path` | TEXT | 否 | — | — |
| 15 | `pdf_sha256` | TEXT | 否 | — | — |
| 16 | `abstract` | TEXT | 是 | `''` | — |
| 17 | `keywords_json` | TEXT | 是 | `'[]'` | — |
| 18 | `input_tokens` | INTEGER | 是 | `0` | — |
| 19 | `output_tokens` | INTEGER | 是 | `0` | — |
| 20 | `metadata_json` | TEXT | 是 | `'{}'` | — |
| 21 | `error_log` | TEXT | 否 | — | — |
| 22 | `parse_error_code` | TEXT | 是 | `''` | — |
| 23 | `created_at` | TEXT | 是 | `datetime('now')` | — |
| 24 | `updated_at` | TEXT | 是 | `datetime('now')` | — |

**索引**

| 索引 | 列 | 唯一 | 来源 | 部分索引 |
|---|---|:---:|---|---|
| `ux_docs_pdf_sha` | `pdf_sha256` | 是 | CREATE INDEX | 是 |
| `idx_docs_doi` | `doi` | 否 | CREATE INDEX | 否 |
| `idx_docs_year` | `pub_year` | 否 | CREATE INDEX | 否 |
| `idx_docs_status` | `download_status`, `parse_status` | 否 | CREATE INDEX | 否 |
| `sqlite_autoindex_documents_2` | `doi` | 是 | UNIQUE 约束 | 否 |
| `sqlite_autoindex_documents_1` | `doc_id` | 是 | 主键 | 否 |

<details><summary>原始 DDL</summary>

```sql
CREATE TABLE documents (
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
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
)
```

</details>

### `events`

| # | 列名 | 类型 | NOT NULL | 默认值 | 主键 |
|---:|---|---|:---:|---|---|
| 0 | `event_id` | INTEGER | 否 | — | 1 |
| 1 | `ts` | TEXT | 是 | `datetime('now')` | — |
| 2 | `level` | TEXT | 是 | `'INFO'` | — |
| 3 | `category` | TEXT | 是 | `'app'` | — |
| 4 | `doc_id` | TEXT | 否 | — | — |
| 5 | `message` | TEXT | 是 | — | — |
| 6 | `detail_json` | TEXT | 是 | `'{}'` | — |

**索引**

| 索引 | 列 | 唯一 | 来源 | 部分索引 |
|---|---|:---:|---|---|
| `idx_events_ts` | `ts` | 否 | CREATE INDEX | 否 |

<details><summary>原始 DDL</summary>

```sql
CREATE TABLE events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    level       TEXT NOT NULL DEFAULT 'INFO',
    category    TEXT NOT NULL DEFAULT 'app',
    doc_id      TEXT,
    message     TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
)
```

</details>

### `knowledge_cards`

| # | 列名 | 类型 | NOT NULL | 默认值 | 主键 |
|---:|---|---|:---:|---|---|
| 0 | `knw_id` | TEXT | 否 | — | 1 |
| 1 | `card_level` | TEXT | 是 | `'L2'` | — |
| 2 | `card_type` | TEXT | 是 | — | — |
| 3 | `element` | TEXT | 否 | — | — |
| 4 | `z_num` | INTEGER | 否 | — | — |
| 5 | `ion_stage` | TEXT | 否 | — | — |
| 6 | `isoelectronic` | TEXT | 否 | — | — |
| 7 | `shell_type` | TEXT | 否 | — | — |
| 8 | `method` | TEXT | 否 | — | — |
| 9 | `code_name` | TEXT | 否 | — | — |
| 10 | `physical_process` | TEXT | 否 | — | — |
| 11 | `conclusion_flags` | INTEGER | 是 | `0` | — |
| 12 | `title_zh` | TEXT | 是 | — | — |
| 13 | `title_en` | TEXT | 是 | `''` | — |
| 14 | `premises_and_conditions` | TEXT | 是 | `''` | — |
| 15 | `summary_zh` | TEXT | 是 | `''` | — |
| 16 | `structured_details_json` | TEXT | 是 | `'{}'` | — |
| 17 | `formulas_latex` | TEXT | 是 | `''` | — |
| 18 | `code_snippet` | TEXT | 是 | `''` | — |
| 19 | `pitfall_warning` | TEXT | 是 | `''` | — |
| 20 | `search_payload` | TEXT | 是 | — | — |
| 21 | `consensus_count` | INTEGER | 是 | `1` | — |
| 22 | `authority_score` | REAL | 是 | `1.0` | — |
| 23 | `recency_year` | INTEGER | 否 | — | — |
| 24 | `card_year` | INTEGER | 否 | — | — |
| 25 | `has_conflict` | INTEGER | 是 | `0` | — |
| 26 | `conflict_with_knw_ids` | TEXT | 是 | `'[]'` | — |
| 27 | `supporting_doc_ids` | TEXT | 是 | `'[]'` | — |
| 28 | `is_vectorized` | INTEGER | 是 | `0` | — |
| 29 | `is_user_verified` | INTEGER | 是 | `0` | — |
| 30 | `source_section` | TEXT | 是 | `''` | — |
| 31 | `page_num` | INTEGER | 否 | — | — |
| 32 | `content_hash` | TEXT | 是 | `''` | — |
| 33 | `source_kind` | TEXT | 是 | `'EXTRACTION'` | — |
| 34 | `audit_status` | TEXT | 是 | `'NORMAL'` | — |
| 35 | `audit_delete_suggested` | INTEGER | 是 | `0` | — |
| 36 | `audit_hidden` | INTEGER | 是 | `0` | — |
| 37 | `audit_delete_reason` | TEXT | 是 | `''` | — |
| 38 | `audit_delete_by` | TEXT | 是 | `''` | — |
| 39 | `audit_delete_at` | TEXT | 否 | — | — |
| 40 | `audit_review_count` | INTEGER | 是 | `0` | — |
| 41 | `audit_positive_count` | INTEGER | 是 | `0` | — |
| 42 | `audit_negative_count` | INTEGER | 是 | `0` | — |
| 43 | `audit_score_min` | INTEGER | 否 | — | — |
| 44 | `audit_score_max` | INTEGER | 否 | — | — |
| 45 | `audit_divergence` | INTEGER | 是 | `0` | — |
| 46 | `needs_vector_update` | INTEGER | 是 | `0` | — |
| 47 | `master_stale` | INTEGER | 是 | `0` | — |
| 48 | `created_at` | TEXT | 是 | `datetime('now')` | — |
| 49 | `updated_at` | TEXT | 是 | `datetime('now')` | — |

**索引**

| 索引 | 列 | 唯一 | 来源 | 部分索引 |
|---|---|:---:|---|---|
| `idx_knw_updated` | `updated_at` | 否 | CREATE INDEX | 否 |
| `idx_knw_conflict` | `has_conflict` | 否 | CREATE INDEX | 否 |
| `idx_knw_element` | `element`, `z_num` | 否 | CREATE INDEX | 否 |
| `idx_knw_coords` | `card_level`, `card_type`, `isoelectronic`, `z_num`, `method`, `physical_process` | 否 | CREATE INDEX | 否 |
| `sqlite_autoindex_knowledge_cards_1` | `knw_id` | 是 | 主键 | 否 |

<details><summary>原始 DDL</summary>

```sql
CREATE TABLE knowledge_cards (
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
)
```

</details>

### `knowledge_fts`

| # | 列名 | 类型 | NOT NULL | 默认值 | 主键 |
|---:|---|---|:---:|---|---|
| 0 | `knw_id` | — | 否 | — | — |
| 1 | `title_zh` | — | 否 | — | — |
| 2 | `search_payload` | — | 否 | — | — |
| 3 | `formulas_latex` | — | 否 | — | — |
| 4 | `code_snippet` | — | 否 | — | — |

<details><summary>原始 DDL</summary>

```sql
CREATE VIRTUAL TABLE knowledge_fts USING fts5(
    knw_id UNINDEXED,
    title_zh,
    search_payload,
    formulas_latex,
    code_snippet,
    content='knowledge_cards',
    content_rowid='rowid',
    tokenize='porter unicode61'
)
```

</details>

### `notation_map`

| # | 列名 | 类型 | NOT NULL | 默认值 | 主键 |
|---:|---|---|:---:|---|---|
| 0 | `raw_symbol` | TEXT | 否 | — | 1 |
| 1 | `canonical_symbol` | TEXT | 是 | — | — |
| 2 | `category` | TEXT | 是 | `''` | — |

**索引**

| 索引 | 列 | 唯一 | 来源 | 部分索引 |
|---|---|:---:|---|---|
| `sqlite_autoindex_notation_map_1` | `raw_symbol` | 是 | 主键 | 否 |

<details><summary>原始 DDL</summary>

```sql
CREATE TABLE notation_map (
    raw_symbol       TEXT PRIMARY KEY,
    canonical_symbol TEXT NOT NULL,
    category         TEXT NOT NULL DEFAULT ''
)
```

</details>

### `raw_evidence_chunks`

| # | 列名 | 类型 | NOT NULL | 默认值 | 主键 |
|---:|---|---|:---:|---|---|
| 0 | `chunk_id` | TEXT | 否 | — | 1 |
| 1 | `parent_knw_id` | TEXT | 是 | — | — |
| 2 | `doc_id` | TEXT | 是 | — | — |
| 3 | `section_title` | TEXT | 是 | `''` | — |
| 4 | `page_num` | INTEGER | 否 | — | — |
| 5 | `context_bundle_md` | TEXT | 是 | — | — |
| 6 | `bundle_hash` | TEXT | 是 | `''` | — |
| 7 | `locator_json` | TEXT | 是 | `'{}'` | — |
| 8 | `created_at` | TEXT | 是 | `datetime('now')` | — |

**外键**

| 列 | 引用表 | 引用列 | ON DELETE | ON UPDATE |
|---|---|---|---|---|
| `doc_id` | `documents` | `doc_id` | CASCADE | NO ACTION |
| `parent_knw_id` | `knowledge_cards` | `knw_id` | CASCADE | NO ACTION |

**索引**

| 索引 | 列 | 唯一 | 来源 | 部分索引 |
|---|---|:---:|---|---|
| `idx_evidence_doc` | `doc_id` | 否 | CREATE INDEX | 否 |
| `idx_evidence_parent` | `parent_knw_id` | 否 | CREATE INDEX | 否 |
| `sqlite_autoindex_raw_evidence_chunks_1` | `chunk_id` | 是 | 主键 | 否 |

<details><summary>原始 DDL</summary>

```sql
CREATE TABLE raw_evidence_chunks (
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
)
```

</details>

### `sync_state`

| # | 列名 | 类型 | NOT NULL | 默认值 | 主键 |
|---:|---|---|:---:|---|---|
| 0 | `path` | TEXT | 否 | — | 1 |
| 1 | `sha256` | TEXT | 是 | — | — |
| 2 | `size` | INTEGER | 是 | `0` | — |
| 3 | `mtime` | REAL | 是 | `0` | — |
| 4 | `synced_at` | TEXT | 否 | — | — |
| 5 | `remote` | TEXT | 是 | `''` | — |

**索引**

| 索引 | 列 | 唯一 | 来源 | 部分索引 |
|---|---|:---:|---|---|
| `sqlite_autoindex_sync_state_1` | `path` | 是 | 主键 | 否 |

<details><summary>原始 DDL</summary>

```sql
CREATE TABLE sync_state (
    path      TEXT PRIMARY KEY,
    sha256    TEXT NOT NULL,
    size      INTEGER NOT NULL DEFAULT 0,
    mtime     REAL NOT NULL DEFAULT 0,
    synced_at TEXT,
    remote    TEXT NOT NULL DEFAULT ''
)
```

</details>

### `system_meta`

| # | 列名 | 类型 | NOT NULL | 默认值 | 主键 |
|---:|---|---|:---:|---|---|
| 0 | `key` | TEXT | 否 | — | 1 |
| 1 | `value` | TEXT | 是 | — | — |
| 2 | `updated_at` | TEXT | 是 | `datetime('now')` | — |

**索引**

| 索引 | 列 | 唯一 | 来源 | 部分索引 |
|---|---|:---:|---|---|
| `sqlite_autoindex_system_meta_1` | `key` | 是 | 主键 | 否 |

<details><summary>原始 DDL</summary>

```sql
CREATE TABLE system_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
)
```

</details>

### `tasks`

| # | 列名 | 类型 | NOT NULL | 默认值 | 主键 |
|---:|---|---|:---:|---|---|
| 0 | `task_id` | INTEGER | 否 | — | 1 |
| 1 | `task_type` | TEXT | 是 | — | — |
| 2 | `doc_id` | TEXT | 否 | — | — |
| 3 | `payload_json` | TEXT | 是 | `'{}'` | — |
| 4 | `status` | TEXT | 是 | `'PENDING'` | — |
| 5 | `priority` | INTEGER | 是 | `0` | — |
| 6 | `attempts` | INTEGER | 是 | `0` | — |
| 7 | `last_error` | TEXT | 否 | — | — |
| 8 | `scheduled_for` | TEXT | 否 | — | — |
| 9 | `started_at` | TEXT | 否 | — | — |
| 10 | `finished_at` | TEXT | 否 | — | — |
| 11 | `created_at` | TEXT | 是 | `datetime('now')` | — |

**外键**

| 列 | 引用表 | 引用列 | ON DELETE | ON UPDATE |
|---|---|---|---|---|
| `doc_id` | `documents` | `doc_id` | CASCADE | NO ACTION |

**索引**

| 索引 | 列 | 唯一 | 来源 | 部分索引 |
|---|---|:---:|---|---|
| `idx_tasks_doc` | `doc_id` | 否 | CREATE INDEX | 否 |
| `idx_tasks_status` | `status`, `priority`, `task_id` | 否 | CREATE INDEX | 否 |

<details><summary>原始 DDL</summary>

```sql
CREATE TABLE tasks (
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
)
```

</details>

## 触发器

### `knowledge_cards_ad`

- 表：`knowledge_cards`

```sql
CREATE TRIGGER knowledge_cards_ad
AFTER DELETE ON knowledge_cards BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, knw_id, title_zh,
                              search_payload, formulas_latex, code_snippet)
    VALUES ('delete', old.rowid, old.knw_id, old.title_zh, old.search_payload,
            old.formulas_latex, old.code_snippet);
END
```

### `knowledge_cards_ai`

- 表：`knowledge_cards`

```sql
CREATE TRIGGER knowledge_cards_ai
AFTER INSERT ON knowledge_cards BEGIN
    INSERT INTO knowledge_fts(rowid, knw_id, title_zh, search_payload,
                              formulas_latex, code_snippet)
    VALUES (new.rowid, new.knw_id, new.title_zh, new.search_payload,
            new.formulas_latex, new.code_snippet);
END
```

### `knowledge_cards_au`

- 表：`knowledge_cards`

```sql
CREATE TRIGGER knowledge_cards_au
AFTER UPDATE ON knowledge_cards BEGIN
    INSERT INTO knowledge_fts(knowledge_fts, rowid, knw_id, title_zh,
                              search_payload, formulas_latex, code_snippet)
    VALUES ('delete', old.rowid, old.knw_id, old.title_zh, old.search_payload,
            old.formulas_latex, old.code_snippet);
    INSERT INTO knowledge_fts(rowid, knw_id, title_zh, search_payload,
                              formulas_latex, code_snippet)
    VALUES (new.rowid, new.knw_id, new.title_zh, new.search_payload,
            new.formulas_latex, new.code_snippet);
END
```

## system_meta 初始值

| key | value |
|---|---|
| `embedding_signature` | `BAAI/bge-m3::dim=1024` |
| `schema_version` | `3.2` |

`created_at` 在数据库创建时由运行时写入，不列入本表。

## FTS5 影子表

以下表由 SQLite FTS5 自动维护，不应直接读写：

```text
knowledge_fts_config
knowledge_fts_data
knowledge_fts_docsize
knowledge_fts_idx
```
