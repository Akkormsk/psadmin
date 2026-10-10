# Ручной deploy тестового VDS через GitHub Actions

Workflow `.github/workflows/deploy-test-vds.yml` обновляет **только**
`https://test.admin.psodin.ru`. Он запускается вручную из ветки
`codex/calculation-v2-foundation`, собирает архив точного commit SHA и по SSH
передаёт его на VDS. В workflow нет `refresh-db`, команд production Compose,
путей production-кода или production-БД.

На VDS workflow заменяет только `/home/deploy/psadmin-test`, сохраняет прежний
каталог в `/home/deploy/psadmin-test-rollbacks/` и запускает существующий
`deploy/vds/twin.sh up`. Перед заменой он считывает прежний SHA из
`/home/deploy/psadmin-test/.twin-version`; после неё записывает новый SHA и
предыдущий SHA в такой же файл. Поэтому в логах workflow всегда есть commit,
на который можно вернуться.

## Однократная подготовка VDS

Это выполняет человек с доступом к серверу, не GitHub Actions и не workflow.

1. Убедиться, что существует `/home/deploy/psadmin-test.env` с отдельными
   тестовыми настройками, отдельным `DJANGO_SECRET_KEY` и выключенными
   `*_ENABLED=0`.
2. Убедиться, что текущий тестовый release содержит
   `/home/deploy/psadmin-test/.twin-version` с полным 40-символьным SHA
   текущего test release. Если файла нет, записать SHA известного текущего
   release до первого запуска workflow.
3. Создать отдельную SSH-ключевую пару только для GitHub Actions. В
   `/home/deploy/.ssh/authorized_keys` добавить **только публичную** часть.
   Закрытую часть никогда не копировать на VDS и не отправлять в чат.
4. Получить у администратора VDS проверенную строку host key в формате
   `test-host ssh-ed25519 AAAA...` и её fingerprint, сверив fingerprint через
   доверенный канал. Не принимать ключ, полученный только через `ssh-keyscan`.

## GitHub Secrets

В репозитории открыть **Settings → Secrets and variables → Actions → New
repository secret** и создать:

| Secret | Что вставить |
| --- | --- |
| `PSADMIN_TEST_SSH_HOST` | Адрес VDS для SSH: предпочтительно его IP-адрес или отдельное имя сервера. |
| `PSADMIN_TEST_SSH_PRIVATE_KEY` | Полное содержимое закрытого ключа отдельной пары для Actions, включая строки `BEGIN` и `END`. |
| `PSADMIN_TEST_SSH_KNOWN_HOSTS` | Проверенная строка host key для того же имени/IP, который указан в `PSADMIN_TEST_SSH_HOST`. |

Не создавайте secrets с паролем VDS, паролем PostgreSQL, содержимым
`psadmin-test.env` или production-данными. Workflow использует пользователя
`deploy` и SSH key, а не пароль.

## Запуск и откат

1. Открыть GitHub → **Actions** → **Deploy test VDS** → **Run workflow**.
2. В списке веток выбрать строго `codex/calculation-v2-foundation`.
3. Для обычного deploy оставить `target_sha` пустым. Для повторного deploy или
   отката вставить полный SHA из лога предыдущего успешного запуска в
   `target_sha`.
4. Дождаться зелёного workflow: он проверяет Docker health `test-web` и URL
   `https://test.admin.psodin.ru`.

Откат не запускает `refresh-db`: он разворачивает прежний commit как новый
ручной test deploy. Если старый код несовместим со схемой тестовой БД после
миграции, остановитесь и привлеките разработчика: восстановление возможно
только из отдельного backup **test-db**, а не из production-БД.
