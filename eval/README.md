# AtomicCalculationDB — extraction quality evaluation set

This directory is the starting point for a small, human-checked evaluation
set. It is deliberately not a unit-test fixture: the goal is to measure
whether the extraction pipeline preserves **useful physics knowledge**, not
just whether it returns valid JSON.

## Scope

Start with **10–20 documents**, then expand only after the first pass is
stable. Cover these categories (one document may cover several):

| Category | What it stresses |
|---|---|
| `complex_table` | merged cells, multi-level headers, units, table notes |
| `formula_dense` | inline/display math, aligned equations, equation references |
| `scanned` | OCR quality, page order, missing text layer |
| `cross_page_definition` | definitions or conditions split across pages |
| `figure_dependent` | convergence curves, error trends, figure captions |
| `supplement` | supplementary material, appendices, long tables |
| `manual` | program manuals, input decks, worked examples |
| `preprint_version` | preprint/journal version pairs for independence checks |

## Case file

Copy `cases.example.jsonl` to `cases.jsonl` and fill one JSON object per
document. Keep `doc_id` stable; use `source_path` only for local reference.

```json
{"case_id":"EVAL-001","doc_id":"DOC-PAP-...","categories":["complex_table","formula_dense"],"source_path":"...","status":"TODO","notes":"","expected_cards":[]}
```

`status`: `TODO` → `LABELED` → `VERIFIED`.

## What to label

For each document, record the knowledge that a domain expert would expect to
retain. Do not label every sentence. At minimum:

- core strategy / model / code / pitfall claims;
- the conditions that make each claim valid;
- numeric value + unit + uncertainty (when reported);
- table/figure/equation references needed to interpret the claim;
- whether the claim is author-reported, AI-derived, or locally reproduced.

## Metrics

| Metric | Question |
|---|---|
| `important_recall` | How many important points were missed? |
| `premise_completeness` | Does each card carry its applicable conditions? |
| `evidence_support` | Is the claim actually supported by the cited excerpt? |
| `numeric_unit_accuracy` | Are values, units, exponents and table headers correct? |
| `duplicate_merge_error` | Were distinct claims merged, or duplicates kept? |
| `conflict_fp_fnr` | False conflict reports / missed conflicts |
| `strategy_validation` | Can the generated strategy run and reach the target? |
| `cost_per_useful_card` | External LLM tokens + local CPU/GPU time + retries |

## Cost accounting

Do not write “0 token” without qualification. Report:

- external LLM input/output tokens;
- API calls and retries;
- local wall-clock time;
- CPU/GPU time for MinerU/ONNX;
- human review time when available.

## First-pass procedure

1. Pick 10–20 documents across the categories above.
2. Export cards and L3 evidence for each document.
3. A domain expert marks important claims, conditions, numbers and units.
4. Compare with the generated cards; record misses and false claims.
5. Calibrate duplicate/conflict thresholds only after this baseline exists.
6. Re-run the same set after prompt, parser, model or threshold changes.

## Current guardrails to test explicitly

- A scan without text layer must become `NEEDS_OCR`, not `LLM_EXTRACTED`.
- A duplicate claim must not destroy either card; it should create a
  `SAME_CLAIM` relation and retain both L3 evidence bundles.
- A failed Qdrant update must leave `needs_vector_update=1` and produce a
  retryable `VECTORIZE` task.
- A conflict must be labelled “suspected” until conditions, target quantity,
  units and approximations are checked.
