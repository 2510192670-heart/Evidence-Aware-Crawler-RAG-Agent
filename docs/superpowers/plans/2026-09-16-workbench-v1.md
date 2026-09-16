# Natural-language collection workbench implementation plan

> Execute sequentially with test-driven development and review after each stage.

**Goal:** Deliver the user-approved URL + description + fields + selected model → sample preview → confirmed collection → structured download workflow.

**Architecture:** Extend the existing task service and validated pipeline. Preserve legacy requests and artifacts. Model output remains declarative data, never executable code. New HTML extraction must use the same target enforcement and bounded execution principles.

**Tech Stack:** Existing Python/FastAPI/httpx/Playwright/SQLite and Vue/TypeScript.

**Spec:** Product agreement in this task on 2026-09-16: public unauthenticated sites first; JD/Taobao later. Single local worker, bounded requests, truthful missing-data and completeness reporting.

## Constraints

- User authorized sequential implementation of all six stages. Historical M5.4 documentation-only scope does not describe this new work.
- Do not replace BM25, change frozen benchmark data, expand repair policy, execute model-generated code, or forward authentication.
- Preserve pre-existing AGENTS.md changes, demo/, and the untracked M7 report.
- Do not commit, tag or push automatically. Existing collector templates require separate review before any changes.
- Keep existing timeout/model-call budgets; preview confirmation must reuse a validated plan rather than add a model call.
- Run targeted tests, full regression, then actual controlled verification. External model/public-network checks must be distinguished from fixture evidence.

## Stage 1 — Public admission and baseline

Files: backend/app/main.py, tasks/service.py, frontend/src/App.vue; new tests/test_workbench_admission.py.

- [x] Capture full regression baseline and classify pre-existing failures.
- [x] Test GET /api/v1/policies returns only deployed policy references and public metadata.
- [x] Test public cURL preview and task submission with a registered policy, preserving query/body and rejecting unknown policies, sensitive metadata and cross-origin imports before persistence.
- [x] Pass resolved policy to parse_curl; move dependent imported URL validation to the service boundary; retain old loopback validation.
- [x] Load optional WDA_TARGET_POLICY_FILE at startup with the existing strict loader; fail closed on invalid configuration.
- [x] Add policy selection to the console and invalidate parsed imports when selection changes.
- [x] Run admission/import tests and frontend build, then inspect real public transport/browser paths before claiming public readiness.

## Stage 2 — Requirements and field contracts

Files: new pipeline/requirements.py and tests/test_workbench_requirements.py; extend TaskInput, pipeline_worker and run_task.

- [x] Define bounded description and field specifications (name, description, type, required) with legacy defaults.
- [x] Test propagation to planner and deterministic result checks, including missing optional values and forbidden invented values.
- [x] Include requirements in the existing initial planning call; reject unsupported requirements explicitly.
- [x] Verify old field-only tasks retain their behavior.

## Stage 3 — Model profiles

Files: new llm/profiles.py and tests/test_model_profiles.py; main.py/service.py and Vue configuration UI.

- [x] Define profile references with HTTPS compatible API settings; secrets must not appear in task specs, logs or artifacts.
- [x] Add profile management and selection with bounded connection checks; preserve environment default.
- [x] Verify configured profile selection survives task execution without leaking credentials.

## Stage 4 — Preview and confirmation

Files: task service/repository, pipeline orchestration, dedicated preview tests and Vue task UI.

- [x] Add an additive preview workflow; persist only validated declarative plan/evidence required for continuation.
- [x] Test explicit confirmation, stale/changed evidence, cancellation, restart and single-worker behavior.
- [x] Continue with the same validated plan and budgets, no second planning call.

## Stage 5 — Page and detail extraction

Files: bounded HTML observation/extraction modules integrated with existing orchestration; real loopback fixture tests.

- [x] Define evidence-derived selectors and same-origin observed links, with no arbitrary script execution.
- [x] Add HTML/rendered-page observation and validated list/detail extraction with bounded pages/records/requests.
- [x] Keep JSON collection behavior unchanged and apply target restrictions consistently across paths.
- [x] Validate actual list/detail data, schema, uniqueness, missing fields and completeness on controlled pages.

## Stage 6 — Delivery and acceptance

Files: result export module/API, Vue result table, documentation and verification report.

- [x] Add CSV/XLSX downloads without changing result.json; protect spreadsheet text from formula interpretation.
- [x] Add source metadata and missing/completeness summaries as additive evidence.
- [x] Run backend regression, frontend build and browser workflow verification.
- [x] Record live public and model verification separately from controlled fixtures; report unverified boundaries honestly.

## Baseline

Branch fix/curl-live-verification, HEAD 0af3486. Existing modifications: AGENTS.md; untracked demo/ and docs/M7_POLICY_RUNTIME_FREEZE_REPORT.md. No files from these existing changes are staged or overwritten.

## Acceptance checkpoint

Implementation/check execution is tracked above; formal freeze is not complete. See docs/WORKBENCH_V1_ACCEPTANCE.md for exact evidence and the retained historical byte-freeze failure. Live public/model/UI task 91e3d053-c99c-4486-bfd0-1d574c3203e2 succeeded with 3 records, 2 model requests including a connection retry, and matching JSON/CSV/XLSX downloads. No commit/tag/push.
