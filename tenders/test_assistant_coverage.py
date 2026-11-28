from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase

from .assistant_coverage import (
    CoverageResult,
    UserOperation,
    _action_values,
    _is_write_view,
    audit_user_operation_coverage,
    changed_user_operations,
)


def _write_view_fixture(request):
    return request.POST.get("name")


def _action_executor_fixture(action, record):
    if action == "create":
        record.save()
    elif action == "delete":
        record.delete()


class AssistantCoverageTests(SimpleTestCase):
    def test_write_view_and_action_variants_are_discovered_from_code(self):
        self.assertTrue(_is_write_view(_write_view_fixture))
        self.assertEqual(_action_values(_action_executor_fixture), ("create", "delete"))

    @patch("tenders.assistant_coverage.available_tools")
    @patch("tenders.assistant_coverage.available_capability_exclusions", return_value={})
    @patch("tenders.assistant_coverage.discover_user_operations")
    def test_existing_tool_source_covers_a_reachable_executor(self, discover, exclusions, tools):
        operation = UserOperation(
            identifier="sample",
            route_name="sample",
            route="sample/",
            callback="sample.views.write",
            executor="sample.actions.wrapper",
            source_file="sample/actions.py",
            reachable_executors=("sample.actions.wrapper", "sample.actions.execute"),
        )
        discover.return_value = [operation]
        tools.return_value = [{"id": "sample.execute", "source": "sample.actions.execute"}]

        result = audit_user_operation_coverage(SimpleNamespace(is_superuser=True))

        self.assertEqual(result, [CoverageResult(operation, ("sample.execute",))])

    @patch("tenders.assistant_coverage.available_tools", return_value=[])
    @patch("tenders.assistant_coverage.available_capability_exclusions")
    @patch("tenders.assistant_coverage.discover_user_operations")
    def test_reasoned_exclusion_covers_only_its_declared_action(self, discover, exclusions, tools):
        operation = UserOperation(
            identifier="sample:archive",
            route_name="sample",
            route="sample/",
            callback="sample.views.write",
            executor="sample.actions.execute",
            source_file="sample/actions.py",
            action="archive",
            reachable_executors=("sample.actions.execute",),
        )
        discover.return_value = [operation]
        exclusions.return_value = {"sample.actions.execute:archive": "Handled by an external signed webhook."}

        result = audit_user_operation_coverage(SimpleNamespace(is_superuser=True))

        self.assertTrue(result[0].covered)
        self.assertEqual(result[0].exclusion_reason, "Handled by an external signed webhook.")

    @patch("tenders.management.commands.audit_assistant_capabilities.audit_user_operation_coverage")
    def test_strict_command_rejects_uncovered_operation(self, audit):
        operation = UserOperation(
            identifier="sample",
            route_name="sample",
            route="sample/",
            callback="sample.views.write",
            executor="sample.actions.execute",
            source_file="sample/actions.py",
        )
        audit.return_value = [CoverageResult(operation, ())]

        with self.assertRaisesMessage(CommandError, "1 user operation"):
            call_command("audit_assistant_capabilities", coverage=True, strict=True)

    @patch("tenders.assistant_coverage._git_diff")
    def test_branch_comparison_finds_changed_code_and_new_route_references(self, git_diff):
        git_diff.return_value = """diff --git a/sample/actions.py b/sample/actions.py
--- a/sample/actions.py
+++ b/sample/actions.py
@@ -8,0 +9,2 @@
+record.save()
+unrelated_change
diff --git a/templates/sample.html b/templates/sample.html
--- a/templates/sample.html
+++ b/templates/sample.html
@@ -1,0 +2 @@
+{% url 'sample_route' %}
"""
        code_change = UserOperation(
            identifier="code",
            route_name="unchanged_route",
            route="unchanged/",
            callback="sample.views.code",
            executor="sample.actions.code",
            source_file="sample/actions.py",
            source_locations=(("sample/actions.py", 5, 12),),
        )
        route_reference = UserOperation(
            identifier="route",
            route_name="sample_route",
            route="sample/",
            callback="sample.views.route",
            executor="sample.actions.route",
            source_file="sample/other.py",
            source_locations=(("sample/other.py", 1, 4),),
        )
        untouched = UserOperation(
            identifier="untouched",
            route_name="other_route",
            route="other/",
            callback="sample.views.other",
            executor="sample.actions.other",
            source_file="sample/other.py",
            source_locations=(("sample/other.py", 10, 20),),
        )

        result = changed_user_operations("origin/test-server", [code_change, route_reference, untouched])

        self.assertEqual(result, [code_change, route_reference])
