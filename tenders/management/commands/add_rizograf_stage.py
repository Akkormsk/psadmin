from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from tenders.models import ProcessDefinition, Proposal, StageCounterpartyLink
from tenders.proposals import apply_proposal

SOURCE_URL = "https://www.fsprint.ru/rizograf.html"

STAGE_PAYLOAD = {
    "name": "Ризография",
    "supplies_input": False,
    "performs_production": True,
    "terminal_mode": "sometimes",
    "scope_tags": ["листовки", "буклеты", "брошюры", "бланки"],
    "when_to_use": "Небольшие и средние тиражи чёрно-белой/малоцветной печати на бумаге до 220 г/м2, где не нужна офсетная печатная форма.",
    "when_not_to_use": "Глянцевая бумага (краска ризографа не сохнет), большие тиражи многоцветной печати, где выгоднее офсет.",
    "parameters": {"required": ["тираж", "формат (А4/А3)", "тип бумаги"], "optional": []},
}


class Command(BaseCommand):
    """Разовая правка: этап «Ризография» + связь с FSPrint
    (price_source_type=internal_calculator, считаем сами по
    tenders.integrations.fsprint_rizograf — статичный прайс-лист
    fsprint.ru/rizograf.html, не калькулятор-API). Идемпотентно
    (get_or_create по имени в apply_proposal), можно запускать повторно
    и на другом окружении (тест) без дублей."""

    help = "Создаёт этап «Ризография» и связывает его с FSPrint как контрагентом."

    def handle(self, *args, **options):
        user = get_user_model().objects.filter(is_superuser=True).first()
        if user is None:
            raise CommandError("Не найден ни один суперпользователь — не на кого оформить.")

        stage_proposal = Proposal.objects.create(
            type=Proposal.TYPE_CREATE_STAGE, payload=STAGE_PAYLOAD,
            summary="Ризография — новый этап (fsprint.ru/rizograf.html, статичный прайс-лист)",
            source_text=SOURCE_URL, created_by=user,
        )
        apply_proposal(stage_proposal, user)
        stage = ProcessDefinition.objects.get(name=STAGE_PAYLOAD["name"])
        self.stdout.write(f"этап: {stage.name} (id={stage.id})")

        link_proposal = Proposal.objects.create(
            type=Proposal.TYPE_LINK_STAGE_COUNTERPARTY,
            payload={
                "stage_id": stage.id,
                "counterparty_name": "FSPrint",
                "price_source_type": StageCounterpartyLink.SOURCE_INTERNAL_CALCULATOR,
                "priority": 10,
                "settings": {
                    "pricing_module": "tenders.integrations.fsprint_rizograf",
                    "source_url": SOURCE_URL,
                    "captured": "2026-09-29",
                    "note": "Статичный прайс-лист, не калькулятор-API. Формула: тариф(тираж) + цена бумаги, А3 = 2x. Спецпредложение «листовки А7» на странице не включено.",
                },
            },
            summary="Связать «Ризография» с FSPrint (internal_calculator, fsprint_rizograf.py)",
            source_text=SOURCE_URL, created_by=user,
        )
        apply_proposal(link_proposal, user)
        link = StageCounterpartyLink.objects.get(stage=stage, counterparty__name="FSPrint")
        self.stdout.write(self.style.SUCCESS(f"связь: {link.stage.name} -> {link.counterparty.name} ({link.price_source_type})"))
