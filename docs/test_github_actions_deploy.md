# Тестовый VDS через GitHub Actions

Тест выкладывается только вручную workflow **Deploy test VDS** из вкладки
Actions. Укажите ветку и, при необходимости, точный commit SHA. `main` и
`master` workflow отвергает. Он разворачивает только `/home/deploy/psadmin-test`
и запускает `twin.sh up`; production, `refresh-db` и production Compose в нём
не используются.

Перед выкладкой агент обязан прогнать локальные тесты. Workflow принимает только
commit, который является предком указанной ветки, сериализует именно его и
сохраняет предыдущий тестовый release в `/home/deploy/psadmin-test-rollbacks`.
Запуски сериализованы: два чата не смогут одновременно менять тест.

Для работы требуются уже настроенные repository secrets:

- `PSADMIN_TEST_SSH_HOST`;
- `PSADMIN_TEST_SSH_PRIVATE_KEY`;
- `PSADMIN_TEST_SSH_KNOWN_HOSTS`.

Ключи и значения secrets не копируются в чаты, ветки или репозиторий. Если
workflow завершается успешно, тест проверен его healthcheck и публичным URL.
