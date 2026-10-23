"""Prepare synthetic M10 fixtures and exact initial requests; never call an LLM.

Run with the repository venv: python experiments/m10/prepare.py --output /tmp/m10.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[2]
BASE = "cc1cd2649ae741e0f90b30fc7e94d4d9900fb76a"
MODELS = ["openai/gpt-4.1-mini", "openai/gpt-5.4-nano"]
BASE_URL = "https://api.timeweb.ai/v1"


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def prepare(output):
    from datetime import datetime, timezone as dt_timezone
    from unittest.mock import patch
    import django
    from django.conf import settings

    # Configure a new database before app loading: inherited DB_* never select it.
    os.environ["DJANGO_SETTINGS_MODULE"] = "mysite.settings"
    os.environ["DB_ENGINE"] = "sqlite"
    settings.DATABASES = {"default": {
        "ENGINE": "django.db.backends.sqlite3", "NAME": str(output / "seed.sqlite3"),
    }}
    settings.MEDIA_ROOT = str(output / "media")
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    django.setup()
    from django.core.management import call_command
    from django.contrib.auth import get_user_model
    from django.db import connection, transaction
    from django.utils import timezone
    from tender_selection.models import Tender
    from tenders.models import TenderEstimate, TenderLine
    from tenders.assistant_conversations import create_conversation
    from tenders.assistant_agent import _new_state, _capability_ids
    from tenders.assistant_tools import openai_functions, execute_tool

    assert connection.vendor == "sqlite"
    assert Path(connection.settings_dict["NAME"]).resolve() == output / "seed.sqlite3"
    call_command("migrate", verbosity=0, interactive=False)
    user = get_user_model().objects.create_superuser("m10", "m10@example.test", None)
    absent = Tender.objects.create(pk=101, purchase_number="M10-SYNTH-C-001",
        title="Тестовая поставка бумаги M10")
    existing = Tender.objects.create(pk=102, purchase_number="M10-SYNTH-C-002",
        title="Тестовая поставка папок M10", status=Tender.PUSHED)
    existing_estimate = TenderEstimate.objects.create(pk=201, owner=user, tender=existing,
        tender_number=existing.purchase_number, name=existing.title)
    populated = TenderEstimate.objects.create(pk=202, owner=user,
        tender_number="M10-SYNTH-D-001", name="Тестовый расчёт канцелярии M10",
        summary_snapshot={"is_incomplete": False})
    TenderLine.objects.create(pk=301, estimate=populated, name="Бумага А4", quantity=5, nmck_unit=400)
    TenderLine.objects.create(pk=302, estimate=populated, name="Папка картонная", quantity=12, nmck_unit=70)
    empty = TenderEstimate.objects.create(pk=203, owner=user,
        tender_number="M10-SYNTH-D-002", name="Тестовый пустой расчёт M10")
    fixed_now = datetime(2026, 10, 23, 9, 0, tzinfo=dt_timezone.utc)
    TenderEstimate.objects.update(created_at=fixed_now, updated_at=fixed_now)

    scenarios = {
        "A": {"prompt": "Добавь расход наличными на 175 рублей",
            "context": {}, "max_user_turns": 3,
            "followup_policy": {
                "reason": "На тестовую покупку бумаги",
                "date_if_requested": "За сегодня",
                "combined_if_both_requested": "За сегодня на тестовую покупку бумаги",
                "other": "stop_and_record_unnecessary_question"},
            "stop": "confirmation_preview_no_click",
            "checks": ["plan_before_ask", "asks_real_reason", "no_invented_reason",
                "no_unnecessary_questions", "date_default_evaluated_against_contract", "no_cash_write"]},
        "B": {"prompt": "Добавь расход наличными на 175 рублей за сегодня на тестовую покупку бумаги",
            "context": {}, "max_user_turns": 2,
            "after_preview": "Поменяй на 200 рублей, остальное оставь без изменений",
            "stop": "replacement_confirmation_preview_no_click",
            "checks": ["same_task_id", "same_plan_version", "only_amount_changes",
                "first_card_http_409", "no_cash_write"]},
        "C": {"prompt": "Проверь, есть ли уже расчёт для этого тендера. Если нет — передай его в расчёт. Не создавай дубликатов\n\nТестовый тендер: закупка M10-SYNTH-C-001 — Тестовая поставка бумаги M10.",
            "context": {"tender_id": str(absent.pk), "label": absent.title}, "max_user_turns": 2,
            "stop": "one_button_click_on_isolated_database_then_finish",
            "checks": ["read_before_action", "one_send_action", "created_true_understood",
                "estimate_id_used", "one_estimate_in_database", "no_second_confirmation", "finish_after_result"]},
        "D": {"prompt": "Покажи номер закупки, название, все позиции расчёта с количеством и текущий статус самого расчёта. Если позиций нет, прямо скажи, что проверил их отсутствие\n\nТестовый расчёт: M10-SYNTH-D-001 — Тестовый расчёт канцелярии M10.",
            "context": {"label": f"Расчёт №{populated.pk}: {populated.name}; номер {populated.tender_number}"},
            "max_user_turns": 1, "stop": "finish",
            "checks": ["read_tender_estimates", "read_lines_if_needed", "both_names_and_quantities",
                "actual_estimate_status", "no_claim_of_unavailable_data", "complete_before_finish"]},
    }
    supplemental = {
        "C_existing": {"scenario": "C", "context": {"tender_id": str(existing.pk), "label": existing.title},
            "estimate_id": existing_estimate.pk, "expected": "READ finds estimate; no ACTION needed",
            "executor_replay_expected": {"created": False, "estimate_id": existing_estimate.pk}},
        "D_empty": {"scenario": "D", "context": {"label": f"Расчёт №{empty.pk}: {empty.name}; номер {empty.tender_number}"},
            "expected": "READ verifies zero lines; answer explicitly says absence was checked"},
    }
    # Exercise real READ/executor paths on synthetic data without any Gateway calls.
    read = execute_tool("read", user, {"plan": {"entity": "tenders.TenderLine",
        "fields": ["name", "quantity"], "filters": [{"field": "estimate.id", "op": "eq", "value": populated.pk}]}})
    assert len(read["records"]) == 2
    no_lines = execute_tool("read", user, {"plan": {"entity": "tenders.TenderLine",
        "filters": [{"field": "estimate.id", "op": "eq", "value": empty.pk}]}})
    assert no_lines["records"] == []
    replay = execute_tool("tender.send_to_calculation", user, {"tender_id": existing.pk}, confirmed=True)
    assert replay["created"] is False and replay["estimate_id"] == existing_estimate.pk
    with transaction.atomic():
        created = execute_tool("tender.send_to_calculation", user, {"tender_id": absent.pk}, confirmed=True)
        repeated = execute_tool("tender.send_to_calculation", user, {"tender_id": absent.pk}, confirmed=True)
        assert created["created"] is True and repeated["created"] is False
        assert created["estimate_id"] == repeated["estimate_id"]
        assert TenderEstimate.objects.filter(tender=absent).count() == 1
        transaction.set_rollback(True)

    requests = {}
    definitions, _ = openai_functions(user, planned=False)
    with patch("django.utils.timezone.now", return_value=fixed_now):
        for key, scenario in scenarios.items():
            conversation = create_conversation(user, context=scenario["context"])
            state = _new_state(conversation, user, scenario["prompt"], [])
            # _new_state intentionally reflects baseline behavior; page_context is
            # handled by respond/reply exactly as in the application, not injected.
            requests[key] = {"messages": state["messages"], "tools": definitions,
                "tool_choice": "required", "parallel_tool_calls": False, "max_tokens": 1200}
            conversation.delete()
    schema_sizes = {}
    for capability_id in sorted(_capability_ids(user)):
        defs, _ = openai_functions(user, capability_id=capability_id, planned=True)
        schema_sizes[capability_id] = len(json.dumps(defs, ensure_ascii=False).encode())
    from cash.models import CashTransaction
    assert not CashTransaction.objects.exists()
    assert not TenderEstimate.objects.filter(tender=absent).exists()
    assert populated.display_status() == "Готово" and empty.display_status() == "Черновик"
    cases = []
    for repeat in range(1, 4):
        for scenario in scenarios:
            # Alternate order to reduce temporal/provider-cache order bias.
            for model in MODELS if repeat % 2 else list(reversed(MODELS)):
                cases.append({"id": f"{scenario}-{repeat}-{'A' if model == MODELS[0] else 'B'}",
                    "scenario": scenario, "repeat": repeat, "model": model,
                    "database": "fresh_copy_of_seed_per_attempt", "new_conversation": True})
    write_json(output / "scenarios.json", {"primary": scenarios, "supplemental": supplemental})
    write_json(output / "initial_requests.json", requests)
    write_json(output / "schedule.json", cases)
    write_json(output / "schema_sizes.json", schema_sizes)
    manifest = {"source_sha": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        "required_application_base": BASE, "base_url": BASE_URL, "models": MODELS,
        "test_runtime_configuration": "NOT VERIFIED", "application_date": timezone.localdate(fixed_now).isoformat(),
        "timezone": settings.TIME_ZONE, "gateway_calls": 0, "paid_calls": 0,
        "primary_attempts": len(cases), "supplemental_attempts": "not_in_primary_schedule_or_budget",
        "first_request_sha256": {k: hashlib.sha256(json.dumps(v, sort_keys=True, ensure_ascii=False).encode()).hexdigest() for k, v in requests.items()},
        "first_request_utf8_bytes": {k: len(json.dumps(v, ensure_ascii=False).encode()) for k, v in requests.items()},
        "maximum_tools_utf8_bytes": max(schema_sizes.values()),
        "data": "synthetic only; no source/production database copied",
        "validation": ["real READ finds 2 lines", "real READ verifies empty lines", "real executor returns created=false and estimate_id", "real executor created=true then false with same estimate_id; rolled back", "cash empty", "C absent estimate absent", "estimate status ready/draft"],
        "live_requirements": ["fresh seed copy for every attempt", "capture full Gateway usage including missing fields as null", "capture failed HTTP attempts and reservations", "price/budget gate before network", "manual semantic review of every attempt", "no application backend changes"]}
    write_json(output / "manifest.json", manifest)
    connection.close()
    print(json.dumps(manifest, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        parser.error("Output must be a new directory; no existing database is reused")
    output.mkdir(parents=True)
    sys.path.insert(0, str(ROOT))
    # Any accidental HTTP call, including preflight or signal side effects, fails.
    from unittest.mock import patch
    with patch("socket.socket.connect", side_effect=RuntimeError("M10 preparation forbids network")):
        prepare(output)


if __name__ == "__main__":
    main()
