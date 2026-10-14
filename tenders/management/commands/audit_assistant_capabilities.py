"""Read-only audit of the live PSADMIN assistant encyclopedia."""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from tenders.assistant_catalog import site_route_inventory, workflow_catalog
from tenders.assistant_tools import available_tools, capability_issues


class Command(BaseCommand):
    help = "Check assistant registry and report workflow, route and executor coverage."

    def add_arguments(self, parser):
        parser.add_argument("--strict", action="store_true", help="Exit nonzero if registered executors are invalid.")

    def handle(self, *args, **options):
        # No need for a real account: tools only consult is_superuser in inventory.
        class AuditUser:
            is_superuser = True

        user = AuditUser()
        actions = available_tools(user)
        workflows = workflow_catalog(user, actions)
        routes = site_route_inventory()
        issues = capability_issues(user)
        self.stdout.write(f"Registered actions: {len(actions)}")
        self.stdout.write(f"Business workflows: {len(workflows)}")
        self.stdout.write(f"Site routes (not equivalent to actions): {len(routes)}")
        for workflow in workflows:
            self.stdout.write(f"  {workflow['id']}: {len(workflow['actions'])} actions")
        if issues:
            for issue in issues:
                self.stderr.write(f"{issue['id']}: {issue['issue']} -> {issue['source']}")
            if options["strict"]:
                raise CommandError(f"{len(issues)} capability registry issue(s)")
        else:
            self.stdout.write(self.style.SUCCESS("Registry references: OK"))
        self.stdout.write(
            "NOTE: this checks registered references, NOT complete UI workflow coverage or end-to-end execution."
        )
