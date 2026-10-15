"""Backend production-knowledge actions shared by UI and assistant."""
import copy
from decimal import Decimal, InvalidOperation
from django.db import transaction

from .cascade_settings import text_search_settings
from .models import CascadeConfigVersion, CascadeLabPreset, CatalogMatchDecision, Lesson, ProductionTrainingExample, ProductionTrainingSession, ProductionTrainingTurn, ProductionType, Proposal, Counterparty, ProcessDefinition, RequirementSkipRule, TenderKnowledgeSource
from .proposals import apply_proposal
from .services import build_training_hypothesis, refresh_training_example_embedding


def _require_admin(user):
    if not getattr(user, "is_superuser", False):
        raise ValueError("Действие доступно только администратору")


def save_cascade_lab_preset(user, name, settings):
    """Save the same named Cascade Lab preset as the settings button."""
    _require_admin(user)
    name = str(name or "").strip()[:200]
    if not name:
        raise ValueError("Введите название набора настроек.")
    preset, created = CascadeLabPreset.objects.update_or_create(
        created_by=user, name=name, defaults={"settings": text_search_settings(settings)},
    )
    return {"id": preset.pk, "name": preset.name, "settings": preset.settings, "created": created}


@transaction.atomic
def activate_cascade_lab_settings(user, name, settings):
    """Activate one Cascade configuration with the exact UI side effects."""
    _require_admin(user)
    name = str(name or "Текущие настройки").strip()[:200]
    CascadeConfigVersion.objects.filter(is_active=True).update(is_active=False)
    version = CascadeConfigVersion.objects.create(
        name=name, settings=text_search_settings(settings), created_by=user, is_active=True,
    )
    return {"id": version.pk, "name": version.name, "created_at": version.created_at.isoformat()}


def restore_requirement_to_route(user, label):
    """Undo the same learned requirement exclusion as the route UI button."""
    _require_admin(user)
    from .services import _normalized_text

    label_normalized = _normalized_text(str(label or ""))[:200]
    if not label_normalized:
        raise ValueError("Не удалось определить строку ТЗ.")
    removed = RequirementSkipRule.objects.filter(label_normalized=label_normalized).update(is_active=False)
    return {"dropped": bool(removed)}


@transaction.atomic
def confirm_proposed_stage(user, session_id, proposal_id):
    """Create one approved route stage, exactly as the route panel does."""
    _require_admin(user)
    session = ProductionTrainingSession.objects.select_for_update().filter(
        pk=session_id, created_by=user, is_confirmed=False,
    ).first()
    if session is None:
        raise ValueError("Этап не найден или маршрут устарел. Обновите расчёт.")
    hypothesis = session.current_hypothesis if isinstance(session.current_hypothesis, dict) else {}
    proposals = hypothesis.get("stage_proposals", [])
    proposed = next((item for item in proposals if isinstance(item, dict) and item.get("id") == proposal_id), None)
    if not isinstance(proposed, dict):
        raise ValueError("Этап не найден или маршрут устарел. Обновите расчёт.")
    role = proposed.get("role")
    stage_payload = {
        "name": proposed.get("name"), "description": proposed.get("description", ""),
        "supplies_input": role == "supply", "performs_production": role == "production",
        "terminal_mode": "always" if role == "completion" else "sometimes",
    }
    proposal = Proposal.objects.create(
        type=Proposal.TYPE_CREATE_STAGE, payload=stage_payload,
        summary=f"Новый этап «{stage_payload['name']}» — предложен из маршрута", created_by=user,
    )
    apply_proposal(proposal, user)
    hypothesis["stage_proposals"] = [item for item in proposals if item.get("id") != proposal_id]
    session.current_hypothesis = hypothesis
    session.save(update_fields=["current_hypothesis", "updated_at"])
    return {**hypothesis, "session_id": session.pk, "route_refresh_required": True}


@transaction.atomic
def reopen_confirmed_route(user, session_id):
    """Return one confirmed route to an editable draft, preserving its history."""
    _require_admin(user)
    accepted = ProductionTrainingSession.objects.select_for_update().filter(
        pk=session_id, created_by=user, is_confirmed=True,
    ).first()
    if accepted is None:
        raise ValueError("Принятый расчёт не найден.")
    if accepted.confirmed_example_id:
        ProductionTrainingExample.objects.filter(pk=accepted.confirmed_example_id).update(is_active=False)
    Lesson.objects.filter(session=accepted).update(is_active=False)
    hypothesis = copy.deepcopy(accepted.current_hypothesis if isinstance(accepted.current_hypothesis, dict) else {})
    hypothesis.update({"is_confirmed": False, "revision_parent_session_id": accepted.pk})
    draft = ProductionTrainingSession.objects.create(
        created_by=user, position_name=accepted.position_name,
        requirements=copy.deepcopy(accepted.requirements), current_hypothesis=hypothesis,
    )
    hypothesis["session_id"] = draft.pk
    draft.current_hypothesis = hypothesis
    draft.save(update_fields=["current_hypothesis", "updated_at"])
    return hypothesis


@transaction.atomic
def confirm_route_training(user, session_id):
    """Confirm one finished route and preserve its learned production knowledge."""
    _require_admin(user)
    from .routes import confirm_route
    from .services import _normalized_text, learn_lessons_from_session

    session = ProductionTrainingSession.objects.select_for_update().filter(
        pk=session_id, created_by=user, is_confirmed=False,
    ).first()
    if session is None:
        raise ValueError("Не удалось сохранить учебный пример.")
    hypothesis = session.current_hypothesis if isinstance(session.current_hypothesis, dict) else {}
    if hypothesis.get("status") in {"processing", "error"}:
        raise ValueError("Дождитесь успешного построения маршрута.")
    saved = 0
    if hypothesis.get("catalog_steps"):
        for step_id, state in hypothesis["catalog_steps"].items():
            instructions = [entry for entry in hypothesis.get("session_instructions", []) if entry.get("step_id", step_id) == step_id]
            saved += learn_lessons_from_session({**state, "session_instructions": instructions}, session, user)
    else:
        saved = learn_lessons_from_session(hypothesis, session, user)
    saved += confirm_route(hypothesis, session, user)
    skipped = 0
    for row in hypothesis.get("requirement_selection", []):
        if not isinstance(row, dict) or row.get("selected") is not False or row.get("source_type") == "manager_clarification":
            continue
        label = str(row.get("label", "")).strip()[:200]
        normalized = _normalized_text(label)
        if not normalized:
            continue
        _, created = RequirementSkipRule.objects.get_or_create(
            label_normalized=normalized, defaults={"label": label, "example_value": str(row.get("value", ""))[:500], "created_by": user, "is_active": True},
        )
        if not created:
            RequirementSkipRule.objects.filter(label_normalized=normalized, is_active=False).update(is_active=True)
        skipped += int(created)
    session.is_confirmed = True
    session.current_hypothesis = {**hypothesis, "is_confirmed": True}
    session.save(update_fields=["is_confirmed", "current_hypothesis", "updated_at"])
    parts = ["Маршрут принят и сохранён." if session.confirmed_example_id else "Расчёт принят."]
    parts.append(f"Новых уроков: {saved}." if saved else "Новых уроков нет.")
    if skipped:
        parts.append(f"Строк ТЗ вынесено из подбора навсегда: {skipped}.")
    return {"message": " ".join(parts), "lessons_saved": saved, "rules_saved": saved, "requirement_skips_saved": skipped}




def select_route_catalog_product(user, payload):
    """Persist one explicit catalog choice using the current route engine."""
    _require_admin(user)
    from .services import apply_catalog_candidate

    if not isinstance(payload, dict):
        raise ValueError("Не удалось выбрать товар поставщика. Обновите гипотезу.")
    session = ProductionTrainingSession.objects.filter(
        pk=payload.get("session_id"), created_by=user, is_confirmed=False,
    ).first()
    line = payload.get("line") if isinstance(payload.get("line"), dict) else {}
    product_id = str(payload.get("product_id", "")).strip()[:100]
    if session is None or not product_id or not str(line.get("name", "")).strip():
        raise ValueError("Не удалось выбрать товар поставщика. Обновите гипотезу.")
    prior = session.current_hypothesis if isinstance(session.current_hypothesis, dict) else {}
    if prior.get("route", {}).get("schema_version") == 1:
        from .routes import catalog_step_state, merge_catalog_step
        step, state = catalog_step_state(prior, str(payload.get("step_id", "")))
        hypothesis = merge_catalog_step(prior, apply_catalog_candidate({**prior, **state}, line, product_id), line, step["id"])
    else:
        hypothesis = apply_catalog_candidate(prior, line, product_id)
    hypothesis["session_id"] = session.pk
    session.current_hypothesis = hypothesis
    session.save(update_fields=["current_hypothesis", "updated_at"])
    selection = hypothesis.get("catalog_selection", {})
    CatalogMatchDecision.objects.create(
        session=session, product=None, supplier_code=str(selection.get("supplier_code") or "oasis")[:50],
        product_external_id=str(selection.get("external_id") or selection.get("id") or "")[:100],
        product_article=str(selection.get("article") or "")[:120],
        product_snapshot={key: selection.get(key) for key in ("external_id", "article", "name", "price", "stock", "url", "category", "matches", "supplier_code", "supplier_name", "supplier_site")},
        decision="selected", reason_codes=["backend_exact_match" if selection.get("fit") == "exact" else "admin_override_mismatch", "selected_by_admin"],
        requirement_signature={"name": str(line.get("name", ""))[:500], "quantity": str(line.get("quantity", ""))[:50], "requirements": line.get("requirements") if isinstance(line.get("requirements"), dict) else {}}, created_by=user,
    )
    supplier_name = str(selection.get("supplier_name") or selection.get("supplier_code") or "поставщика")
    change = f"Выбран товар поставщика {supplier_name}: {selection.get('name', 'товар')} · арт. {selection.get('article', '')}".strip()
    ProductionTrainingTurn.objects.create(session=session, feedback=change, understood_changes=[change], hypothesis=hypothesis)
    return hypothesis


def draft_production_counterparty(user, text="", url="", upload=None):
    """Create the same reviewable counterparty draft as the production-base form."""
    _require_admin(user)
    from .services import TenderAIError, extract_calculation_source, parse_counterparty_draft

    if upload is not None:
        from pathlib import Path
        if Path(upload.name).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("Пока принимаем скриншот (PNG/JPG), текст или ссылку — без PDF/Excel.")
        if upload.size > 20 * 1024 * 1024:
            raise ValueError("Файл больше 20 МБ.")
    try:
        source = extract_calculation_source(source_text=text, source_url=url, upload=upload)
        active_stages = list(ProcessDefinition.objects.filter(is_active=True).values("id", "name"))
        draft = parse_counterparty_draft(source["content"], [stage["name"] for stage in active_stages])
    except TenderAIError as exc:
        raise ValueError(str(exc)) from exc
    raw_file = raw_file_name = raw_file_content_type = None
    if upload is not None:
        upload.seek(0)
        raw_file, raw_file_name, raw_file_content_type = upload.read(), upload.name[:255], (upload.content_type or "")[:100]
    knowledge_source = TenderKnowledgeSource.objects.create(
        title=draft["name"][:300], source_type=source["source_type"], url=source["url"],
        content_summary=source["content"][:4000], structured_data=source["structured_data"], raw_file=raw_file,
        raw_file_name=raw_file_name or "", raw_file_content_type=raw_file_content_type or "", created_by=user,
    )
    return {"source_id": knowledge_source.pk, "name": draft["name"], "notes": draft["notes"],
            "suggested_stage_names": draft["suggested_stage_names"], "active_stages": active_stages}


@transaction.atomic
def add_route_calculation_sources(user, payload, uploads=None):
    """Attach verified cost sources and rerun the existing route calculation."""
    _require_admin(user)
    from .services import TenderAIError, apply_verified_source_quote, build_training_hypothesis, extract_calculation_source

    if not isinstance(payload, dict):
        raise ValueError("Не удалось определить статью расчёта или источник.")
    session = ProductionTrainingSession.objects.select_for_update().filter(
        pk=payload.get("session_id"), created_by=user, is_confirmed=False,
    ).first()
    line = payload.get("line") if isinstance(payload.get("line"), dict) else {}
    hypothesis = session.current_hypothesis if session and isinstance(session.current_hypothesis, dict) else {}
    costs = hypothesis.get("costs") if isinstance(hypothesis.get("costs"), list) else []
    sources = payload.get("sources") if isinstance(payload.get("sources"), list) else [payload]
    if session is None or not str(line.get("name", "")).strip() or not sources or len(sources) > 6:
        raise ValueError("Не удалось определить статью расчёта или источник.")
    uploads = uploads or {}
    feedback = str(payload.get("feedback", "")).strip()[:6000]
    prepared = []
    for index, raw in enumerate(sources):
        if not isinstance(raw, dict):
            raise ValueError("Некорректный источник расчёта.")
        try:
            cost_index = int(raw["cost_index"]) if raw.get("cost_index") not in (None, "") else None
        except (TypeError, ValueError):
            raise ValueError("Некорректная статья расчёта.")
        if cost_index is not None and not 0 <= cost_index < len(costs):
            raise ValueError("Некорректная статья расчёта.")
        upload = uploads.get(f"file_{index}" if isinstance(payload.get("sources"), list) else "file")
        if upload is not None and upload.size > 10 * 1024 * 1024:
            raise ValueError(f"Файл источника № {index + 1} больше 10 МБ.")
        target = costs[cost_index] if cost_index is not None else None
        target_name = target.get("name", "") if target else ""
        source = TenderKnowledgeSource.objects.filter(pk=raw.get("source_id"), is_active=True).first() if raw.get("source_id") else None
        try:
            if source is not None and source.url:
                extracted = extract_calculation_source(source_url=source.url, selection_context={"line": line, "feedback": feedback, "target_name": target_name})
                source.content_summary = extracted["content"]
                source.structured_data = {**(source.structured_data if isinstance(source.structured_data, dict) else {}), **(extracted.get("structured_data") or {})}
                source.save(update_fields=["content_summary", "structured_data", "updated_at"])
            elif source is not None:
                extracted = {"content": source.content_summary, "source_type": source.source_type, "url": source.url, "structured_data": source.structured_data if isinstance(source.structured_data, dict) else {}}
            else:
                extracted = extract_calculation_source(source_text=str(raw.get("source_text", "")), source_url=str(raw.get("source_url", "")), upload=upload, selection_context={"line": line, "feedback": feedback, "target_name": target_name})
                source = TenderKnowledgeSource.objects.create(title=(str(raw.get("title", "")).strip() or str(raw.get("supplier_name", "")).strip() or target_name or f"Источник для {line['name']}")[:300], supplier_name=str(raw.get("supplier_name", "")).strip()[:200], source_type=extracted["source_type"], url=extracted["url"], content_summary=extracted["content"], structured_data={"scope": "cost" if target else "position", "position_name": str(line["name"])[:300], "cost_name": target_name, **(extracted.get("structured_data") or {})}, created_by=user, is_active=False)
        except TenderAIError as exc:
            raise ValueError(str(exc)) from exc
        prepared.append((source, extracted, cost_index, target))
    instructions = [feedback] if feedback else []
    for number, (source, extracted, _index, target) in enumerate(prepared, 1):
        purpose = f"Для статьи «{target.get('name', 'расход')}» пересчитай цену по источнику." if target else "Определи подходящую статью или процесс для этого источника."
        instructions.append(f"ИСТОЧНИК № {number}. {purpose}\nДАННЫЕ ИСТОЧНИКА:\n{extracted['content'][:12000]}")
    updated = build_training_hypothesis(line, current=hypothesis, feedback="\n\n".join(instructions))
    for source, extracted, _index, target in prepared:
        updated = apply_verified_source_quote(updated, line, (extracted.get("structured_data") or {}).get("price_quote"), str(source), source.url, target)
    updated["session_id"] = session.pk
    previous = hypothesis.get("sources") if isinstance(hypothesis.get("sources"), list) else []
    ids = {source.pk for source, *_ in prepared}
    cards = [{"id": source.pk, "title": source.title, "supplier_name": source.supplier_name, "source_type": source.source_type, "url": source.url, "scope": "cost" if target else "position", "cost_name": target.get("name", "") if target else "", "is_pending": not source.is_active} for source, _extracted, _index, target in prepared]
    updated["sources"] = [item for item in previous if item.get("id") not in ids] + cards
    session.current_hypothesis = updated
    session.save(update_fields=["current_hypothesis", "updated_at"])
    ProductionTrainingTurn.objects.create(session=session, feedback="\n\n".join(instructions), understood_changes=updated.get("understood_changes", []), hypothesis=updated)
    return updated


def start_route_build(user, line, start_catalog, *, submit_job, record_stage, stale_after):
    """Start the existing asynchronous route build for one complete position."""
    _require_admin(user)
    from django.utils import timezone
    if not isinstance(line, dict) or not str(line.get("name", "")).strip():
        raise ValueError("Сначала заполните позицию и примените требования ТЗ.")
    try:
        if float(line.get("quantity")) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        raise ValueError("Сначала заполните позицию и примените требования ТЗ.")
    position_name = str(line["name"])[:500]
    running = ProductionTrainingSession.objects.filter(
        created_by=user, position_name=position_name, is_confirmed=False,
        current_hypothesis__status="processing", updated_at__gte=timezone.now() - stale_after,
    ).order_by("-updated_at").first()
    if running is not None:
        return {"status": "processing", "session_id": running.pk}
    session = ProductionTrainingSession.objects.create(
        created_by=user, position_name=position_name,
        requirements=line.get("requirements") if isinstance(line.get("requirements"), dict) else {},
        current_hypothesis={"status": "processing"},
    )
    def work(current_session):
        hypothesis = build_training_hypothesis(line, progress_callback=lambda stage: record_stage(current_session.pk, stage))
        if start_catalog:
            step_id = next((step.get("id") for step in hypothesis.get("route", {}).get("processes", []) if step.get("kind") == "catalog"), "")
            hypothesis = build_training_hypothesis(line, current=hypothesis, recompute="catalog", step_id=step_id, progress_callback=lambda stage: record_stage(current_session.pk, stage))
        ProductionTrainingTurn.objects.create(session=current_session, hypothesis=hypothesis)
        return hypothesis
    submit_job(session.pk, work)
    return {"status": "processing", "session_id": session.pk}


def revise_route_build(user, payload, *, submit_job, record_stage, stale_after):
    """Continue the existing route dialogue; shared by its screen and agent."""
    _require_admin(user)
    if not isinstance(payload, dict):
        raise ValueError("Не удалось продолжить диалог. Обновите гипотезу и повторите.")
    try:
        session = ProductionTrainingSession.objects.get(
            pk=payload.get("session_id"), created_by=user, is_confirmed=False,
        )
        line = payload.get("line") if isinstance(payload.get("line"), dict) else {}
        feedback = str(payload.get("feedback", "")).strip()
        question_answers = payload.get("question_answers") if isinstance(payload.get("question_answers"), dict) else None
        accepted_requirement_proposals = payload.get("accepted_requirement_proposals") if isinstance(payload.get("accepted_requirement_proposals"), list) else None
        instructions_override = payload.get("instructions") if isinstance(payload.get("instructions"), list) else None
        clear_ranking = bool(payload.get("clear_ranking"))
        refresh = bool(payload.get("refresh"))
        scope = str(payload.get("scope", "all")).strip().lower()
        learn_for_similar = bool(payload.get("learn_for_similar", not scope.endswith("_current")))
        if scope.endswith("_current"):
            scope = scope.removesuffix("_current")
        step_id = str(payload.get("step_id", "")).strip()
        recompute = "catalog" if scope in {"catalog", "requirements"} else "production" if scope == "production_price" else "all"
        quantity = Decimal(str(line.get("quantity") or "0").replace(",", "."))
        if len(feedback) > 3000 or not str(line.get("name", "")).strip() or quantity <= 0:
            raise ValueError
        if (not feedback and question_answers is None and accepted_requirement_proposals is None
                and instructions_override is None and not clear_ranking and not refresh
                and scope not in {"requirements", "catalog", "production_price"}):
            raise ValueError
    except (ValueError, TypeError, InvalidOperation, ProductionTrainingSession.DoesNotExist) as exc:
        raise ValueError("Не удалось продолжить диалог. Обновите гипотезу и повторите.") from exc

    prior = session.current_hypothesis if isinstance(session.current_hypothesis, dict) else {}
    if prior.get("status") == "processing":
        from django.utils import timezone
        if session.updated_at >= timezone.now() - stale_after:
            return {"status": "processing", "session_id": session.pk}
        saved_hypothesis = line.get("requirements", {}).get("production", {})
        if (isinstance(saved_hypothesis, dict) and str(saved_hypothesis.get("session_id")) == str(session.pk)
                and isinstance(saved_hypothesis.get("route"), dict)):
            prior = saved_hypothesis
        prior = {key: value for key, value in prior.items() if key not in {"status", "stage", "stage_label"}}
    if scope == "requirements" and not prior.get("catalog_search_started"):
        prior["requirement_selection"] = line.get("requirements", {}).get("requirements", [])
        session.current_hypothesis = prior
        session.save(update_fields=["current_hypothesis", "updated_at"])
        return {**prior, "session_id": session.pk}
    if question_answers is not None or accepted_requirement_proposals is not None:
        questions = {str(question.get("id")): question for question in prior.get("questions", []) if isinstance(question, dict) and question.get("id")}
        answers = {str(key): str(value).strip()[:1000] for key, value in (question_answers or {}).items() if str(key) in questions and str(value).strip()}
        prior = {**prior, "question_answers": {**prior.get("question_answers", {}), **answers}}
        requirements = dict(line.get("requirements")) if isinstance(line.get("requirements"), dict) else {}
        prior_requirements = session.requirements if isinstance(session.requirements, dict) else {}
        existing = requirements.get("clarifications")
        if not isinstance(existing, list):
            existing = prior_requirements.get("clarifications", [])
        clarifications = {str(item.get("question_id")): dict(item) for item in existing if isinstance(item, dict) and item.get("question_id")}
        for question_id, answer in answers.items():
            clarifications[question_id] = {"question_id": question_id, "label": str(questions[question_id].get("text", "")).strip()[:500], "value": answer, "source": "Моё уточнение", "source_type": "manager_clarification", "selected": True}
        proposals = {str(item.get("id")): item for item in prior.get("requirement_proposals", []) if isinstance(item, dict) and item.get("id")}
        for proposal_id in accepted_requirement_proposals or []:
            proposal = proposals.get(str(proposal_id))
            if proposal is not None:
                clarifications[f"name-{proposal_id}"] = {"question_id": f"name-{proposal_id}", "label": str(proposal.get("label", "")).strip()[:500], "value": str(proposal.get("value", "")).strip()[:1000], "source": "Из названия · подтверждено", "source_type": "manager_clarification", "selected": True}
        requirements["clarifications"] = list(clarifications.values())
        line = {**line, "requirements": requirements}

    def work(current_session):
        hypothesis = build_training_hypothesis(
            line, current=prior, feedback=feedback,
            progress_callback=lambda stage: record_stage(current_session.pk, stage),
            instructions_override=instructions_override, recompute=recompute,
            clear_ranking=clear_ranking, learn_for_similar=learn_for_similar,
            **({"step_id": step_id} if step_id else {}),
        )
        current_session.position_name = str(line.get("name", ""))[:500]
        current_session.requirements = line.get("requirements") if isinstance(line.get("requirements"), dict) else {}
        ProductionTrainingTurn.objects.create(session=current_session, feedback=feedback, understood_changes=hypothesis.get("understood_changes", []), hypothesis=hypothesis)
        return hypothesis

    session.current_hypothesis = {**prior, "status": "processing"}
    session.save(update_fields=["current_hypothesis", "updated_at"])
    submit_job(session.pk, work, fallback=prior)
    return {"status": "processing", "session_id": session.pk}


@transaction.atomic
def save_manual_training_example(user, payload):
    """Save the exact manual route-example entered on the production screen."""
    _require_admin(user)
    if not isinstance(payload, dict):
        raise ValueError("Не удалось сохранить учебный пример.")
    try:
        line = payload.get("line") if isinstance(payload.get("line"), dict) else {}
        production_type = ProductionType.objects.get(code=payload.get("production_type"), is_active=True)
        name = str(line.get("name", "")).strip()
        if not name:
            raise ValueError
        features = payload.get("features") if isinstance(payload.get("features"), list) else []
        raw_routes = payload.get("routes") if isinstance(payload.get("routes"), list) else []
    except (ValueError, TypeError, ProductionType.DoesNotExist) as exc:
        raise ValueError("Не удалось сохранить учебный пример.") from exc
    routes = []
    for route_index, raw_route in enumerate(raw_routes[:5]):
        if not isinstance(raw_route, dict):
            continue
        processes = []
        for raw_process in raw_route.get("processes", [])[:12]:
            if not isinstance(raw_process, dict):
                continue
            role = raw_process.get("role") if raw_process.get("role") in {"supply", "production", "completion"} else "production"
            process_name = str(raw_process.get("name", "")).strip()[:200]
            if not process_name:
                continue
            ProcessDefinition.objects.get_or_create(name=process_name, role=role)
            processes.append({"role": role, "name": process_name, "reason": str(raw_process.get("reason", ""))[:300]})
        if processes:
            routes.append({"name": str(raw_route.get("name", "")).strip()[:120] or f"Маршрут {route_index + 1}", "reason": str(raw_route.get("reason", ""))[:300], "processes": processes})
    if not routes:
        raise ValueError("Добавьте хотя бы один процесс в маршрут.")
    example = ProductionTrainingExample.objects.create(
        production_type=production_type, position_name=name[:500],
        requirements=line.get("requirements") if isinstance(line.get("requirements"), dict) else {},
        features=[str(value)[:300] for value in features[:10]], routes=routes,
        note=str(payload.get("note", ""))[:500], created_by=user,
    )
    refresh_training_example_embedding(example)
    ProductionTrainingExample.objects.filter(
        production_type=production_type, position_name__iexact=name, is_active=True,
    ).exclude(pk=example.pk).update(is_active=False, superseded_by=example)
    return {"message": f"Пример сохранён: {production_type.name}.", "example_id": example.pk}


def deactivate_lesson(lesson_id):
    """Deactivate the learned instruction without deleting historical evidence."""
    return bool(Lesson.objects.filter(pk=lesson_id, is_active=True).update(is_active=False))


@transaction.atomic
def deactivate_route_knowledge(payload):
    """Disable both learned route representations, mirroring the UI."""
    if not isinstance(payload, dict):
        raise ValueError("Не удалось определить сохранённый опыт")
    if payload.get("example_id") not in (None, ""):
        try:
            example_id = int(payload["example_id"])
        except (ValueError, TypeError):
            raise ValueError("Неверный идентификатор маршрута")
        changed = ProductionTrainingExample.objects.filter(pk=example_id).update(is_active=False)
        Lesson.objects.filter(
            session__confirmed_example_id=example_id,
            scope__in=["route", "production_step"],
        ).update(is_active=False)
        return bool(changed)
    try:
        lesson_id = int(payload["lesson_id"])
    except (ValueError, TypeError, KeyError):
        raise ValueError("Не указан идентификатор знания")
    lesson = Lesson.objects.filter(
        pk=lesson_id, scope__in=["route", "production_step"],
    ).first()
    if lesson is None:
        raise ValueError("Сохранённый опыт не найден")
    lesson.is_active = False
    lesson.save(update_fields=["is_active"])
    if lesson.session_id and lesson.session.confirmed_example_id:
        ProductionTrainingExample.objects.filter(
            pk=lesson.session.confirmed_example_id,
        ).update(is_active=False)
    return True


@transaction.atomic
def confirm_counterparty_with_stages(user, payload):
    """Commit one approved production-base counterparty with stage links."""
    if not isinstance(payload, dict):
        raise ValueError("Не удалось прочитать форму контрагента")
    name = str(payload.get("name", "")).strip()[:200]
    if not name:
        raise ValueError("Не указано название контрагента")
    notes = str(payload.get("notes", ""))[:2000]
    raw_ids = payload.get("stage_ids", [])
    if not isinstance(raw_ids, list) or not all(str(x).isdigit() for x in raw_ids):
        raise ValueError("Некорректные производственные этапы")
    stage_ids = list(dict.fromkeys(int(x) for x in raw_ids))
    stages = list(ProcessDefinition.objects.filter(pk__in=stage_ids))
    if len(stages) != len(stage_ids):
        raise ValueError("Один или несколько этапов не найдены")
    proposal = Proposal.objects.create(
        type=Proposal.TYPE_CREATE_COUNTERPARTY,
        payload={"name": name, "notes": notes},
        summary=f"Новый контрагент «{name}»", created_by=user,
    )
    apply_proposal(proposal, user)
    counterparty = Counterparty.objects.get(name=name)
    for stage in stages:
        link = Proposal.objects.create(
            type=Proposal.TYPE_LINK_STAGE_COUNTERPARTY, batch_id=proposal.batch_id,
            payload={"stage_id": stage.pk, "counterparty_id": counterparty.pk},
            summary=f"Связать «{stage.name}» и «{name}»", created_by=user,
        )
        apply_proposal(link, user)
    if payload.get("source_id"):
        TenderKnowledgeSource.objects.filter(
            pk=payload["source_id"], counterparty__isnull=True,
        ).update(counterparty=counterparty)
    return {
        "id": counterparty.pk, "name": counterparty.name,
        "stage_names": list(counterparty.stage_links.select_related("stage").values_list("stage__name", flat=True)),
    }

@transaction.atomic
def draft_production_feedback(user, text):
    """Prepare one reviewable proposal batch; no global changes are applied."""
    from .services import parse_production_feedback
    from .proposals import payload_from_feedback_item

    text = str(text).strip()
    if not text:
        raise ValueError("Напишите, что изменить")
    stages = list(ProcessDefinition.objects.filter(is_active=True).values("id", "name"))
    counterparties = list(Counterparty.objects.filter(is_active=True).values("id", "name"))
    items = parse_production_feedback(text, stages, counterparties)
    batch_id = None
    proposals = []
    for item in items:
        proposal = Proposal.objects.create(
            type=item["type"], payload=payload_from_feedback_item(item["type"], item["raw"]),
            summary=item["summary"], source_text=text, created_by=user,
            **({"batch_id": batch_id} if batch_id else {}),
        )
        batch_id = batch_id or proposal.batch_id
        proposals.append(proposal)
    return {
        "batch_id": str(batch_id) if batch_id else None,
        "items": [{"id": p.pk, "type": p.type, "type_display": p.get_type_display(),
                   "summary": p.summary, "payload": p.payload} for p in proposals],
        "active_stages": stages, "active_counterparties": counterparties,
    }
