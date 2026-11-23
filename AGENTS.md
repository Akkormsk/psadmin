# PSADMIN: Project Contract

## 1. Environments & Core Invariants
- **Sources of Truth:** GitHub for code; Timeweb PostgreSQL for operational data. Never commit secrets, dumps, or client/supplier exports.
- **Safety Gates:** Work only in `feature/*` branches and TEST. Never touch PROD, prod flags, destructive migrations, deploy, or mass AI/backfill without explicit owner approval (silence is not consent).
- **Architecture:** Preserve V1 until V2 is accepted. Always inspect data structures, entities, and relations before searching, filtering, or building UI. Use existing Django models and services; no parallel registries or calculation engines. LLM interprets language; backend executes math, filtering, and ranking.
- **Django & UI:** New user-facing fields exposed to assistant READ must have `verbose_name` (reply with human labels, not field names). Verify deploy via actual commit SHA and health; verify UI in a real browser ONLY when explicitly requested by the user.
- **Testing & Token Efficiency:** Verify changes with fast end-to-end smoke tests using realistic anonymized data snapshots (golden fixtures) through the real shared backend executor. Run only targeted domain tests unless core logic was touched—never generate bloated synthetic mock suites or run full-repo tests for isolated changes.

## 2. Branch Lifecycle & Cleanup
- **Hierarchy:** `main` (PROD) ← `test-server` (staging, auto-deploys on push) ← `feature/*` (1 chat = 1 feature).
- **Periodic Branch Cleanup:** Whenever a `feature/*` branch is merged into `test-server` or `main`, immediately propose deleting the merged local and remote `feature/*` branch and its temporary handoff file. Also proactively flag stale/merged `feature/*` branches for cleanup at the start of new tasks.

## 3. Documentation & Modular Protocols
- **Single Source of Truth:** `docs/assistant_protocol.md` is the high-level system index and invariant registry. Do not create `CLAUDE.md` or random notes.
- **Domain Modularity:** When updating documentation adds or expands a distinct business domain, proactively split that domain into `docs/protocols/<domain>.md` and link it in the main index. Read ONLY the protocol file relevant to the current task.
- **Statuses & Sync:** Use honest statuses (`PROD_VERIFIED`, `TEST_VERIFIED`, `CODE_ONLY`, `DECIDED_BACKLOG`, `DEFERRED`, `REJECTED`) with deployed SHA. Update the relevant protocol in the same commit set as code/TEST changes. Remember: `test-server` deploys on every push; PROD deploy requires explicit approval (and extra confirmation during 09:00–19:00 MSK).

## 4. Assistant Capabilities (M3.1 / M3.2)
- **Shared Executors:** Put HTTP/JS/write logic in a shared backend executor for Web and Telegram (no Telegram-specific business logic, no assistant-only helpers). Require permissions, preview/confirmation, idempotency, and audit.
- **Scope:** M3.1 = one macro-operation with one final confirmation. Multiple independent READ/ACTION = M3.2. Test every published `source` and unified executor before marking `TEST_VERIFIED`.
## 5. Development Memory / Verified Cases
- For work on document reading, EIS, Hermes, AI, or regressions, consult `docs/protocols/development_memory.md`, search open AND closed repository Issues and relevant domain protocol before claiming an old case was never seen or is unknown. Preserve VERIFIED_SUCCESS, VERIFIED_FAILURE, NOT_OBSERVABLE and evidence separately.
- After investigating a real case, update one existing matching curated Finding Issue or create one with SHA, case, observed result and evidence. Never promote a CI signal into a product defect without confirmation. Do not store raw logs, documents or secrets. No automatic production fixes.
