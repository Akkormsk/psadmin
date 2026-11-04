from unittest.mock import patch
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from tenders import test_source_routes as route_fixtures
from tenders.models import TenderComputeJob
from .models import FilterSettings
from .profile_triage import CONTRACT
from .services import intake_readiness_for


class IntakeReadinessTests(TestCase):
    setUp = route_fixtures.SourceRouteTests.setUp

    def test_readiness_requires_actual_routes_all_phases_and_verified_profile(self):
        route_fixtures.SourceRouteTests.run_route(self)
        selection = FilterSettings.load()
        selection.include_words = ""; selection.save(update_fields=["include_words"])
        user = get_user_model().objects.create_superuser('readiness-owner', password='test')
        summary = intake_readiness_for(user)
        self.assertEqual(summary['total'], 1)
        self.assertEqual(summary['routes_ready'], 1)
        self.assertEqual(summary['complete'], 0)
        job = TenderComputeJob.objects.latest('pk')
        job.status = 'ready'
        from .intake_readiness import PHASES
        job.diagnostics['stages'] = {phase: {'state': 'complete'} for phase in PHASES}
        job.save()
        self.tender.profile_assessment = {'contract': CONTRACT, 'title': self.tender.title or self.tender.object_info,
            'object_info': self.tender.object_info, 'verdict': 'fit'}
        self.tender.save(update_fields=['profile_assessment'])
        with patch('tenders.services._ai_gateway_json') as gateway:
            summary = intake_readiness_for(user)
        gateway.assert_not_called()
        self.assertTrue(summary['all_complete'])
        self.assertEqual(summary['complete'], 1)
        self.tender.profile_assessment['verdict'] = 'review'
        self.tender.save(update_fields=['profile_assessment'])
        self.assertTrue(intake_readiness_for(user)['all_complete'])
        self.tender.profile_assessment['stale'] = True
        self.tender.save(update_fields=['profile_assessment'])
        self.assertFalse(intake_readiness_for(user)['all_complete'])
        self.tender.profile_assessment.pop('stale')
        self.tender.profile_assessment['verdict'] = 'fit'
        self.tender.save(update_fields=['profile_assessment'])
        from tenders.assistant_tools import execute_tool
        self.assertEqual(execute_tool('tender.intake_readiness', user, {}), summary)
        self.client.force_login(user)
        self.assertEqual(self.client.get(reverse('tender_selection:analysis_status'), {'coverage': '1'}).json()['coverage'], summary)
        job.diagnostics['stages']['tz']['outcome'] = 'source_only'; job.save()
        self.assertFalse(intake_readiness_for(user)['all_complete'])

    def test_manager_cannot_read_all_incoming_private_coverage(self):
        user = get_user_model().objects.create_user('readiness-manager')
        with self.assertRaises(Exception):
            intake_readiness_for(user)
