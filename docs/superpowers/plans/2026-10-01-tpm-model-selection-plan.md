# PLAN: TPM 限流模型选择 + 设置页

**spec:** `docs/superpowers/specs/2026-10-01-tpm-model-selection-design.md`
**worktree:** `.worktrees/tpm-model-selection` (base bd2554f)
**phase:** B0 skeleton — card outline only (no production/test code; those come in B1..Bn)

---

## Global Constraints

1. **Language / runtime.** Single-file Python 3 proxy. No new dependencies.
2. **Verification.** `python3 ctyun-stream-fix-proxy.test.py`; baseline 219 test functions all green (verified by orchestrator at bd2554f).
3. **Tier assignment.** All cards tier A (no runtime-only validation gaps; every card produces code that can be fully tested with temp files + in-process module import + socket-level integration). Cards must be executed in TDD order: failing test first, then implementation.
4. **Test determinism.** Unit tests reuse `load_proxy_module()` (in-process import, no socket). Integration tests reuse `make_scripted_upstream`, `start_proxy(seed_persist=...)`, `admin_get`, `admin_post`, `post_sse_auth`, `stderr_text`. Persist files use `tempfile.mkdtemp`. Global mutable state (TPM_BUCKETS, TPM_WAITERS, etc.) is reset via `_tpm_cleanup(mod)` in unit tests; integration tests use fresh `start_proxy` per test.
5. **Segmented writing (B0→B1..Bn→B-final).** 8 cards; first dispatch produces this B0 skeleton. Subsequent dispatches fill cards 1-8 with complete production + test code + verification commands. Final dispatch (B-final) runs Plan Self-Review across all cards.
6. **Don't change `_log` or `classify_outcome` signatures.** Spec explicitly excludes these.
7. **Workload discipline.** Grep/Glob to locate anchors, then targeted Read of relevant sections (no full-file re-reads). If upstream TPM rate-limit triggers, sleep 60s and retry.
8. **Card commits.** Each card gets a distinct `git commit` with Conventional Commits message (`feat:` for production, `test:` for tests).
9. **Drift alignment.** Spec was written against b1de8b3; worktree base is bd2554f (P4 probe+health API merged). All line-number anchors in this plan are from current code at bd2554f, verified by grep on 2026-10-01.

---

## Task Cards

### Card 1: `TPM_MODEL_BUDGETS` constant + `tpm_limit_for` two-level fallback
**tier:** A
**TDD order:** 1

**Files:**
- `ctyun-stream-fix-proxy.py:100-113` (constants)
- `ctyun-stream-fix-proxy.py:455-466` (`tpm_limit_for`)
- `ctyun-stream-fix-proxy.test.py` (unit tests in `ProxyDashboardUnitTest`)

**Interfaces:**
- Add `TPM_MODEL_BUDGETS: dict = {}` after `TPM_KEY_CAP` (line 106); guarded by `_CFG_LOCK` for writes; reads are reference-assignment-atomic (no lock needed).
- Remove `_TPM_LIMIT_BY_MODEL_BUILTIN` (line 112).
- Keep `TPM_LIMIT_BY_MODEL` (line 110) and `_parse_tpm_limit_by_model` (line 68) for migration seed only (Card 6 consumes it).
- Change `tpm_limit_for(model) -> int` (line 455): `TPM_MODEL_BUDGETS.get(model)` → `TPM_LIMIT` (two-level; remove `_TPM_LIMIT_BY_MODEL_BUILTIN` branch). Docstring updated: "仅启用模型走 budget 判定路径".
- `tpm_limit_for` docstring updated: "两级回退：TPM_MODEL_BUDGETS → TPM_LIMIT".

**Acceptance:**
- `tpm_limit_for("kimi-k3-oc")` returns 30000 when `TPM_MODEL_BUDGETS = {"kimi-k3-oc": 30000}`; returns `TPM_LIMIT` for unknown model.
- `_TPM_LIMIT_BY_MODEL_BUILTIN` no longer exists as module-level name.
- Unit test: `tpm_limit_for` returns budget for enabled model; returns global TPM_LIMIT for non-enabled model.

---

### Card 2: `tpm_admit` opt-in short-circuit + `tpm_settle` docstring update
**tier:** A
**TDD order:** 2 (depends on Card 1 for `TPM_MODEL_BUDGETS`)

**Files:**
- `ctyun-stream-fix-proxy.py:515-582` (`tpm_admit`)
- `ctyun-stream-fix-proxy.py:585-602` (`tpm_settle`)
- `ctyun-stream-fix-proxy.test.py` (unit tests in `ProxyDashboardUnitTest`)

**Interfaces:**
- `tpm_admit(key_id, est, model)`: At function top, before `budget = tpm_limit_for(model)`, add: `if model not in TPM_MODEL_BUDGETS: return ("ok", 0)`. When `model is None`, `None not in {}` is True → always pass-through.
- `tpm_settle(key_id, est, actual, model)`: No signature change. Docstring adds: "未启用模型桶不存在 → `bucket is None` no-op 分支命中，直通模型天然免疫。后续启用→禁用→在途 waiter 继续走完当次判定后自然淘汰。"

**Acceptance:**
- Model NOT in `TPM_MODEL_BUDGETS`: `tpm_admit` returns `("ok", 0)`, no bucket created in `TPM_BUCKETS`, no waiter appended.
- Model IN `TPM_MODEL_BUDGETS`: existing behavior preserved (budget check, queue, reject).
- `model=None` treated as not-in-budgets → pass-through.
- Unit tests: (a) non-enabled model returns ("ok",0) with empty TPM_BUCKETS; (b) enabled model still goes through budget path.
- No change to 429 response body structure or `model_tpm_limit` code.

---

### Card 3: Pure functions — `tpm_body_err_samples`, `tpm_budget_recommend`, `tpm_settings_snapshot` + `tpm_snapshot` update
**tier:** A
**TDD order:** 3 (depends on Card 1 for `TPM_MODEL_BUDGETS` to construct snapshot)

**Files:**
- `ctyun-stream-fix-proxy.py:704` (after `logs_snapshot`) — three new functions
- `ctyun-stream-fix-proxy.py:605-643` (`tpm_snapshot`) — update
- `ctyun-stream-fix-proxy.test.py` (unit tests)

**Interfaces:**

1. `tpm_body_err_samples(lines: list, model: str) -> list[int]`
   - Regex: `re.compile(r"result=body-err\b.*\bmodel=%s\b.*\btpm=(\d+)" % re.escape(model))`
   - Scans list of REQ-line strings; returns list of int tpm values. Tolerates lines without match (skip).
   - Pure function, no locks, no IO.

2. `tpm_budget_recommend(model: str, lines: list) -> dict`
   - Calls `tpm_body_err_samples(lines, model)`.
   - If samples non-empty: `base = min(samples)`; `recommended = (base * 9 // 10) // 1000 * 1000` (floor to thousand, minimum 1000).
   - choices: `[recommended, recommended*3//2//1000*1000, recommended*2]` with labels `["推荐", "宽松×1.5", "宽松×2"]`; dedup.
   - If samples empty: `{"recommended": None, "samples": 0, "hint": "无上游拒绝证据，建议不限流", "choices": [{"value": TPM_LIMIT//2, "label": "默认/2"}, {"value": TPM_LIMIT, "label": "默认"}, {"value": TPM_LIMIT*2, "label": "默认×2"}]}`.
   - Pure function (TPM_LIMIT is module-level constant, read-only).

3. `tpm_settings_snapshot() -> dict`
   - Model list union: `RECENT_REQUESTS` model names (STATS_LOCK) ∪ `STATS["daily_by_model"][today_key()]` keys (STATS_LOCK) ∪ `TPM_MODEL_BUDGETS` keys (_CFG_LOCK). Dedup, sorted.
   - For each model: `{"name", "enabled": name in TPM_MODEL_BUDGETS, "budget": TPM_MODEL_BUDGETS.get(name), "recommend": tpm_budget_recommend(name, lines)}`.
   - `lines`: with `LOG_LOCK`, copy `LOG_RING` entries' `"line"` strings.
   - Returns: `{"models": [...], "default_limit": TPM_LIMIT}`.
   - Three locks held separately (no nesting): LOG_LOCK → copy lines; STATS_LOCK → read RECENT_REQUESTS/daily_by_model; _CFG_LOCK → read TPM_MODEL_BUDGETS.

4. `tpm_snapshot()` update (line 605):
   - Rename `"limit_by_model"` key → `"model_budgets"` in `config` dict; content = `dict(TPM_MODEL_BUDGETS)`.
   - Add `"enabled_models": sorted(TPM_MODEL_BUDGETS.keys())` to `config` dict.
   - `"remaining"` per-model budget calc (`tpm_limit_for(model) - bucket.used`, line 628) keeps working via two-level fallback.

**Acceptance:**
- `tpm_body_err_samples` correctly extracts tpm values from simulated REQ lines; ignores non-matching models; tolerates lines without tpm= field.
- `tpm_budget_recommend` with samples `[31038, 32375, 33227]` → `recommended=27000`, choices `[27000, 40000, 54000]` with labels. No-samples path returns hint + default choices.
- `tpm_settings_snapshot` returns correct models list, enabled flag, budget, recommend block.
- `tpm_snapshot` returns `enabled_models` and `model_budgets` in config.

---

### Card 4: Persist layer — `load_tpm_model_budgets`, `save_stats_counters` extension, `persist_upstream` extension, `set_tpm_model_budgets`
**tier:** A
**TDD order:** 4 (depends on Card 1 for `TPM_MODEL_BUDGETS` const)

**Files:**
- `ctyun-stream-fix-proxy.py:841-849` (`persist_upstream`)
- `ctyun-stream-fix-proxy.py:978-1006` (`save_stats_counters`)
- `ctyun-stream-fix-proxy.py:1031` (after `load_capture_errors`) — new `load_tpm_model_budgets`
- `ctyun-stream-fix-proxy.py:1329-1346` (after `set_capture_errors`) — new `set_tpm_model_budgets`
- `ctyun-stream-fix-proxy.test.py` (unit + integration tests)

**Interfaces:**

1. `load_tpm_model_budgets(path: str) -> dict` (after line 1035)
   - Reads persist file via `_load_persist_file(path)`.
   - Extracts `"tpm_model_budgets"` key.
   - Per-key validation: model name must be non-empty str (≤200 chars); value must be int > 0. ≤32 keys total.
   - Invalid entries skipped (tolerance pattern matching `load_daily_by_model_buckets`).
   - Returns validated dict (may be empty).

2. `persist_upstream(base, path, capture_errors=False)` — add `tpm_model_budgets` key
   - Signature unchanged.
   - Inside `with _CFG_LOCK: budgets_snap = dict(TPM_MODEL_BUDGETS)` (before the json.dump line ~847).
   - json.dump now writes `{"upstream_base": ..., "capture_errors": ..., "tpm_model_budgets": budgets_snap}`.
   - This prevents "change upstream endpoint" from silently dropping model budgets.

3. `save_stats_counters(path)` — add `tpm_model_budgets` key (line 990-994)
   - After `probe_enabled` capture, add `budgets = dict(TPM_MODEL_BUDGETS)` inside `with _CFG_LOCK:` block.
   - Add `"tpm_model_budgets": budgets` to the json.dump dict (line 999-1005).

4. `set_tpm_model_budgets(budgets: dict) -> None` (after line 1346)
   - `global TPM_MODEL_BUDGETS`
   - `with _CFG_LOCK: TPM_MODEL_BUDGETS = dict(budgets); persist_upstream(UPSTREAM_BASE, PERSIST_PATH, capture_errors=CAPTURE_ERRORS)`
   - After lock: `save_stats_counters(PERSIST_PATH)` (same deadlock-avoidance pattern as `set_capture_errors`).

**Acceptance:**
- `load_tpm_model_budgets` rejects missing file → `{}`; rejects non-dict value → `{}`; tolerates invalid entries (skip); accepts valid entries.
- `persist_upstream` writes `tpm_model_budgets` key alongside `upstream_base`/`capture_errors`.
- `save_stats_counters` writes `tpm_model_budgets` key; roundtrip persist→load preserves exact dict.
- `set_tpm_model_budgets` atomically replaces `TPM_MODEL_BUDGETS`, triggers persist + stats save.
- Unit test: `load_tpm_model_budgets` tolerance matrix (missing, corrupted, invalid keys, valid keys).

---

### Card 5: API endpoints — `GET /api/tpm_settings`, `GET /api/config` extension, `POST /api/config` extension
**tier:** A
**TDD order:** 5 (depends on Cards 1-4)

**Files:**
- `ctyun-stream-fix-proxy.py:2134-2217` (`do_GET`)
- `ctyun-stream-fix-proxy.py:2219-2262` (`do_POST /api/config`)

**Interfaces:**

1. `GET /api/tpm_settings` (new endpoint in `do_GET`, after `/api/tpm_stats` branch ~line 2143)
   - New `elif path == "/api/tpm_settings":` branch calling `self._send_json(200, tpm_settings_snapshot())`.
   - No auth required (same as `/api/stats`).

2. `GET /api/config` extension (line 2144-2149)
   - Add `"tpm_model_budgets"` key to the payload dict inside `with _CFG_LOCK:` block.

3. `POST /api/config` extension (line 2245-2262 area)
   - After `capture_errors` parsing (~line 2246), extract optional `tpm = data.get("tpm_model_budgets")` (None when absent).
   - Validation before calling setters (when `tpm is not None`): must be dict; ≤32 keys; each key non-empty str ≤200 chars; each value int > 0; violation → 400 with per-item error message.
   - After `set_capture_errors(cap)` (~line 2258): if tpm given, call `set_tpm_model_budgets(tpm)`.
   - Response dict (~line 2260): add `"tpm_model_budgets"` key (current value under lock).

**Acceptance:**
- `GET /api/tpm_settings` returns `{"models": [...], "default_limit": ...}` with correct model list, enabled flags, recommend blocks.
- `GET /api/config` returns `tpm_model_budgets` key.
- `POST /api/config` with valid `{"tpm_model_budgets": {"kimi": 1000}}` → 200, response includes updated budgets.
- `POST /api/config` with invalid values → 400 with descriptive error.
- Auth: `POST /api/config` requires localhost or X-Admin-Token (existing `write_allowed` check unchanged).

---

### Card 6: `main()` startup migration — seed TPM_MODEL_BUDGETS from env or default
**tier:** A
**TDD order:** 6 (depends on Card 4 for `load_tpm_model_budgets`, Card 1 for `TPM_MODEL_BUDGETS`)

**Files:**
- `ctyun-stream-fix-proxy.py:2875-2912` (`main()`)

**Interfaces:**
- After `CAPTURE_ERRORS = load_capture_errors(PERSIST_PATH)` (line 2887), add `global TPM_MODEL_BUDGETS` + migration block:
  - Call `load_tpm_model_budgets(PERSIST_PATH)` → `budgets`.
  - If `budgets` empty and `CTYUN_TPM_LIMIT_BY_MODEL` env non-empty: parse via `_parse_tpm_limit_by_model`, log migration line to stderr, `save_stats_counters(PERSIST_PATH)` to persist immediately.
  - If `budgets` empty and no env: default `{"kimi-k3-oc": 30000}` seed.
  - Assign `TPM_MODEL_BUDGETS = budgets`.

**Acceptance:**
- Persist file has `tpm_model_budgets` → loaded as-is (no env override).
- Persist missing + `CTYUN_TPM_LIMIT_BY_MODEL` env non-empty → env parsed and persisted; stderr contains migration message.
- Both missing → default `{"kimi-k3-oc": 30000}` seed.
- `TPM_MODEL_BUDGETS` assigned before server starts (no race).

---

### Card 7: Dashboard v1.5 — TPM 限流设置 section (HTML + JS)
**tier:** A
**TDD order:** 7 (depends on Card 5 for `/api/tpm_settings` endpoint)

**Files:**
- `ctyun-stream-fix-proxy.py:2304-2861` (`_DASHBOARD_SRC`)
- `ctyun-stream-fix-proxy.test.py` (`test_dashboard_html_full_page`)

**Interfaces:**

1. HTML section (inserted after upstream section `</section>` at line 2400):
   - New `<section class="card" id="tpm-settings-card">` with child elements: `<div class="card-title">TPM 限流设置（按模型启用）</div>`, `<div id="tpm-models">` (row container), `<button id="tpm-save" type="button">保存限流设置</button>`, `<p class="msg" id="tpm-msg" role="status"></p>`.

2. JS functions (inserted before `poll()` first call at line 2856):
   - `loadTpmSettings()` — fetch `/api/tpm_settings` → call `renderTpmSettings(data)`.
   - `renderTpmSettings(data)` — build per-model rows using `el()` / `textContent`: checkbox (`data-model` attr), `<select>` (`data-model` attr) with options from `choices` array (value/label), hint `<span>`.
   - Save handler: `$("tpm-save")` click → collect checked models + selected budget values → `fetch POST /api/config` with `{tpm_model_budgets: collected}` → show ok/err msg → reload `loadTpmSettings()`.
   - No `innerHTML` — use `el()` + `textContent` throughout (consistent with existing dashboard pattern).
   - `loadTpmSettings()` called once at startup (before `poll()` first call) and after save success.

**Acceptance:**
- Dashboard HTML contains `TPM 限流设置`, `id="tpm-models"`, `id="tpm-save"`, `/api/tpm_settings`.
- No `innerHTML` in the new JS code.
- Checkbox state reflects `enabled` from API; select options match `choices`; save triggers POST and refresh.
- Existing `test_dashboard_html_full_page` extended with assertions for the new elements.

---

### Card 8: All tests — unit tests, integration test, TpmPerModelTest migration
**tier:** A
**TDD order:** 8 (runs after all production cards; each test class should be independent)

**Files:**
- `ctyun-stream-fix-proxy.test.py`

**Test classes / methods:**

1. **`ProxyDashboardUnitTest` additions:**
   - `_tpm_cleanup` extended to also reset `TPM_MODEL_BUDGETS` (preserve/restore pattern).
   - `test_tpm_body_err_samples_extracts_tpm` — construct line list, assert extracted values.
   - `test_tpm_body_err_samples_filters_by_model` — different model lines skipped.
   - `test_tpm_body_err_samples_tolerates_no_tpm` — lines without tpm= ignored.
   - `test_tpm_budget_recommend_with_samples` — 3 samples → recommended=27000, choices=[27000,40000,54000].
   - `test_tpm_budget_recommend_no_samples` — returns hint + default choices.
   - `test_tpm_admit_non_enabled_bypasses` — non-enabled model returns ("ok",0), no bucket created.
   - `test_tpm_admit_enabled_works` — enabled model goes through budget path.
   - `test_tpm_limit_for_two_level` — enabled model gets budget; non-enabled gets TPM_LIMIT.
   - `test_load_tpm_model_budgets_tolerance` — missing/empty/corrupt/invalid entries.

2. **New class `TpmModelSelectionTest(unittest.TestCase)`:**
   - `setUp`: `make_scripted_upstream` + `free_port`.
   - `tearDown`: terminate proxy, remove persist_dir, stop upstreams.
   - `test_disabled_model_passthrough` — seed `{}` (no models enabled), `CTYUN_TPM_LIMIT=50`, send est>50 request → 200, `/api/tpm_stats` buckets empty, no `tpm-queue-full` in stderr.
   - `test_enabled_model_429` — seed `{"kimi-k3-oc": 1000}`, same key: small kimi (prime) + big kimi → 429; same key deepseek → 200.
   - `test_recommend_choices_populated` — seed `{"kimi": 1000}`, send traffic, `/api/tpm_settings` models list non-empty, recommend choices present.
   - `test_save_restart_persist_roundtrip` — POST `{"tpm_model_budgets": {"glm-5.3-oc": 50000}}` → 200; persist file contains key; restart proxy (same persist dir) → GET /api/config returns saved value.
   - `test_env_migration_path` — seed_persist without `tpm_model_budgets` + `extra_env={"CTYUN_TPM_LIMIT_BY_MODEL": "kimi-k3-oc:1000"}` → after startup, GET /api/config `tpm_model_budgets == {"kimi-k3-oc": 1000}`, persist file has the key.
   - `test_post_empty_budgets_clears` — POST `{"tpm_model_budgets": {}}` → disabled models all pass-through (est>TPM_LIMIT → 200).
   - `test_post_invalid_budgets_400` — POST `{"tpm_model_budgets": {"m": -1}}` → 400; POST `{"tpm_model_budgets": "x"}` → 400; POST with >32 keys → 400.

3. **`test_dashboard_html_full_page` extension (in existing test, ~line 2634):**
   - `assertIn("TPM 限流设置", html)`
   - `assertIn('id="tpm-models"', html)`
   - `assertIn('id="tpm-save"', html)`
   - `assertNotIn("innerHTML", js_code)` (or scan the JS portions).

4. **`TpmPerModelTest` migration (existing class at line 4983):**
   - `test_kimi_independent_budget` (line 5005): Replace `extra_env={"CTYUN_TPM_LIMIT_BY_MODEL": "kimi-k3-oc:1000"}` with `seed_persist={"tpm_model_budgets": {"kimi-k3-oc": 1000}}` (or keep env path with assertion that migration stderr line appears). Implementer chooses: prefer seed_persist for clean test, keep one env-migration variant.
   - `test_tpm_stats_shape_with_limit_by_model` (line 5125): Update assertions — `limit_by_model` snapshot may show `{}` (not persisted yet) or migrated value; update assertion accordingly. `model_budgets` key should exist.
   - All other `TpmPerModelTest` methods: ensure they pass under new opt-in semantics. If a test uses global `TPM_LIMIT` budget path without enabling the model, it may need `seed_persist` to enable that model.

**Acceptance:**
- `python3 ctyun-stream-fix-proxy.test.py` all green, baseline 219 + new tests all pass.
- `TpmPerModelTest` migration: no `CTYUN_TPM_LIMIT_BY_MODEL` env in tests (or exactly one migration-path test); all tests use `seed_persist` with `tpm_model_budgets`.
- Coverage of spec Acceptance items: disabled passthrough / enabled 429 / recommend algorithm / model list generation / save+restart persist / env migration / UI block rendering / invalid input rejection.

---

## Card Execution Order (TDD sequence)

| Order | Card | Depends On | Commit Message |
|-------|------|-----------|----------------|
| 1 | TPM_MODEL_BUDGETS + tpm_limit_for | — | `feat(proxy): add TPM_MODEL_BUDGETS and two-level tpm_limit_for` |
| 2 | tpm_admit opt-in + tpm_settle | Card 1 | `feat(proxy): opt-in tpm_admit — non-enabled models bypass rate limit` |
| 3 | Pure functions + tpm_snapshot | Card 1 | `feat(proxy): tpm_body_err_samples, tpm_budget_recommend, tpm_settings_snapshot` |
| 4 | Persist layer | Card 1 | `feat(proxy): load/save/set_tpm_model_budgets with persist extension` |
| 5 | API endpoints | Cards 3, 4 | `feat(proxy): GET /api/tpm_settings and POST /api/config tpm_model_budgets` |
| 6 | main() startup migration | Card 4 | `feat(proxy): seed TPM_MODEL_BUDGETS from env or default on startup` |
| 7 | Dashboard v1.5 | Card 5 | `feat(dashboard): TPM 限流设置 section v1.5` |
| 8 | All tests | All above | `test: TPM model selection unit + integration tests + TpmPerModelTest migration` |

---

*B0 skeleton complete. B1..Bn dispatch will fill production code + test code + verification commands for cards 1-8. B-final will run Plan Self-Review across all cards.*