# Project invariants

## Shared agent rules

- GitHub is the source of truth for code. Production PostgreSQL is the source of truth for operational knowledge; never copy it into Git.
- Work in an isolated branch and test environment. Do not change production, production flags, secrets, or run destructive migrations without the owner's direct approval.
- Preserve V1 behaviour until the corresponding V2 workflow has been accepted. Reuse existing models and domain services; do not add parallel registries, calculator engines, or learning stores.
- Do not hardcode suppliers, products, or business rules in core logic. LLMs interpret language; backend services validate, rank, and calculate.
- Verify the actual test commit and health before claiming a deployment. Do not run mass AI/backfill work without a defined budget.
- Check UI changes in a real browser when authentication is available. Do not create pages-inside-pages, duplicate global chrome, or substitute a fake modal page for an in-app workspace.
- Every new user-facing screen must use the PSADMIN navigation contract: logical parent fallback, Home access, and no navigation traps.
- After a milestone report: completed work, verification, commit, test deployment state, limitations, and a concrete manual check.

- Before changing assistant, catalogue search, ranking, feedback, or calculation behavior, read `docs/assistant_protocol.md`.
- The production database on Timeweb is the canonical assistant knowledge base.
- Local automated tests must not connect to or modify production data.
- Git contains code, migrations, and curated test fixtures, not production database dumps, credentials, customer documents, or supplier exports.
- The LLM interprets natural language. Calculations, validation, catalogue filtering, and ranking are performed by the backend.
- Confirmed administrator feedback and superseded knowledge must remain versioned and recoverable.
- Production changes are deployed through GitHub. Do not deploy or mutate Timeweb configuration without explicit authorization.
- Supplier integrations must not download images unless explicitly requested.
- Every new user-facing Django model and field that can be exposed through assistant READ must define a meaningful human `verbose_name`; add `help_text` when the label alone cannot distinguish its business meaning. Do not create a second assistant-only naming catalogue: READ publishes these model metadata automatically, and the agent must use human labels in replies rather than technical field names.
- If a site screen exposes a composite business selection that cannot be represented by ordinary ORM filters, publish it on the owning model through the existing generic READ view hook and reuse the same shared read-only service as the screen. Never copy its rules into the agent or add a phrase-specific tool; test that the screen and READ use the same selector.

# Personal coding behavior

These rules apply to all coding work across projects on this machine. They are the sole repo-wide human/AI development instructions; do not introduce or require CLAUDE.md.

## 1. Think before coding

Don't assume. Don't hide confusion. Surface tradeoffs.

Before implementing:

- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them — don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity first

Minimum code that solves the problem. Nothing speculative.

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Self-check: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical changes

Touch only what you must. Clean up only your own mess.

When editing existing code:

- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it — don't delete it.

When your changes create orphans:

- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

Test: every changed line should trace directly to the user's request.

## 4. Goal-driven execution

Define success criteria. Loop until verified.

Transform tasks into verifiable goals:

- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan with explicit verification steps.

## 5. TDD and Test-first execution

Build your own evaluation loop. Fail first, then fix.

Before writing functional code:

- Whenever possible, write the test that reproduces the bug or validates the new feature first.
- Run the test to watch it fail before implementing the solution.
- Use local test execution as your primary feedback mechanism for agent self-evaluation.
- If a test cannot be written upfront, explicitly state the blocker before proceeding.

Test: You should observe a failing test state before you ever write the code to make it pass.

## 6. Code over comments

Self-explanatory code first. If comments are necessary, they must explain the code.
Comment context and constraints, not mechanics.

- Prioritize expressive naming and clean structure over explanatory comments.
- Only comment when the logic is inherently non-obvious or bound by external schemas.
- Focus comments strictly on the "why" of system quirks, business rules, and third-party restrictions.
- DO NOT explain language features, frameworks, or generic technology mechanics.

When tempted to write a comment, first try renaming or restructuring. Usually that removes the need.
Self-check: "If I delete this comment, does the code become harder to understand correctly?" If no, delete it.

---

Tradeoff: these rules bias toward caution over speed. For trivial tasks, use judgment.

## Единый живой протокол и сменяемость AI-разработчика (с 2026-10-13)

**Правила:** этот `AGENTS.md` — единственная инструкция о том, КАК вести разработку. `docs/assistant_protocol.md` — единственный документ о том, ЧТО реально работает, как проходит тендер от ЕИС до исполнения и какие решения согласованы, но не выпущены. Это правило относится к Claude, Codex, ChatGPT, локальным и облачным агентам одинаково. При смене агента не нужен отдельный handoff: прочитать `AGENTS.md`, протокол `main`, протокол `test-server`, затем проверить Git/развёрнутые SHA и тесты.

### Различать код, тест и фактический релиз

- `main/docs/assistant_protocol.md` описывает **подтверждённое поведение продакшена**, а не обещания, только что слитый код или гипотезы. При неустановленном deployed SHA честно помечать состояние «сверка с сервером не выполнена», не объявлять Git-ветку запущенным релизом.
- `test-server/docs/assistant_protocol.md` описывает **подтверждённое поведение тестового сервера**, отличия от продакшена и отдельно план. Экспериментальная ветка/PR и локальные тесты не означают «развёрнуто на тесте».
- Для каждой возможности явно указывать статус: `PROD_VERIFIED` (проверены сервер и коммит), `TEST_VERIFIED`, `CODE_ONLY` (присутствует в ветке, без живой приёмки), `DECIDED_BACKLOG`, `DEFERRED` или `REJECTED`. Не повышать статус на основании одного текста handoff или mock-теста. Фактические статусы серверов — только по release SHA (для тестового VDS `.twin-version` или эквивалентный достоверный источник, для прода — развёрнутый Git SHA/версия релиза).
- В начале каждого протокола фиксировать дату, ветку, commit SHA исходного кода, deployed SHA **или** отметку «не подтверждён», пройденные проверки/известные пробелы. Не хранить персональные данные, секреты и реальные выгрузки.
- Не путать три вещи: **согласованный продуктовый план**, **код и автоматические тесты** и **подтверждённую работу на сервере**. Результат промежуточного этапа фиксировать в протоколе текущей feature-ветки, но не объявлять тестовым или продовым релизом.
- При изменении тестового кода/архитектуры, принятом решении пользователя, находке, результате эксперимента или переносе в тест: **в том же наборе коммитов** обновлять `docs/assistant_protocol.md` в рабочей ветке. После успешного тестового деплоя сверять SHA/health и приводить протокол `test-server` к реально проверенному состоянию; бэклог сохранять отдельно от работающего.
- Перед **каждым** production deploy подготовить изменение протокола для `main`; после релиза сверить deployed SHA и основные сценарии и актуализировать запись о реально работающем. Если сервер и ветка разошлись, не исправлять историю догадками, не заявлять окончание релиза, зафиксировать расхождение. Никогда не обновлять продовый протокол только потому, что M3/другая ветка готова.
- Протокол описывает полный путь (ЕИС → фильтрация → документы/факты/риски → V2 коммерческие позиции и маршруты → подготовка и кэш → калькуляторы/экономика → торги/протокол/результат → исполнение), вызовы и модели, источники истины, ограничения, очередь/интеграции, тесты, решения и бэклог. Ссылаться на реальные кодовые пути и тесты, а не пересказывать предполагаемые функции.
- **Запрещено создавать новые текстовые документы** `*.md`/`*.txt` в качестве roadmap, handoff, промптов Claude/Codex, архитектурных заметок, TODO, отчётов или параллельных протоколов. Новые сведения пишутся в `docs/assistant_protocol.md`; нормы работы — в `AGENTS.md`. Уже существующие файлы до подтверждённого переноса не удалять. Исключения — необходимые для работы кода/инструментов стандартизованные файлы (`requirements.txt`, данные тестов, машинные fixtures, лицензии), но не вторые человеческие инструкции.
- История тестовых замеров и контрактные автотесты могут оставаться отдельными **машинными артефактами**; они не становятся вторым текстовым источником решений. Старые заметки удаляются только после проверки, что все действующие требования перенесены, и с одобрения владельца.
- Любое слияние веток, удаление документов, миграции production DB, prod/test deploy или массовый AI backfill — только в соответствии с ранее выданным явным разрешением владельца; отсутствие ответа не является согласием.

### Контрольный список перед передачей работы или релизом

1. Сверить `git branch`, `git status`, HEAD, base/PR, дату и последний подтверждённый deployed SHA нужного окружения.
2. Открыть обе версии протокола, сравнить изменения схемы и алгоритмов с кодом, миграциями и тестами; актуализировать статусы, открытые вопросы, отклонённые подходы и следующий проверяемый шаг.
3. Не удалять не перенесённые требования; не объявлять интеграцию принятой по одному адаптеру; сохранять V1 до явной приёмки V2.
4. Запустить целевые тесты; указать реальные результаты или блокер. После релиза проверить health и пользовательский путь на целевом сервере, зафиксировать точную ревизию.
5. В итоговом сообщении оставить пользователю: изменения, commit SHA, тесты/серверную приёмку, оставшийся долг, нужен ли его approval. Никаких отдельных handoff-файлов.

### Дополнительные проверяемые ограничения релиза

- `test-server` автоматически деплоится GitHub Actions при **любом push**, включая изменения только документов. Перед merge в эту ветку проверить последний успешный workflow и текущую ветку/релиз, чтобы не затереть незакоммиченные серверные изменения; после push дождаться успешного deploy+health. Если SHA нельзя проверить — не обещать, что сервер соответствует ветке.
- Прод-деплой запрещён без прямой команды владельца. Сохраняется ранее согласованное дополнительное ограничение: в **09:00–19:00 по Москве** нужно отдельно и однозначно подтвердить выполнение релиза именно в рабочее окно; вне окна также требуется явная команда на деплой. Обязательны backup, миграции и проверка здоровья по реально используемым скриптам.
- При добавлении новых приложений либо изменении ACTION требуется тест, что все опубликованные `source` реально разрешаются в callable и что UI/ассистент используют **один** backend executor. Ни один статус `PROD_VERIFIED` или `TEST_VERIFIED` не ставить только по наличию Python-функции.
- Удалённые в ходе консолидации инструкции восстанавливаются из Git-истории соответствующей ветки. Удаление markdown **не** означает удаление тестов/фикстур, миграций, механизмов журналирования или операционных скриптов.

### Рост энциклопедии пользовательских операций (M3.1, решение владельца 2026-10-13)

- Единица энциклопедии — законченная пользовательская задача (макрофункция), а не каждый внутренний вызов, сохранение, лог или служебный шаг. Описание включает назначение, обязательные входы, результат, побочные эффекты, подтверждение и источник общего исполнителя.
- Любая новая или изменённая пользовательская возможность сайта в том же PR должна быть сопоставлена с реестром assistant capabilities приложения-владельца: добавить/изменить/исключить декларацию и тесты. Нельзя оставлять новые возможности неизвестными энциклопедии молча. Если операция не может быть безопасно опубликована, явно указать причину в действующем протоколе, а не создавать ложный executable source.
- Инвентаризация должна быть автоматизируема и проверяема тестами, но обнаружение новой функции не означает автоматического разрешения AI на её выполнение. Для записи обязательны права, preview/confirmation, идемпотентность и аудит.
- Для функций, доступных только через логику HTTP View/JavaScript, сначала выделить реальный общий backend executor, используемый одновременно сайтом и агентом. Нельзя переписывать бизнес-логику в assistant-only helper.
- Пользователь может дать входные данные в любом количестве сообщений. Сохранять собранные аргументы и задавать вопросы только о недостающих; изменение введённого значения заменяет старое. На исполнение переходить только после проверки полного контракта входов и server-side подтверждения записи.
- Интерфейсные представления Web и Telegram являются адаптерами к общему диалоговому состоянию и семантическим действиям: текст, одиночный/множественный выбор, дата/число, вложение, preview, подтверждение/отмена, статус долгой задачи. Telegram не реализует отдельную бизнес-логику.
- M3.1 — одна макрооперация с любым количеством её штатных backend-шагов, один требующий подтверждения итог. Самостоятельный AI-цикл из нескольких независимых READ/ACTION относится к M3.2, не маскировать его гигантским AI-only исполнителем.
