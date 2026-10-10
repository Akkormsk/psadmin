# Claude / Codex handoff

## Verified starting point

- Repository: `https://github.com/Akkormsk/psadmin.git`
- Working branch: `codex/calculation-v2-foundation`
- Last verified implementation and deployed test commit: `845ef32`.
- Local worktree used for this work: `C:\Users\d_kor\PycharmProjects\psadmin2\.codex\worktrees\assistant-stage-answer`
- Test checkout: `/home/deploy/psadmin-test`; isolated service: `psadmin-test-test-web-1`; URL: `https://test.admin.psodin.ru/`
- Production checkout: `/home/deploy/psadmin`, branch `main`, commit `c626a595fe28086649b95c1e3deae27a90728b3b`. Do not modify it without direct owner approval.

## Run and verify

From a checkout with dependencies installed:

```powershell
python manage.py test tenders.test_provider_workspace tenders.test_provider_calculators tenders.test_sewing_price_list --keepdb --noinput
python manage.py check
```

On the isolated test VDS, deployment is run from `/home/deploy/psadmin-test` using `./deploy/vds/twin.sh up`. It builds `test-web`, migrates only the isolated test database and waits for health. `twin.sh refresh-db` copies production data and is destructive to the test DB: do not run it without explicit approval. Never deploy production from this workflow.

## Completed

- Global assistant drawer: conversation history, clear/archive controls, page-context snapshots and registered provider workflows.
- Sewing XLS upload/normalization, confirmation, provider binding and deterministic `calculate_provider` result.
- Provider workspace with Overview, Prices and Calculator; assistant opens the same provider card.
- Provider price cleanup: drafts can be deleted; superseded/inactive versions are hidden from normal history while retained for recoverability.
- Global Data entry from home and account navigation, with Counterparties and Capabilities tabs; a counterparty opens through the existing workspace layer.
- Price versions expand into real stored structured rows, including variants, minimum quantities and formatted prices.
- Test deployment at `3460ce9`; 5 provider workspace tests and `manage.py check` passed after deployment.

## Current limitations

- The global **Data** entry and user-facing workspace are implemented; the header was simplified to one Back/Home/profile row. Browser visual acceptance still requires an authenticated owner session. `/tenders/production/base/` remains a route-drawer JSON endpoint for route operations.
- Provider workspace navigation was automated-tested but final browser visual acceptance requires an authenticated owner session.
- The full V2 calculation pipeline, Universal Data Agent, Project111 and FSPrint remain unfinished.

## Exact next task

Visually accept the polished shared navigation flow: home → Data → provider → Calculator → Back and direct/assistant opening. Then add multi-role filtering and compact counterparty details using `Counterparty` without a duplicate registry; after UI acceptance, begin Universal Data Agent.

## Safety

No production deployment, production configuration change, secret in Git, destructive migration, or uncontrolled AI/backfill. Read `AGENTS.md`, `docs/PSADMIN.md`, and `docs/assistant_protocol.md` before touching assistant, catalogue, feedback or calculation behaviour.

## Copy-paste prompt

```text
Read AGENTS.md, docs/PSADMIN.md and docs/CLAUDE_HANDOFF.md. Confirm the current branch, HEAD, Git status and isolated test deployment before editing. Continue the exact next task from the handoff: add the smallest global Data workspace reusing Counterparty and ProcessDefinition, then open counterparties through the existing workspace layer. Do not repeat prior audits, add duplicate registries/calculators/learning engines, or alter production. Use focused tests and browser verification where authentication is available. Commit logical completed work, push it to codex/calculation-v2-foundation and deploy only the isolated test stack.
```
