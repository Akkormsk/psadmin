# PSADMIN: Project Contract

## 1. Environments & Core Invariants
- **Sources of Truth:** GitHub for code; Timeweb PostgreSQL for operational data. Never commit secrets, dumps, or client/supplier exports.
- **Safety Gates:** Work only in `feature/*` branches and TEST. Never touch PROD, prod flags, destructive migrations, deploy, or mass AI/backfill without explicit owner approval (silence is not consent).
- **Architecture:** Preserve V1 until V2 is accepted. Always inspect data structures, entities, and relations before searching, filtering, or building UI. Use existing Django models and services; no parallel registries or calculation engines. LLM interprets language; backend executes math, filtering, and ranking.
- **Django & UI:** New user-facing fields exposed to assistant READ must have `verbose_name` (reply with human labels, not field names). Verify deploy via actual commit SHA and health; verify UI in a real browser ONLY when explicitly requested by the user.
- **Testing & Token Efficiency:** Verify changes with fast end-to-end smoke tests using realistic anonymized data snapshots (golden fixtures) through the real shared backend executor. Run only targeted domain tests unless core logic was touched—never generate bloated synthetic mock suites or run full-repo tests for isolated changes.
- **Bug verification:** Use a short regression test based on the reported scenario. Confirm it fails before the fix and passes after it. If the result is saved for later use, verify what the next step actually reads. Run this test before reporting the bug as fixed.
- **Scoped cleanup:** When changing code, remove obsolete logic in the affected area. Merge duplicate tests only when the remaining test still catches the same failures.

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
## GitHub Issues — единственный реестр подтверждённых ошибок

- Перед регистрацией исследовать сохранённую диагностику/логи, определить конкретную техническую причину, подтвердить воспроизведение или доказательство отказа. Симптом «не прочитался», предположение, общий статус partial/failed и сам факт неудачного workflow недостаточны. Не создавать Issue до установления причины. Не создавать параллельные реестры и модули.
- Искать совпадения среди открытых и закрытых Issues по механизму ошибки, а не по тендеру, сообщению пользователя или номеру задания. Один стабильный ключ на одну причину (домен/компонент/код отказа); не включать номер тендера, дату, SHA или ID запуска в ключ. Использовать только общий scripts/issue_memory.py через сериализованный workflow регистрации; не создавать Issues вручную/API в обход проверки. Повторный подтверждённый случай увеличивает счётчик; повторная запись того же идентификатора события ничего не меняет. Возврат исправленной ошибки открывает прежний Issue.
- Обязательные данные: техническая причина и этап, сценарий, ожидаемое и фактическое поведение, полный SHA, ссылка на доказательство, стабильный ID случая, точечный автоматический сценарий воспроизведения (или явно manual, если автоматизация пока невозможна). Не публиковать секреты и сырые документы. Неподтверждённые случаи остаются в штатной диагностике источника до исследования.
- После каждого успешного деплоя проверять все открытые подтверждённые ошибки по их сохранённым регрессиям, не запускать платные вызовы. Закрывать только при PASS конкретного ранее воспроизведённого сценария на проверенной версии; визуальная приёмка и отсутствие проверяемого сценария оставляют Issue открытым. Успех deploy/health или общего CI сам по себе не доказательство исправления. Не закрывать production-дефект по TEST; учитывать область подтверждения.
