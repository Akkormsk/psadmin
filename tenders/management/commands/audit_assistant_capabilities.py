"""Read-only audit of the live PSADMIN assistant encyclopedia."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from tenders.assistant_coverage import audit_user_operation_coverage, changed_user_operations
from tenders.assistant_tools import available_tools, capability_issues, workflow_catalog


class Command(BaseCommand):
    help = "Check assistant registry and report workflow, route and executor coverage."

    def add_arguments(self, parser):
        parser.add_argument("--strict", action="store_true", help="Exit nonzero if registered executors are invalid.")
        parser.add_argument("--coverage", action="store_true", help="Discover user write operations and report missing assistant coverage.")
        parser.add_argument("--base-ref", help="Limit coverage to operations changed since this Git ref.")

    def handle(self, *args, **options):
        # No need for a real account: tools only consult is_superuser in inventory.
        class AuditUser:
            is_superuser = True

        user = AuditUser()
        actions = available_tools(user)
        workflows = workflow_catalog(user, actions)
        issues = capability_issues(user)
        uncovered = []
        self.stdout.write(f"Registered actions: {len(actions)}")
        self.stdout.write(f"Business workflows: {len(workflows)}")
        for workflow in workflows:
            self.stdout.write(f"  {workflow['id']}: {len(workflow['actions'])} actions")
        if issues:
            for issue in issues:
                self.stderr.write(f"{issue['id']}: {issue['issue']} -> {issue['source']}")
            if options["strict"]:
                raise CommandError(f"{len(issues)} capability registry issue(s)")
        else:
            self.stdout.write(self.style.SUCCESS("Registry references: OK"))
        if options["coverage"]:
            coverage = audit_user_operation_coverage(user)
            if options.get("base_ref"):
                changed_ids = {
                    operation.identifier for operation in changed_user_operations(options["base_ref"])
                }
                coverage = [result for result in coverage if result.operation.identifier in changed_ids]
                self.stdout.write(f"Compared with: {options['base_ref']}")
            uncovered = [result for result in coverage if not result.covered]
            excluded = [result for result in coverage if result.exclusion_reason]
            self.stdout.write(f"Discovered user write operations: {len(coverage)}")
            self.stdout.write(f"Published operations: {len(coverage) - len(uncovered) - len(excluded)}")
            self.stdout.write(f"Reasoned exclusions: {len(excluded)}")
            for result in uncovered:
                operation = result.operation
                action = f" [{operation.action}]" if operation.action else ""
                self.stderr.write(
                    f"UNCOVERED {operation.route_name}{action}: {operation.executor} ({operation.route})"
                )
        else:
            self.stdout.write("NOTE: run with --coverage to discover user operations outside the registry.")
        if options["strict"] and uncovered:
            raise CommandError(f"{len(uncovered)} user operation(s) lack assistant coverage")
