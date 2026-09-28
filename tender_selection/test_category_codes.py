from django.test import SimpleTestCase

from .services import CATEGORY_GROUPS


class CategoryCodeFormatTests(SimpleTestCase):
    """Госплан молча возвращает 0 результатов для «группового» ОКПД2-кода
    (4 символа, например "18.1") — нужен «классовый» уровень (5 символов,
    "18.12"), хотя наш собственный код матчит и то, и другое как префикс.
    Обнаружено на реальном тендере (018.12.19.190), который никогда не
    попадал во «Входящие» из-за кода "18.1" в списке категорий."""

    def test_every_category_code_is_a_full_okpd2_class(self):
        for code, label in CATEGORY_GROUPS:
            self.assertRegex(code, r"^\d{2}\.\d{2}$", f"{code} ({label}) — не классовый уровень ОКПД2")
