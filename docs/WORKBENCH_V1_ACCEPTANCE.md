# Workbench v1 acceptance record

Date: 2026-09-16, Asia/Shanghai. Working branch: `fix/curl-live-verification`, starting HEAD `0af3486`.

## Status

Implemented and exercised the six-stage workbench workflow. Formal freeze remains pending the historical source-freeze test decision; this document does not claim a tag, commit, release or universally supported websites.

## Requirement evidence

| Requirement | Implementation | Evidence |
| --- | --- | --- |
| Public admission | deployed policy selection, cURL policy propagation, startup loader | workbench admission tests; real HTTPS and books.toscrape.com fetch |
| Consistent public transport | checked IP connection + TLS hostname, no auth/cookie replay, robots and byte bounds | pinning/proxy tests; real Chromium through guarded transport |
| Description and typed fields | bounded requirements; explicit unsupported-requirements response; missing optional fields remain null | workbench requirements tests; actual model interpreted Chinese description |
| User model configuration | in-memory HTTPS compatible profiles, selection, explicit probe, removal | profile/API/browser tests; actual DeepSeek task using configured environment |
| Preview and confirmation | real sample, matching plan hash, cancellation, single-worker lock; no second planning step | preview tests and actual browser confirmation |
| HTML/list/detail | declarative CSS arguments, observed same-origin links, bounded traversal | real local HTTP list/detail test; actual public list extraction |
| Formatted delivery | result table, sources, JSON/CSV/XLSX downloads from hash-checked result.json | actual browser downloaded all three; independent openpyxl/CSV readback matched JSON |
| Legacy behavior | original pipeline defaults/collector templates/benchmark files retained | full regression, existing console QA, Trace tests; old byte-freeze exception below |

## Commands and outcomes

- Full backend suite: `.venv/Scripts/python.exe -m pytest tests -q` → **1040 passed, 1 failed, 2 warnings**, 69.14 s. Failure: `tests/test_m55_failure_fixtures.py::test_frozen_compatibility_and_viewer_contract`, current source differs from `v0.5.4-freeze`. This already failed at the initial baseline (976 passed / 1 failed). Assertion retained, not skipped or deleted.
- Targeted requirement + error taxonomy tests after fixing an introduced category mismatch → **37 passed**.
- Public pinning/resolution/runtime tests → **77 passed**.
- Public browser/robots/runtime checks after bounding robots content → **13 passed**.
- Profile creation/selection/probe/removal tests → **4 passed**, 2 existing deprecation warnings.
- Requirement/result-export targeted tests after collector compatibility adjustment → **10 passed**.
- Frontend build: `npm.cmd --prefix frontend run build` → PASS (TypeScript and Vite).
- Existing console mock QA: `.venv/Scripts/python.exe frontend/qa_console.py` → PASS; zero cloud calls. Its fixture now supplies the added policy/model discovery endpoints.
- Existing Trace viewer: `TRACE_BASE_URL=http://127.0.0.1:8002/console/`, pytest `frontend/test_trace_viewer.py -q` → **14 passed**.
- Actual Vue + FastAPI/storage bridge UI test, including configured model selection, policy, requirements and confirmation → PASS. This test uses a fixture worker and is not cloud evidence.

The full-suite count is from the final run after all three review corrections. The complete output is in the local temporary file wda-final-tests.log.

## Actual public + model + browser task

Command: `.venv/Scripts/python.exe scripts/verify_workbench_live.py --live`.

- Task: `91e3d053-c99c-4486-bfd0-1d574c3203e2`.
- Public site: `https://books.toscrape.com/`; allowlisted by the example deployment policy.
- User workflow: UI creation → real observation → DeepSeek initial plan → visible sample → matching confirmation → extraction → table → three browser downloads.
- Model: `deepseek-flash`, using the existing machine-level credential loaded into this server process. Credential value was not printed or placed in files.
- Result: **3 records, 1 page, partial**, record limit reached; zero missing requested fields; 111.31 s.
- Model requests: **2**, including the gateway's existing connection retry; one successful initial plan, no second planning phase. First usage entry is unknown, second is 2808 input / 113 output tokens. Do not report this as one network call.
- Actual generated selectors: `article.product_pod`, `h3 a` title/href, `p.price_color`; no generated executable code.
- JSON/CSV/XLSX downloads independently compared: **identical 3 rows × 3 fields**. Excel readback used bundled openpyxl, without adding an application dependency.
- Page errors: none. Desktop 1440×1000 and mobile 390×844 checked; mobile document width did not exceed viewport. Full-page screenshots inspected.
- Registered task artifacts remain in `data/tasks/91e3d053-c99c-4486-bfd0-1d574c3203e2/`.
- Browser screenshots, downloaded files and verification record: `C:/Users/Administrator/AppData/Local/Temp/wda-live-nuvnrnpn/` (temporary evidence, not tracked repository files).

Earlier live checks: guarded HTTPS `example.com` returned 200; a deterministic manually authored public HTML plan also extracted three book rows. These earlier checks are not model-planning evidence.

## Final review corrections

Independent read-only review reproduced two important edge cases. Both were fixed and reviewed again: optional off-origin detail candidates no longer reject an admissible list page, and HTTP error documents cannot be accepted as successful HTML data. A separate budget regression proved and fixed a 61st document request at the transition to the next list page. The new tests failed before each fix. Targeted HTML/public-browser tests: **8 passed**, including actual Chromium against a controlled HTTP 503 response and real list/detail pages. No additional model calls were needed for these fixes.

The final server was restarted with these changes; the persisted live task and all three records remain readable through the result API. Frontend TypeScript/Vite build passed again. Review found no remaining blocker in the three fixes.

## Compatibility and limitations

- No benchmark/case dataset or collector template changes. No new runtime dependencies, migration, push, commit or tag.
- Old JSON task artifacts retain their shapes; HTML tasks use a declarative DOM plan in plan.json, so consumers of newly introduced HTML tasks must understand that plan type. Legacy tasks are not rewritten.
- Optional fields / record limits / HTML do not export the old standalone collector; reports explicitly mark collector unavailability. Required-only JSON fields retain legacy collector equivalence verification.
- Public demonstration covers one practice website; real detail-page traversal is verified on controlled HTTP pages, not a broad set of public sites.
- Memory-only model profiles require re-entry after restart. Compatibility with arbitrary providers is not claimed.
- Filters, summaries, login, arbitrary interactions, cursor/offset pagination and JD/Taobao remain unsupported; unsupported requirements must fail rather than be silently claimed as applied.
- Historical all-source byte freeze conflicts with the approved new development scope. A user decision is requested before changing that test or declaring formal freeze.
- Pre-existing AGENTS.md, demo/, M7 governance/report work is preserved. The combined worktree includes those changes; do not attribute every changed file to this acceptance task.

## Git handoff

Branch remains `fix/curl-live-verification`, HEAD `0af3486`. Working tree has modified and untracked files; nothing committed or pushed. Final tracked diff: 15 files, 486 insertions, 86 deletions (untracked additions and pre-existing work are not represented by that statistic). `git diff --check` passes; Git reports only checkout line-ending notices. Suggested commit after reviewing the combined worktree: `feat: add bounded natural-language collection workbench`.
