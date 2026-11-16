# Окружения, доставка и диагностика

[Главный индекс](../assistant_protocol.md)

Статус приложения подтверждается фактическим deployed SHA, а не HEAD документации.

## 13. Окружения, доставка и диагностика

| Контур | Ветка и адрес | Серверные пути | Подтверждение версии |
|---|---|---|---|
| PROD | `main`, https://admin.psodin.ru/ | `/home/deploy/psadmin`, `compose.yml`, `web`/`db` | `X-PSADMIN-Revision`, `.release-sha`, production Actions |
| TEST | `test-server`, https://test.admin.psodin.ru/ | `/home/deploy/psadmin-test`, `compose.test.yml`, `test-web`/`test-db` | host/container `.twin-version`, test Actions |

Оба приложения на одном VDS, но код/сервисы/базы разделены. Не переносить флаги и runtime между ними вслепую.

TEST push → `deploy-test-vds.yml` → проверенный собственный backup TEST → архив/сохранение прежнего release → `twin.sh up` → миграции только TEST → offline checks → SHA/health/public URL. Обычный deploy не обновляет базу из PROD. `twin.sh refresh-db` разрушает текущую TEST-базу и требует отдельного разрешения.

PROD push → `deploy-prod-vds.yml` → существующий `deploy.sh` → build/backup/migrations/health → проверка checkout/container SHA и public URL. Любое изменение документов тоже запускает production release. Нужна прямая команда владельца, а в 09:00–19:00 МСК — отдельное подтверждение запуска в рабочее окно. Для этой уборки без такого разрешения готовится отдельный PR к `main`.

Личный SSH агенту не нужен: соединение/секреты использует Actions. Для чтения доступны GitHub API (ветки, workflow runs/jobs/logs), публичные HTTP-ответы и Timeweb Cloud GET `/api/v1/servers`, `/api/v1/servers/{id}`. Инфраструктурный статус не доказывает готовность тендера. Полные серверные ответы могут содержать секреты, их не печатать и не коммитить.

TEST `workflow_dispatch` с `inspect_tender_number` использует `scripts/inspect_tender_analysis.py`, пропускает deploy/платные вызовы и ставит `SET TRANSACTION READ ONLY`. Сначала читать workflow выбранной ветки. Режимы расширенной диагностики в `feature/m3-agent-simple` отличаются и не все являются read-only. Платный pilot, backup/rehearsal/maintenance не маскировать под чтение.
