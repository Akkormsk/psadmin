"""One real Django conversation turn, with a shared 50 RUB pilot ledger.

Uses the unmodified baseline agent, registry and executors. Only the HTTP
transport is observed; no model responses or tool calls are synthesized.
"""
import argparse
from datetime import date
from decimal import Decimal
import io
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import time
from unittest.mock import patch
from urllib.request import urlopen
from urllib.error import HTTPError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from experiments.m10.budget import Budget


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    temporary.replace(path)


def run(args):
    if not re.fullmatch(r"[A-D]-(?:mini|nano|54mini)(?:-[1-3])?", args.attempt):
        raise ValueError("Invalid attempt name")
    if args.start and args.confirm_event is not None:
        raise ValueError("A button cannot start a conversation")
    if subprocess.check_output(["git", "branch", "--show-current"], cwd=ROOT, text=True).strip() != "feature/m3-agent-simple":
        raise RuntimeError("M10 branch required")
    difference = subprocess.check_output(["git", "diff", "cc1cd264", "--", "*.py", ":!experiments/**"], cwd=ROOT)
    if difference:
        raise RuntimeError("Application Python differs from the fixed baseline")
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    ledger_path = directory / "ledger.json"
    ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {
        "approved_limit_rub": 50, "reservations": [], "attempt_calls": {}, "calls": [],
    }
    if ledger["approved_limit_rub"] != 50:
        raise RuntimeError("Only the approved 50 RUB pilot is supported")
    rates = json.loads((ROOT / "experiments/m10/rates.json").read_text())["rates"]
    budget = Budget(50, rates)
    budget.reservations = [Decimal(x) for x in ledger["reservations"]]
    budget.attempt_calls = ledger["attempt_calls"]
    database = directory / (args.attempt + ".sqlite3")
    state_path = directory / (args.attempt + ".json")
    if args.start:
        if database.exists() or state_path.exists():
            raise RuntimeError("Independent attempt must start from a fresh seed copy")
        shutil.copyfile(args.seed, database)
    elif not database.exists() or not state_path.exists():
        raise RuntimeError("No attempt to resume")
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        "model": args.model, "turns": [], "scenario": args.scenario,
        "source_application_sha": "cc1cd2649ae741e0f90b30fc7e94d4d9900fb76a",
    }
    if state["model"] != args.model or state["scenario"] != args.scenario:
        raise RuntimeError("Cannot change model or scenario inside an attempt")
    os.environ["DB_ENGINE"] = "sqlite"
    os.environ["DJANGO_SETTINGS_MODULE"] = "mysite.settings"
    os.environ["TIMEWEB_AI_BASE_URL"] = "https://api.timeweb.ai/v1"
    os.environ["TIMEWEB_AI_ASSISTANT_MODEL"] = args.model
    from django.conf import settings
    settings.DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": str(database)}}
    settings.MEDIA_ROOT = str(directory / "media")
    import django
    django.setup()
    from django.contrib.auth import get_user_model
    from django.db import connection
    from tenders.assistant_conversations import create_conversation, respond, serialize_messages, serialize_debug_journal
    from tenders.models import OwnerInteraction, TenderEstimate
    from cash.models import CashTransaction
    user = get_user_model().objects.get(username="m10")
    if args.start:
        conversation = create_conversation(user)
        state["conversation_id"] = conversation.pk
    else:
        conversation = OwnerInteraction.objects.get(pk=state["conversation_id"])

    def persist():
        ledger["reservations"] = [str(x) for x in budget.reservations]
        ledger["attempt_calls"] = budget.attempt_calls
        ledger["charged_or_reserved_rub"] = str(budget.charged_or_reserved)
        save(ledger_path, ledger)

    def observed_transport(request, **kwargs):
        if request.full_url != "https://api.timeweb.ai/v1/chat/completions":
            raise RuntimeError("Unexpected Gateway URL")
        body = json.loads(request.data)
        index = budget.reserve(args.attempt, body)
        entry = {"attempt": args.attempt, "model": args.model, "request": body,
            "reservation_index": index, "usage": None, "http_status": None}
        ledger["calls"].append(entry)
        persist()  # Reserve before any paid network operation.
        started = time.monotonic()
        try:
            with urlopen(request, **kwargs) as response:
                raw = response.read()
                entry["http_status"] = response.status
                entry["fallback_header"] = response.headers.get("x-timeweb-attempted-fallbacks")
                payload = json.loads(raw)
                entry["response"] = payload
                entry["usage"] = payload.get("usage")
                actual = payload.get("model", "")
                entry["actual_model"] = actual
                requested = args.model.split("/", 1)[-1]
                if not actual or not (actual == args.model or actual == requested or actual.startswith(requested + "-")):
                    raise RuntimeError("Unverified actual response model; retain reservation and stop")
                budget.settle(index, args.model, entry["usage"])
                if entry["fallback_header"] not in (None, "0"):
                    raise RuntimeError("Gateway fallback attempted; exclude from clean A/B and stop")
                return io.BytesIO(raw)
        except HTTPError as exc:
            entry["http_status"] = exc.code
            entry["http_error_body"] = exc.read().decode(errors="replace")[:4000]
            raise
        except Exception as exc:
            entry["error"] = type(exc).__name__ + ": " + str(exc)
            raise
        finally:
            entry["seconds"] = time.monotonic() - started
            persist()

    if args.message is None and args.confirm_event is None:
        args.message = json.loads((ROOT / "experiments/m10/scenarios.json").read_text())["primary"][args.scenario]["prompt"]
    started = time.monotonic()
    turn = {"input": args.message, "error": None}
    try:
        with patch("tenders.assistant_agent.urlopen", observed_transport), patch(
                "django.utils.timezone.localdate", return_value=date(2026, 10, 23)):
            if args.confirm_event is not None:
                from django.test import RequestFactory
                from tenders.assistant_ui import conversation_action_confirm
                request = RequestFactory().post("/isolated-confirmation", {"accept": "yes"})
                request.user = user
                response = conversation_action_confirm(request, conversation.pk, args.confirm_event)
                turn["button_event"] = args.confirm_event
                turn["http_status"] = response.status_code
                turn["http_body"] = json.loads(response.content)
            else:
                respond(conversation, user, args.message)
    except Exception as exc:
        turn["error"] = type(exc).__name__ + ": " + str(exc)
    finally:
        turn["seconds"] = time.monotonic() - started
        conversation.refresh_from_db()
        state["turns"].append(turn)
        state["messages"] = serialize_messages(conversation)
        state["context"] = conversation.context
        state["debug"] = serialize_debug_journal(conversation)
        state["cash_count"] = CashTransaction.objects.count()
        state["estimate_count"] = TenderEstimate.objects.count()
        state["estimates"] = list(TenderEstimate.objects.values("id", "tender_id", "tender_number", "name"))
        save(state_path, state)
        persist()
        connection.close()
    print(json.dumps({"attempt": args.attempt, "turn": {k:v for k,v in turn.items() if k != "http_body"}, "last_message": state["messages"][-1] if state["messages"] else None,
        "cash_count": state["cash_count"], "charged_or_reserved_rub": str(budget.charged_or_reserved),
        "call_count": budget.attempt_calls.get(args.attempt, 0)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--seed", type=Path, required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--model", required=True, choices=["openai/gpt-4.1-mini", "openai/gpt-5.4-nano", "openai/gpt-5.4-mini"])
    parser.add_argument("--scenario", default="A", choices=["A", "B", "C", "D"])
    parser.add_argument("--message")
    parser.add_argument("--confirm-event", type=int)
    parser.add_argument("--start", action="store_true")
    run(parser.parse_args())
