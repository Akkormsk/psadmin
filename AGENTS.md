# Repository Guidelines

## Project Structure & Module Organization

PSADMIN uses Django 5.2. Configuration and URL routing live in `mysite/`; `manage.py` is the command entry point. `tender_selection/` handles tender discovery and intake; `tenders/` contains calculations, document processing, and assistant capabilities. Other Django apps own their domains. Templates live in `templates/`, assets in `static/`, and schema changes in each app’s `migrations/`. Operational tools are in `scripts/` and `deploy/vds/`.

## Build, Test, and Development Commands

Install dependencies with `python -m pip install -r requirements.txt` in a virtual environment. For an isolated local database:

- `python manage.py migrate` applies migrations.
- `python manage.py runserver` starts development Django.
- `python manage.py check` checks configuration.
- `python manage.py makemigrations --check --dry-run` detects schema drift.
- `python manage.py test tender_selection.test_secondary_intake` runs focused tests.
- `python manage.py test` runs the complete suite.

Local SQLite is the default; use isolated PostgreSQL 16 for migration and concurrency checks. Never point tests at production.

## Coding Style & Architecture

Use four-space Python indentation, `snake_case` functions, and existing Django conventions. No repository-wide formatter or linter is configured. Scope changes narrowly; inspect definitions and imports with `rg` before reading file ranges. Prepare data structures before dependent features. Preserve V1 until V2 acceptance; reuse existing models and executors. Keep calculations, validation, and permissions in backend services, not LLM prompts. Use existing CSS tokens. Give model fields meaningful labels.

## Testing Guidelines

Tests use Django/unittest and `test_*.py` modules. Reproduce bugs, run targeted tests, then relevant regression; run the full suite for significant/shared changes. Mock external services and paid AI. Verify data preservation, permissions, and transaction boundaries. Report existing failures separately; do not invent coverage targets or rerun unchanged tests without reason.

## Commits & Pull Requests

Use short imperative messages, following history: “Respect Gosplan search limits.” Fetch current refs and work in an isolated feature branch. PRs describe the problem, resulting behavior, validation, and limitations; include screenshots for UI changes. Preserve unrelated work.

## Security, Documentation & Releases

Follow the [mandatory contributor policy](docs/assistant_protocol.md#mandatory-contributor-policy), including approval, data protection, assistant contracts, and release rules. `docs/assistant_protocol.md` is the sole project protocol: read relevant sections and update confirmed behavior there; create no parallel memory documents. Keep secrets, production dumps, and customer documents out of Git. Do not change PROD/main without explicit authorization. Authorized TEST pushes deploy automatically through existing Actions: verify actual SHA and health before claiming success. Never bypass deployment safeguards.
