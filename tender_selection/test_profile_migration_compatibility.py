from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class ProfileMigrationCompatibilityTests(TransactionTestCase):
    def test_both_deployed_histories_merge_without_losing_existing_records(self):
        app = 'tender_selection'
        base = (app, '0037_tender_market_forecast')
        target = (app, '0040_merge_profile_and_intake')
        restore_targets = MigrationExecutor(connection).loader.graph.leaf_nodes()
        try:
            for lineage in ('0039_alter_profile_triage_labels', '0039_tender_kanban_visible_until'):
                executor = MigrationExecutor(connection)
                executor.migrate([base])
                executor = MigrationExecutor(connection)
                executor.migrate([(app, lineage)])
                old_apps = executor.loader.project_state([(app, lineage)]).apps
                OldTender = old_apps.get_model(app, 'Tender')
                values = {'purchase_number': lineage, 'title': 'Сохранённая закупка',
                    'notification_raw': {'source': {'commonInfo': {'purchaseObjectInfo': 'Исходное описание'}}}}
                if lineage == '0039_alter_profile_triage_labels':
                    values.update(profile_reason='Причина прежнего решения', profile_model='existing/model')
                item = OldTender.objects.create(**values)
                executor = MigrationExecutor(connection)
                executor.migrate([target])
                CurrentTender = executor.loader.project_state([target]).apps.get_model(app, 'Tender')
                current = CurrentTender.objects.get(pk=item.pk)
                self.assertEqual(current.purchase_number, lineage)
                self.assertEqual(current.title, values['title'])
                self.assertEqual(current.notification_raw, values['notification_raw'])
                if 'profile_reason' in values:
                    self.assertEqual(current.profile_reason, values['profile_reason'])
                    self.assertEqual(current.profile_model, values['profile_model'])
        finally:
            MigrationExecutor(connection).migrate(restore_targets)
