import hashlib
import io
import json
import os
import time as _time
import uuid
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import requests

from django.test import SimpleTestCase, RequestFactory
from django.core.management import call_command
from django.utils import timezone

from telemetry.views import _is_admin, get_alerts_for_user, apply_window, get_type_choices, expand_type_labels, apply_filters, apply_keyword_filter
from telemetry.purge_scheduler import next_purge_target, sleep_until, run_purge
from telemetry.context_processors import admin_panel
from telemetry.jira import build_summary as jira_build_summary
from telemetry.jira import create_jira_ticket as jira_create_ticket_api
from telemetry.jira import priority_for_severity as jira_priority_for_severity
from telemetry.jira import GISC_TO_ASSIGNEE
from telemetry import email_sender as email_sender_mod
from telemetry.email_sender import send_email as email_sender_send
from wis2_ingestion import _compute_display_title, parse_wmem_record


class IsAdminCachingTest(SimpleTestCase):
    def _make_user(self, is_admin=False, is_superuser=False):
        user = MagicMock(spec=['id', 'groups', 'is_superuser'])
        user.id = 1
        user.is_superuser = is_superuser
        user.groups.filter.return_value.exists.return_value = is_admin
        return user

    def test_admin_returns_true(self):
        user = self._make_user(is_admin=True)
        self.assertTrue(_is_admin(user))

    def test_regular_returns_false(self):
        user = self._make_user(is_admin=False)
        self.assertFalse(_is_admin(user))

    def test_superuser_returns_true(self):
        user = self._make_user(is_admin=False, is_superuser=True)
        self.assertTrue(_is_admin(user))

    def test_result_is_cached(self):
        user = self._make_user(is_admin=False)
        _is_admin(user)
        self.assertFalse(user._is_admin_cached)
        user.groups.filter.return_value.exists.return_value = True
        self.assertFalse(_is_admin(user))

    def test_new_user_instance_has_no_cache(self):
        user = self._make_user(is_admin=True)
        _is_admin(user)
        self.assertTrue(user._is_admin_cached)
        user2 = self._make_user(is_admin=True)
        self.assertFalse(hasattr(user2, '_is_admin_cached'))


class PostOnlyLogoutTest(SimpleTestCase):
    def test_get_returns_405(self):
        from django.test import Client
        client = Client()
        response = client.get('/logout/')
        self.assertEqual(response.status_code, 405)

    def test_post_without_auth_redirects(self):
        from django.test import Client
        client = Client()
        response = client.post('/logout/')
        self.assertIn(response.status_code, (302, 403))


class IntParamHandlingTest(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = MagicMock()
        self.user.is_authenticated = True
        self.user.id = 1
        self.user.groups.filter.return_value.exists.return_value = True

    @patch('telemetry.views.get_alerts_for_user')
    def test_api_alerts_per_day_bad_days(self, mock_get_alerts):
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs
        mock_qs.annotate.return_value = mock_qs
        mock_qs.values.return_value = mock_qs
        mock_qs.__iter__ = lambda s: iter([])
        mock_get_alerts.return_value = mock_qs
        from telemetry.views import api_alerts_per_day
        request = self.factory.get('/api/alerts/per-day/', {'days': 'abc'})
        request.user = self.user
        response = api_alerts_per_day(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['granularity'], 'day')

    @patch('telemetry.views.get_alerts_for_user')
    def test_api_alerts_per_day_valid_params(self, mock_get_alerts):
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs
        mock_qs.annotate.return_value = mock_qs
        mock_qs.values.return_value = mock_qs
        mock_qs.__iter__ = lambda s: iter([])
        mock_get_alerts.return_value = mock_qs
        from telemetry.views import api_alerts_per_day
        request = self.factory.get('/api/alerts/per-day/', {'days': '7'})
        request.user = self.user
        response = api_alerts_per_day(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(len(data['labels']), 7)
        self.assertEqual(data['granularity'], 'day')

    @patch('telemetry.views.get_alerts_for_user')
    def test_api_alerts_per_day_12h_is_hourly(self, mock_get_alerts):
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs
        mock_qs.annotate.return_value = mock_qs
        mock_qs.values.return_value = mock_qs
        mock_qs.__iter__ = lambda s: iter([])
        mock_get_alerts.return_value = mock_qs
        from telemetry.views import api_alerts_per_day
        request = self.factory.get('/api/alerts/per-day/', {'window': '12h', 'days': '30'})
        request.user = self.user
        response = api_alerts_per_day(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['granularity'], 'hour')
        mock_qs.filter.assert_called()

    @patch('telemetry.views.get_alerts_for_user')
    def test_api_alerts_per_day_custom_invalid_falls_back_to_all(self, mock_get_alerts):
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs
        mock_qs.annotate.return_value = mock_qs
        mock_qs.values.return_value = mock_qs
        mock_qs.__iter__ = lambda s: iter([])
        mock_get_alerts.return_value = mock_qs
        from telemetry.views import api_alerts_per_day
        request = self.factory.get('/api/alerts/per-day/', {'window': 'custom', 'from': 'garbage', 'to': '2026-08-03T00:00'})
        request.user = self.user
        response = api_alerts_per_day(request)
        data = json.loads(response.content)
        self.assertEqual(data['granularity'], 'day')

    @patch('telemetry.views.get_alerts_for_user')
    def test_api_alerts_per_day_custom_inverted_falls_back_to_all(self, mock_get_alerts):
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs
        mock_qs.annotate.return_value = mock_qs
        mock_qs.values.return_value = mock_qs
        mock_qs.__iter__ = lambda s: iter([])
        mock_get_alerts.return_value = mock_qs
        from telemetry.views import api_alerts_per_day
        request = self.factory.get('/api/alerts/per-day/', {'window': 'custom', 'from': '2026-08-05T00:00', 'to': '2026-08-03T00:00'})
        request.user = self.user
        response = api_alerts_per_day(request)
        data = json.loads(response.content)
        self.assertEqual(data['granularity'], 'day')

    @patch('telemetry.views.get_alerts_for_user')
    def test_api_alerts_per_day_custom_24h_is_hourly(self, mock_get_alerts):
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs
        mock_qs.annotate.return_value = mock_qs
        mock_qs.values.return_value = mock_qs
        mock_qs.__iter__ = lambda s: iter([])
        mock_get_alerts.return_value = mock_qs
        from telemetry.views import api_alerts_per_day
        request = self.factory.get('/api/alerts/per-day/', {'window': 'custom', 'from': '2026-08-01T00:00', 'to': '2026-08-02T00:00'})
        request.user = self.user
        response = api_alerts_per_day(request)
        data = json.loads(response.content)
        self.assertEqual(data['granularity'], 'hour')

    @patch('telemetry.views.get_alerts_for_user')
    def test_api_alerts_per_day_custom_long_is_daily(self, mock_get_alerts):
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs
        mock_qs.annotate.return_value = mock_qs
        mock_qs.values.return_value = mock_qs
        mock_qs.__iter__ = lambda s: iter([])
        mock_get_alerts.return_value = mock_qs
        from telemetry.views import api_alerts_per_day
        request = self.factory.get('/api/alerts/per-day/', {'window': 'custom', 'from': '2026-08-01T00:00', 'to': '2026-08-03T00:00'})
        request.user = self.user
        response = api_alerts_per_day(request)
        data = json.loads(response.content)
        self.assertEqual(data['granularity'], 'day')
        self.assertEqual(len(data['labels']), 3)


class ApplyWindowTest(SimpleTestCase):
    def test_all_returns_qs_unchanged(self):
        qs = MagicMock()
        new_qs, window = apply_window(qs, 'all', '', '')
        self.assertIs(new_qs, qs)
        self.assertEqual(window, 'all')

    def test_unknown_window_falls_back_to_all(self):
        qs = MagicMock()
        new_qs, window = apply_window(qs, 'bogus')
        self.assertIs(new_qs, qs)
        self.assertEqual(window, 'all')

    def test_12h_filters(self):
        qs = MagicMock()
        apply_window(qs, '12h')
        qs.filter.assert_called_once()
        self.assertIn('event_time__gte', qs.filter.call_args.kwargs)

    def test_24h_filters(self):
        qs = MagicMock()
        apply_window(qs, '24h')
        qs.filter.assert_called_once()
        self.assertIn('event_time__gte', qs.filter.call_args.kwargs)

    def test_custom_valid_filters_both_bounds(self):
        qs = MagicMock()
        new_qs, window = apply_window(qs, 'custom', '2026-08-01T00:00', '2026-08-02T23:59')
        qs.filter.assert_called_once_with(
            event_time__gte='2026-08-01T00:00',
            event_time__lte='2026-08-02T23:59',
        )
        self.assertEqual(window, 'custom')

    def test_custom_missing_bound_falls_back_to_all(self):
        qs = MagicMock()
        new_qs, window = apply_window(qs, 'custom', '', '2026-08-02T23:59')
        self.assertIs(new_qs, qs)
        self.assertEqual(window, 'all')

    def test_custom_invalid_falls_back_to_all(self):
        qs = MagicMock()
        new_qs, window = apply_window(qs, 'custom', 'not-a-date', '2026-08-02T23:59')
        self.assertIs(new_qs, qs)
        self.assertEqual(window, 'all')

    def test_custom_reversed_falls_back_to_all(self):
        qs = MagicMock()
        new_qs, window = apply_window(qs, 'custom', '2026-08-02T00:00', '2026-08-01T00:00')
        self.assertIs(new_qs, qs)
        self.assertEqual(window, 'all')


class AlertSearchTest(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = MagicMock()
        self.user.is_authenticated = True
        self.user.id = 1

    def test_requires_login(self):
        from telemetry.views import alert_search
        request = self.factory.get('/api/alert-search/?q=meteo')
        request.user = MagicMock(is_authenticated=False)
        response = alert_search(request)
        self.assertEqual(response.status_code, 302)

    @patch('telemetry.views.apply_keyword_filter')
    @patch('telemetry.views.get_alerts_for_user')
    def test_empty_q_returns_false(self, mock_get_alerts, mock_kw):
        mock_get_alerts.return_value = MagicMock()
        from telemetry.views import alert_search
        request = self.factory.get('/api/alert-search/')
        request.user = self.user
        response = alert_search(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertFalse(data['found'])
        self.assertEqual(data['count'], 0)
        mock_kw.assert_not_called()

    @patch('telemetry.views.apply_keyword_filter')
    @patch('telemetry.views.get_alerts_for_user')
    def test_match_returns_found_true(self, mock_get_alerts, mock_kw):
        mock_qs = MagicMock()
        mock_qs.count.return_value = 3
        mock_kw.return_value = mock_qs
        mock_get_alerts.return_value = MagicMock()
        from telemetry.views import alert_search
        request = self.factory.get('/api/alert-search/?q=meteo')
        request.user = self.user
        response = alert_search(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['found'])
        self.assertEqual(data['count'], 3)

    @patch('telemetry.views.apply_keyword_filter')
    @patch('telemetry.views.get_alerts_for_user')
    def test_no_match_returns_found_false(self, mock_get_alerts, mock_kw):
        mock_qs = MagicMock()
        mock_qs.count.return_value = 0
        mock_kw.return_value = mock_qs
        mock_get_alerts.return_value = MagicMock()
        from telemetry.views import alert_search
        request = self.factory.get('/api/alert-search/?q=nope')
        request.user = self.user
        response = alert_search(request)
        data = json.loads(response.content)
        self.assertFalse(data['found'])


def _q_fields(qobj, acc=None):
    if acc is None:
        acc = []
    for child in qobj.children:
        if isinstance(child, tuple):
            acc.append(child[0])
        else:
            _q_fields(child, acc)
    return acc


class KeywordFilterTest(SimpleTestCase):
    def test_empty_q_returns_qs_unchanged(self):
        qs = MagicMock()
        out = apply_keyword_filter(qs, '   ')
        self.assertIs(out, qs)
        qs.filter.assert_not_called()

    def test_non_uuid_keyword_matches_text_fields(self):
        qs = MagicMock()
        qs.filter.return_value = qs
        qs.annotate.return_value = qs
        apply_keyword_filter(qs, 'meteo')
        fields = _q_fields(qs.filter.call_args_list[0].args[0])
        for field in (
            'event_type__icontains', 'source__icontains', 'node__name__icontains',
            'title__icontains', 'display_title__icontains', 'description__icontains',
            'subtype__icontains', 'channel__icontains', 'dataschema__icontains',
            'incident_hash__icontains',
        ):
            self.assertIn(field, fields)
        self.assertNotIn('id', fields)
        blob_kwargs = qs.filter.call_args_list[1].kwargs
        self.assertIn('_searchable_blob__icontains', blob_kwargs)

    def test_uuid_keyword_adds_exact_id_match(self):
        qs = MagicMock()
        qs.filter.return_value = qs
        qs.annotate.return_value = qs
        apply_keyword_filter(qs, str(uuid.uuid4()))
        fields = _q_fields(qs.filter.call_args_list[0].args[0])
        self.assertIn('id', fields)

    def test_apply_filters_applies_keyword(self):
        qs = MagicMock()
        qs.filter.return_value = qs
        qs.annotate.return_value = qs
        apply_filters(qs, {'q': 'meteo'})
        fields = _q_fields(qs.filter.call_args_list[0].args[0])
        self.assertIn('event_type__icontains', fields)


class GetAlertsForUserTest(SimpleTestCase):
    def test_admin_gets_all(self):
        user = MagicMock(spec=['id'])
        user.id = 1
        with patch('telemetry.views._is_admin', return_value=True):
            with patch('telemetry.views.Alert') as MockAlert:
                result = get_alerts_for_user(user)
                MockAlert.objects.all.assert_called_once()
                self.assertEqual(result, MockAlert.objects.all.return_value)

    def test_regular_with_profile(self):
        user = MagicMock(spec=['id', 'profile'])
        user.id = 1
        profile = MagicMock()
        profile.allowed_nodes.all.return_value = ['node1']
        user.profile = profile
        with patch('telemetry.views._is_admin', return_value=False):
            with patch('telemetry.views.Alert') as MockAlert:
                result = get_alerts_for_user(user)
                MockAlert.objects.filter.assert_called_once_with(node__in=['node1'])
                self.assertEqual(result, MockAlert.objects.filter.return_value)

    def test_regular_no_profile_returns_empty(self):
        user = MagicMock(spec=['id'])
        user.id = 1
        with patch('telemetry.views._is_admin', return_value=False):
            with patch('telemetry.views.Alert') as MockAlert:
                result = get_alerts_for_user(user)
                MockAlert.objects.none.assert_called_once()
                self.assertEqual(result, MockAlert.objects.none.return_value)


class ComputeDisplayTitleTest(SimpleTestCase):
    def test_template_notice(self):
        self.assertEqual(_compute_display_title('Template: foo', ''), 'Maintenance')

    def test_test_system(self):
        self.assertEqual(_compute_display_title('un-wmo-global-test', ''), 'Maintenance')

    def test_cma_global_monitor(self):
        self.assertEqual(_compute_display_title('CMA Global Monitor', ''), 'Maintenance')

    def test_gisc_beijing(self):
        self.assertEqual(_compute_display_title('GISC Beijing node', ''), 'Maintenance')

    def test_dwd_service(self):
        self.assertEqual(_compute_display_title('DWD Service', ''), 'Maintenance')

    def test_maintenance_keyword(self):
        self.assertEqual(_compute_display_title('some maintenance task', ''), 'Maintenance')

    def test_context_deadline(self):
        self.assertEqual(_compute_display_title('t', 'context deadline exceeded'), 'Timeout: context deadline exceeded')

    def test_unexpected_eof(self):
        self.assertEqual(_compute_display_title('t', 'unexpected EOF'), 'Network Termination: unexpected EOF')

    def test_goaway(self):
        self.assertEqual(_compute_display_title('t', 'server sent GOAWAY'), 'Network Termination: server sent GOAWAY')

    def test_connection_refused(self):
        self.assertEqual(_compute_display_title('t', 'connection refused'), 'Connection Refused')

    def test_http_502(self):
        self.assertEqual(_compute_display_title('t', 'HTTP status 502'), 'HTTP Error: 502 Bad Gateway')

    def test_http_403(self):
        self.assertEqual(_compute_display_title('t', 'HTTP status 403'), 'HTTP Error: 403 Forbidden')

    def test_http_404(self):
        self.assertEqual(_compute_display_title('gc.wis.cma.cn', 'HTTP status 404 Not Found'), 'HTTP Error: 404 Not Found')

    def test_http_503(self):
        self.assertEqual(_compute_display_title('t', 'HTTP status 503'), 'HTTP Error: 503 Service Unavailable')

    def test_http_unknown_code(self):
        self.assertEqual(_compute_display_title('t', 'HTTP status 999'), 'HTTP Error: 999')

    def test_network_unreachable(self):
        self.assertEqual(_compute_display_title('wis2.ncm.gov.sa', 'connect: network is unreachable'), 'Network Error: network is unreachable')

    def test_invalid_response(self):
        self.assertEqual(_compute_display_title('t', 'expected a valid start token'), 'Invalid Response')

    def test_io_timeout(self):
        self.assertEqual(_compute_display_title('t', 'i/o timeout'), 'Timeout: i/o timeout')

    def test_target_is_down(self):
        self.assertEqual(_compute_display_title('Target foo is down', ''), 'Target is down')

    def test_fallback_to_title(self):
        self.assertEqual(_compute_display_title('Some other title', ''), 'Some other title')

    def test_none_title_and_description(self):
        self.assertEqual(_compute_display_title(None, None), '')

    def test_empty_strings(self):
        self.assertEqual(_compute_display_title('', ''), '')


class ParseWmemRecordTest(SimpleTestCase):
    def _make_payload(self, **overrides):
        base = {
            'id': 'test-123',
            'type': 'ca.meteocean.wis2.alert',
            'source': 'test/source',
            'subject': 'node-a',
            'time': '2026-01-01T00:00:00Z',
            'data': {
                'severity': 'WARNING',
                'content': {
                    'title': 'Test alert',
                    'description': 'desc',
                }
            }
        }
        base.update(overrides)
        return base

    def test_valid_record(self):
        result = parse_wmem_record(self._make_payload())
        self.assertIsNotNone(result)
        self.assertEqual(result[0], 'test-123')
        self.assertEqual(result[2], 'ca.meteocean.wis2.alert')
        self.assertEqual(result[4], 'node-a')

    def test_no_id_returns_none(self):
        result = parse_wmem_record({'type': 'x', 'data': {}})
        self.assertIsNone(result)

    def test_empty_dict_returns_none(self):
        result = parse_wmem_record({})
        self.assertIsNone(result)

    def test_incident_hash_deterministic(self):
        payload = self._make_payload()
        r1 = parse_wmem_record(payload)
        r2 = parse_wmem_record(payload)
        self.assertEqual(r1[15], r2[15])

    def test_incident_hash_differs_by_title(self):
        r1 = parse_wmem_record(self._make_payload())
        payload2 = self._make_payload()
        payload2['data']['content']['title'] = 'Different title'
        r2 = parse_wmem_record(payload2)
        self.assertNotEqual(r1[15], r2[15])

    def test_incident_hash_differs_by_node(self):
        r1 = parse_wmem_record(self._make_payload())
        r2 = parse_wmem_record(self._make_payload(subject='node-b'))
        self.assertNotEqual(r1[15], r2[15])

    def test_display_title_computed(self):
        result = parse_wmem_record(self._make_payload())
        self.assertEqual(result[14], 'Test alert')

    def test_malformed_returns_none(self):
        result = parse_wmem_record({'id': 123, 'data': 'not a dict'})
        self.assertIsNone(result)


class PurgeAlertsCommandTest(SimpleTestCase):
    def _configure_batches(self, mock_alert, batches):
        mock_filter = mock_alert.objects.filter.return_value
        mock_filter.values_list.return_value.__getitem__.side_effect = batches
        return mock_filter

    @patch('telemetry.management.commands.purge_alerts.Alert')
    @patch('telemetry.management.commands.purge_alerts.IncidentEvent')
    @patch('telemetry.management.commands.purge_alerts.get_retention_days')
    def test_persistence_mode_does_nothing(self, mock_days, mock_incident, mock_alert):
        mock_days.return_value = None
        out = io.StringIO()
        call_command('purge_alerts', stdout=out)
        self.assertIn('Persistence mode', out.getvalue())
        mock_alert.objects.filter.assert_not_called()
        mock_incident.objects.filter.assert_not_called()

    @patch('telemetry.management.commands.purge_alerts.Alert')
    @patch('telemetry.management.commands.purge_alerts.IncidentEvent')
    @patch('telemetry.management.commands.purge_alerts.transaction')
    @patch('telemetry.management.commands.purge_alerts.get_retention_days')
    def test_purges_in_batches_and_cascades_history(self, mock_days, mock_txn, mock_incident, mock_alert):
        mock_days.return_value = 30
        batch1 = [str(uuid.uuid4()) for _ in range(5000)]
        batch2 = [str(uuid.uuid4())]
        mock_filter = self._configure_batches(mock_alert, [batch1, batch2, []])
        mock_incident.objects.filter.return_value.delete.return_value = (1, {})
        mock_filter.delete.side_effect = [(3, {}), (1, {})]

        out = io.StringIO()
        call_command('purge_alerts', stdout=out)

        self.assertEqual(mock_incident.objects.filter.call_count, 2)
        self.assertEqual(mock_filter.delete.call_count, 2)
        self.assertIn('Done: 4 alert(s) purged.', out.getvalue())

    @patch('telemetry.management.commands.purge_alerts.Alert')
    @patch('telemetry.management.commands.purge_alerts.IncidentEvent')
    @patch('telemetry.management.commands.purge_alerts.get_retention_days')
    def test_dry_run_deletes_nothing(self, mock_days, mock_incident, mock_alert):
        mock_days.return_value = 30
        mock_filter = mock_alert.objects.filter.return_value
        mock_filter.count.return_value = 10

        out = io.StringIO()
        call_command('purge_alerts', stdout=out, dry_run=True)

        self.assertIn('would purge 10 alert(s) (dry-run)', out.getvalue())
        self.assertIn('would be purged (dry-run)', out.getvalue())
        mock_filter.values_list.assert_not_called()
        mock_incident.objects.filter.assert_not_called()

    @patch('telemetry.management.commands.purge_alerts.Alert')
    @patch('telemetry.management.commands.purge_alerts.IncidentEvent')
    @patch('telemetry.management.commands.purge_alerts.transaction')
    @patch('telemetry.management.commands.purge_alerts.get_retention_days')
    def test_days_override_respected(self, mock_days, mock_txn, mock_incident, mock_alert):
        mock_days.return_value = None
        batch = [str(uuid.uuid4())]
        mock_filter = self._configure_batches(mock_alert, [batch, []])
        mock_filter.delete.side_effect = [(1, {})]

        out = io.StringIO()
        call_command('purge_alerts', stdout=out, days=30)

        self.assertIn('purging alerts ingested before', out.getvalue())
        self.assertIn('Done: 1 alert(s) purged.', out.getvalue())

    def test_get_retention_days_returns_none_without_policy(self):
        from telemetry.models import get_retention_days
        with patch('telemetry.models.AlertRetentionPolicy.objects', new=MagicMock()) as mock_objects:
            mock_objects.first.return_value = None
            self.assertIsNone(get_retention_days())

    def test_get_retention_days_inactive_ignores_days(self):
        from telemetry.models import get_retention_days
        with patch('telemetry.models.AlertRetentionPolicy.objects', new=MagicMock()) as mock_objects:
            obj = MagicMock()
            obj.ttl_active = False
            obj.retention_days = 30
            mock_objects.first.return_value = obj
            self.assertIsNone(get_retention_days())

    def test_get_retention_days_active_with_days(self):
        from telemetry.models import get_retention_days
        with patch('telemetry.models.AlertRetentionPolicy.objects', new=MagicMock()) as mock_objects:
            obj = MagicMock()
            obj.ttl_active = True
            obj.retention_days = 30
            mock_objects.first.return_value = obj
            self.assertEqual(get_retention_days(), 30)

    def test_get_retention_days_active_without_days(self):
        from telemetry.models import get_retention_days
        with patch('telemetry.models.AlertRetentionPolicy.objects', new=MagicMock()) as mock_objects:
            obj = MagicMock()
            obj.ttl_active = True
            obj.retention_days = None
            mock_objects.first.return_value = obj
            self.assertIsNone(get_retention_days())

    def test_clean_requires_days_when_active(self):
        from django.core.exceptions import ValidationError
        from telemetry.models import AlertRetentionPolicy
        policy = AlertRetentionPolicy(ttl_active=True, retention_days=None)
        with self.assertRaises(ValidationError):
            policy.clean()

    def test_clean_allows_active_with_days_and_inactive_without(self):
        from telemetry.models import AlertRetentionPolicy
        AlertRetentionPolicy(ttl_active=True, retention_days=30).clean()
        AlertRetentionPolicy(ttl_active=False, retention_days=None).clean()
        AlertRetentionPolicy(ttl_active=False, retention_days=30).clean()


class TypeFilterGroupingTest(SimpleTestCase):
    RAW_TYPES = [
        'int.wmo.wis.wme.wnm.validation.metadata',
        'int.wmo.wis.wme.event.wnm.validation.metadata',
        'int.wmo.wis.wme.wnm.validation.schema',
    ]

    def _user(self):
        user = MagicMock()
        user.id = 1
        user.groups.filter.return_value.exists.return_value = True
        return user

    @patch('telemetry.views.get_cached_choices')
    def test_get_type_choices_groups_duplicate_labels(self, mock_choices):
        mock_choices.return_value = list(self.RAW_TYPES)
        self.assertEqual(
            get_type_choices(self._user()),
            ['validation metadata', 'validation schema'],
        )

    @patch('telemetry.views.get_cached_choices')
    def test_expand_type_labels_returns_all_variants(self, mock_choices):
        mock_choices.return_value = list(self.RAW_TYPES)
        expanded = expand_type_labels(self._user(), ['validation metadata'])
        self.assertEqual(
            set(expanded),
            {
                'int.wmo.wis.wme.wnm.validation.metadata',
                'int.wmo.wis.wme.event.wnm.validation.metadata',
            },
        )

    @patch('telemetry.views.get_cached_choices')
    def test_expand_type_labels_empty(self, mock_choices):
        mock_choices.return_value = list(self.RAW_TYPES)
        self.assertEqual(expand_type_labels(self._user(), []), [])

    @patch('telemetry.views.get_cached_choices')
    def test_expand_type_labels_unknown_label_returns_empty(self, mock_choices):
        mock_choices.return_value = list(self.RAW_TYPES)
        self.assertEqual(expand_type_labels(self._user(), ['nope']), [])

    def test_apply_filters_uses_event_type_in(self):
        qs = MagicMock()
        qs.filter.return_value = qs
        types = [
            'int.wmo.wis.wme.wnm.validation.metadata',
            'int.wmo.wis.wme.event.wnm.validation.metadata',
        ]
        out = apply_filters(qs, {'type': types})
        qs.filter.assert_called_with(event_type__in=types)
        self.assertIs(out, qs)


class PurgeSchedulerTest(SimpleTestCase):
    def _now(self, y, m, d, hh, mm=0):
        return timezone.make_aware(datetime(y, m, d, hh, mm))

    def test_target_before_hour_is_today(self):
        now = self._now(2026, 8, 3, 2, 30)
        target = next_purge_target(now, hour=3)
        self.assertEqual((target.hour, target.minute), (3, 0))
        self.assertEqual(target.date(), now.date())

    def test_target_after_hour_is_tomorrow(self):
        now = self._now(2026, 8, 3, 4, 30)
        target = next_purge_target(now, hour=3)
        self.assertEqual((target.hour, target.minute), (3, 0))
        self.assertEqual(target.date(), now.date() + timedelta(days=1))

    def test_target_at_hour_is_tomorrow(self):
        now = self._now(2026, 8, 3, 3, 0)
        target = next_purge_target(now, hour=3)
        self.assertEqual(target.date(), now.date() + timedelta(days=1))

    def test_sleep_until_stops_when_stop_fn_true(self):
        start = _time.monotonic()
        sleep_until(timezone.now() + timedelta(days=1), stop_fn=lambda: True)
        self.assertLess(_time.monotonic() - start, 1)

    @patch('telemetry.purge_scheduler.call_command')
    def test_run_purge_calls_command(self, mock_call):
        ok = run_purge()
        self.assertTrue(ok)
        mock_call.assert_called_once()
        self.assertEqual(mock_call.call_args[0][0], 'purge_alerts')
        self.assertIn('stdout', mock_call.call_args[1])

    @patch('telemetry.purge_scheduler.call_command')
    def test_run_purge_survives_failure(self, mock_call):
        mock_call.side_effect = RuntimeError('boom')
        ok = run_purge()
        self.assertFalse(ok)


class ContextProcessorTest(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @patch('telemetry.context_processors._is_admin', return_value=True)
    def test_exposes_admin_true(self, _):
        req = self.factory.get('/')
        req.user = object()
        self.assertTrue(admin_panel(req)['is_admin'])

    @patch('telemetry.context_processors._is_admin', return_value=False)
    def test_exposes_admin_false(self, _):
        req = self.factory.get('/')
        req.user = object()
        self.assertFalse(admin_panel(req)['is_admin'])


class JiraSummaryTest(SimpleTestCase):
    def _alert(self, display=None, title=None):
        alert = MagicMock()
        alert.display_title = display
        alert.title = title
        return alert

    def test_same_titles_returns_single(self):
        self.assertEqual(jira_build_summary(self._alert('X', 'X')), 'X')

    def test_different_titles_concatenated(self):
        self.assertEqual(
            jira_build_summary(self._alert('Maintenance', 'CMA Global Monitor')),
            'Maintenance | CMA Global Monitor',
        )

    def test_only_title(self):
        self.assertEqual(jira_build_summary(self._alert(None, 'Some title')), 'Some title')

    def test_none_falls_back(self):
        self.assertEqual(jira_build_summary(self._alert(None, None)), 'Untitled Alert')


class JiraCreateTicketTest(SimpleTestCase):
    def _patch_env(self, **kwargs):
        base = {
            'JIRA_URL': 'https://jira.wmo.int',
            'JIRA_PROJECT_KEY': 'TESTWIS',
            'JIRA_API_TOKEN': 'tok',
        }
        base.update(kwargs)
        return patch.dict(os.environ, base)

    def test_unconfigured_returns_error(self):
        with patch.dict(os.environ, {
            'JIRA_URL': '',
            'JIRA_PROJECT_KEY': '',
            'JIRA_API_TOKEN': '',
        }):
            key, error = jira_create_ticket_api('sum', 'desc')
        self.assertIsNone(key)
        self.assertIn('not configured', error)

    @patch('telemetry.jira.requests.post')
    def test_success_returns_key(self, mock_post):
        mock_post.return_value.status_code = 201
        mock_post.return_value.json.return_value = {'key': 'TESTWIS-1'}
        with self._patch_env():
            key, error = jira_create_ticket_api('sum', 'desc')
        self.assertEqual(key, 'TESTWIS-1')
        self.assertIsNone(error)
        sent = mock_post.call_args
        self.assertEqual(sent[1]['json']['fields']['project']['key'], 'TESTWIS')
        self.assertEqual(sent[1]['json']['fields']['summary'], 'sum')
        self.assertNotIn('customfield_10002', sent[1]['json']['fields'])
        self.assertEqual(sent[1]['headers']['Authorization'], 'Bearer tok')

    @patch('telemetry.jira.requests.post')
    def test_custom_field_included_when_configured(self, mock_post):
        mock_post.return_value.status_code = 201
        mock_post.return_value.json.return_value = {'key': 'TESTWIS-2'}
        with self._patch_env(
            JIRA_COUNTRY_FIELD_ID='customfield_10002',
            JIRA_COUNTRY_FIELD_VALUE='morocco',
        ):
            key, error = jira_create_ticket_api('sum', 'desc')
        self.assertEqual(key, 'TESTWIS-2')
        self.assertIsNone(error)
        fields = mock_post.call_args[1]['json']['fields']
        self.assertEqual(fields['customfield_10002'], 'morocco')

    @patch('telemetry.jira.requests.post')
    def test_failure_returns_error_text(self, mock_post):
        mock_post.return_value.status_code = 400
        mock_post.return_value.text = 'bad request'
        with self._patch_env():
            key, error = jira_create_ticket_api('sum', 'desc')
        self.assertIsNone(key)
        self.assertEqual(error, 'bad request')

    @patch('telemetry.jira.requests.post')
    def test_network_error_returns_message(self, mock_post):
        mock_post.side_effect = requests.RequestException('boom')
        with self._patch_env():
            key, error = jira_create_ticket_api('sum', 'desc')
        self.assertIsNone(key)
        self.assertEqual(error, 'boom')

    def test_priority_critical_high(self):
        self.assertEqual(jira_priority_for_severity('CRITICAL'), 'High')

    def test_priority_error_medium(self):
        self.assertEqual(jira_priority_for_severity('ERROR'), 'Medium')

    def test_priority_other_low(self):
        for sev in ('WARNING', 'INFO', 'DEBUG', 'UNKNOWN', '', None):
            self.assertEqual(jira_priority_for_severity(sev), 'Low')

    def test_priority_case_insensitive(self):
        self.assertEqual(jira_priority_for_severity('critical'), 'High')

    @patch('telemetry.jira.requests.post')
    def test_assignee_and_priority_included_in_payload(self, mock_post):
        mock_post.return_value.status_code = 201
        mock_post.return_value.json.return_value = {'key': 'TESTWIS-3'}
        with self._patch_env():
            key, error = jira_create_ticket_api(
                'sum', 'desc',
                assignee_username='dd1cd716-f84c-49de-bda5-8abd4a2fcdb4',
                priority='High',
            )
        self.assertEqual(key, 'TESTWIS-3')
        self.assertIsNone(error)
        fields = mock_post.call_args[1]['json']['fields']
        self.assertEqual(
            fields['assignee']['name'],
            'dd1cd716-f84c-49de-bda5-8abd4a2fcdb4',
        )
        self.assertEqual(fields['priority']['name'], 'High')

    @patch('telemetry.jira.requests.post')
    def test_assignee_and_priority_omitted_when_not_given(self, mock_post):
        mock_post.return_value.status_code = 201
        mock_post.return_value.json.return_value = {'key': 'TESTWIS-4'}
        with self._patch_env():
            key, error = jira_create_ticket_api('sum', 'desc')
        self.assertEqual(key, 'TESTWIS-4')
        fields = mock_post.call_args[1]['json']['fields']
        self.assertNotIn('assignee', fields)
        self.assertNotIn('priority', fields)

    def test_gisc_assignees_cover_expected_giscs(self):
        for name in ('GISC-Toulouse', 'GISC-Casablanca', 'GISC-Washington', 'GISC-Tehran',
                     'ECCC-MSC Global Discovery Catalogue', 'MetOffice/NOAA Global Cache'):
            self.assertIn(name, GISC_TO_ASSIGNEE)
            self.assertTrue(GISC_TO_ASSIGNEE[name]['username'])


class CreateJiraTicketViewTest(SimpleTestCase):
    UUID = '00000000-0000-0000-0000-000000000000'

    def setUp(self):
        self.factory = RequestFactory()
        self.user = MagicMock()
        self.user.id = 1
        self.user.is_authenticated = True

    @patch('telemetry.views.IncidentEvent.objects.create')
    @patch('telemetry.views.jira_create_ticket')
    @patch('django.shortcuts.get_object_or_404')
    def test_success(self, mock_get, mock_jira, mock_event):
        alert = MagicMock()
        alert.display_title = 'HTTP Error: 502 Bad Gateway'
        alert.title = 'HTTP status 502'
        alert.description = 'desc'
        alert.incident_hash = 'abc'
        mock_get.return_value = alert
        mock_jira.return_value = ('TESTWIS-1', None)
        from telemetry.views import create_jira_ticket
        req = self.factory.post('/alert/x/jira/')
        req.user = self.user
        resp = create_jira_ticket(req, self.UUID)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['key'], 'TESTWIS-1')
        self.assertEqual(mock_event.call_args.kwargs['event_type'], 'jira_ticket')

    @patch('telemetry.views.IncidentEvent.objects.create')
    @patch('telemetry.views.jira_create_ticket')
    @patch('django.shortcuts.get_object_or_404')
    def test_uses_submitted_summary_and_description(self, mock_get, mock_jira, mock_event):
        alert = MagicMock()
        alert.display_title = 'HTTP Error: 502 Bad Gateway'
        alert.title = 'HTTP status 502'
        alert.description = 'desc'
        alert.incident_hash = 'abc'
        mock_get.return_value = alert
        mock_jira.return_value = ('TESTWIS-1', None)
        from telemetry.views import create_jira_ticket
        req = self.factory.post(
            '/alert/x/jira/',
            data=json.dumps({'summary': 'Custom summary', 'description': 'Custom desc'}),
            content_type='application/json',
        )
        req.user = self.user
        resp = create_jira_ticket(req, self.UUID)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_jira.call_args.args[0], 'Custom summary')
        self.assertEqual(mock_jira.call_args.args[1], 'Custom desc')

    @patch('telemetry.views.jira_create_ticket')
    @patch('django.shortcuts.get_object_or_404')
    def test_failure_returns_502(self, mock_get, mock_jira):
        alert = MagicMock()
        alert.display_title = 'X'
        alert.title = 'X'
        alert.description = 'desc'
        alert.incident_hash = 'abc'
        mock_get.return_value = alert
        mock_jira.return_value = (None, 'boom')
        from telemetry.views import create_jira_ticket
        req = self.factory.post('/alert/x/jira/')
        req.user = self.user
        resp = create_jira_ticket(req, self.UUID)
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(json.loads(resp.content)['error'], 'boom')

    @patch('telemetry.views.IncidentEvent.objects.create')
    @patch('telemetry.views.jira_create_ticket')
    @patch('django.shortcuts.get_object_or_404')
    def test_sends_assignee_and_priority(self, mock_get, mock_jira, mock_event):
        alert = MagicMock()
        alert.display_title = 'HTTP Error: 502 Bad Gateway'
        alert.title = 'HTTP status 502'
        alert.description = 'desc'
        alert.severity = 'CRITICAL'
        alert.incident_hash = 'abc'
        mock_get.return_value = alert
        mock_jira.return_value = ('TESTWIS-1', None)
        from telemetry.views import create_jira_ticket
        req = self.factory.post(
            '/alert/x/jira/',
            data=json.dumps({
                'summary': 'Custom summary',
                'assignee': 'dd1cd716-f84c-49de-bda5-8abd4a2fcdb4',
            }),
            content_type='application/json',
        )
        req.user = self.user
        resp = create_jira_ticket(req, self.UUID)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            mock_jira.call_args.kwargs['assignee_username'],
            'dd1cd716-f84c-49de-bda5-8abd4a2fcdb4',
        )
        self.assertEqual(mock_jira.call_args.kwargs['priority'], 'High')

    @patch('telemetry.views.IncidentEvent.objects.create')
    @patch('telemetry.views.jira_create_ticket')
    @patch('django.shortcuts.get_object_or_404')
    def test_priority_medium_for_error(self, mock_get, mock_jira, mock_event):
        alert = MagicMock()
        alert.display_title = 'X'
        alert.title = 'X'
        alert.description = 'desc'
        alert.severity = 'ERROR'
        alert.incident_hash = 'abc'
        mock_get.return_value = alert
        mock_jira.return_value = ('TESTWIS-1', None)
        from telemetry.views import create_jira_ticket
        req = self.factory.post(
            '/alert/x/jira/',
            data=json.dumps({'assignee': 'c8ace557-1959-498e-9c86-bab6658f086a'}),
            content_type='application/json',
        )
        req.user = self.user
        resp = create_jira_ticket(req, self.UUID)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_jira.call_args.kwargs['priority'], 'Medium')

    def test_get_returns_405(self):
        from telemetry.views import create_jira_ticket
        req = self.factory.get('/alert/x/jira/')
        req.user = self.user
        resp = create_jira_ticket(req, self.UUID)
        self.assertEqual(resp.status_code, 405)


class EmailSenderTest(SimpleTestCase):
    def _gmail_env(self):
        return {
            'GMAIL_CLIENT_ID': 'client-id',
            'GMAIL_CLIENT_SECRET': 'client-secret',
            'GMAIL_REFRESH_TOKEN': 'refresh-token',
            'SMTP_USER': 'sender@gmail.com',
        }

    def test_not_configured_without_env(self):
        with patch.dict(os.environ, {}, clear=False):
            for key in email_sender_mod.GMAIL_REQUIRED_ENV:
                os.environ.pop(key, None)
            self.assertFalse(email_sender_mod.is_configured())

    def test_configured_when_env_set(self):
        with patch.dict(os.environ, self._gmail_env(), clear=False):
            self.assertTrue(email_sender_mod.is_configured())

    @patch('telemetry.email_sender.requests.post')
    def test_access_token_posts_correct_fields(self, mock_post):
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {'access_token': 'tok'}
        with patch.dict(os.environ, self._gmail_env(), clear=False):
            token = email_sender_mod._access_token()
        self.assertEqual(token, 'tok')
        args, kwargs = mock_post.call_args
        self.assertEqual(args[0], email_sender_mod.GMAIL_TOKEN_URL)
        self.assertEqual(kwargs['data']['grant_type'], 'refresh_token')
        self.assertEqual(kwargs['data']['refresh_token'], 'refresh-token')
        self.assertEqual(kwargs['data']['client_id'], 'client-id')

    @patch('telemetry.email_sender.requests.post')
    def test_send_gmail_uses_raw_message(self, mock_post):
        import base64
        from email import message_from_bytes
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {'access_token': 'tok'}
        send_resp = MagicMock(status_code=200)
        send_resp.json.return_value = {}
        mock_post.side_effect = [token_resp, send_resp]
        with patch.dict(os.environ, self._gmail_env(), clear=False):
            email_sender_send('Alert subject', 'Line one\nLine two', 'to@example.com')
        args, kwargs = mock_post.call_args_list[1]
        self.assertEqual(args[0], email_sender_mod.GMAIL_SEND_URL)
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer tok')
        raw = base64.urlsafe_b64decode(kwargs['json']['raw'].encode('ascii'))
        msg = message_from_bytes(raw)
        self.assertEqual(msg['Subject'], 'Alert subject')
        self.assertEqual(msg['To'], 'to@example.com')
        self.assertEqual(msg['From'], 'WIS2 Monitoring <sender@gmail.com>')
        self.assertEqual(msg.get_payload(decode=True).decode('utf-8').rstrip('\n'), 'Line one\nLine two')

    @patch('telemetry.email_sender.requests.post')
    def test_send_error_raises_readable_message(self, mock_post):
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {'access_token': 'tok'}
        error_resp = MagicMock(status_code=403)
        error_resp.json.return_value = {'error': {'message': 'Daily limit exceeded'}}
        mock_post.side_effect = [token_resp, error_resp]
        with patch.dict(os.environ, self._gmail_env(), clear=False):
            with self.assertRaises(RuntimeError) as ctx:
                email_sender_send('Sub', 'Body', 'to@example.com')
        self.assertIn('403', str(ctx.exception))
        self.assertIn('Daily limit exceeded', str(ctx.exception))

    @patch('telemetry.email_sender.requests.post')
    def test_network_error_wraps_as_runtime_error(self, mock_post):
        mock_post.side_effect = requests.RequestException('connection timeout')
        with patch.dict(os.environ, self._gmail_env(), clear=False):
            with self.assertRaises(RuntimeError) as ctx:
                email_sender_send('Sub', 'Body', 'to@example.com')
        self.assertIn('network error', str(ctx.exception))

    @patch('django.core.mail.send_mail')
    def test_fallback_to_smtp_when_not_configured(self, mock_send_mail):
        with patch.dict(os.environ, {}, clear=False):
            for key in email_sender_mod.GMAIL_REQUIRED_ENV:
                os.environ.pop(key, None)
            email_sender_send('Sub', 'Body', 'to@example.com')
        mock_send_mail.assert_called_once_with(
            subject='Sub',
            message='Body',
            from_email=None,
            recipient_list=['to@example.com'],
            fail_silently=False,
        )


class EmailResponsibleGmailTest(SimpleTestCase):
    UUID = '00000000-0000-0000-0000-000000000000'

    def setUp(self):
        self.factory = RequestFactory()
        self.user = MagicMock()
        self.user.id = 1
        self.user.is_authenticated = True
        self.user.get_full_name.return_value = 'Agent One'

    def _alert(self):
        alert = MagicMock()
        alert.id = 'x'
        alert.node_id = 'urn:wmo:md:node'
        alert.display_title = 'HTTP Error: 502 Bad Gateway'
        alert.title = 'HTTP status 502'
        alert.event_type = 'http'
        alert.event_time = timezone.now()
        alert.description = 'Gateway down'
        alert.errors = None
        alert.tests = None
        alert.summary = None
        alert.ingested_at = timezone.now()
        alert.incident_hash = 'abc'
        return alert

    def _responsible_queryset(self, responsible):
        qs = MagicMock()
        qs.exists.return_value = True
        qs.__iter__.return_value = iter([responsible])
        return qs

    @patch('telemetry.models.NodeResponsible')
    @patch('telemetry.views.IncidentEvent.objects.create')
    @patch('telemetry.views.send_email')
    @patch('django.shortcuts.get_object_or_404')
    def test_success_sends_and_creates_event(self, mock_get, mock_send, mock_event, mock_responsible):
        mock_get.return_value = self._alert()
        responsible = MagicMock()
        responsible.id = 1
        responsible.name = 'Jane Doe'
        responsible.email = 'jane@example.com'
        mock_responsible.objects.filter.return_value = self._responsible_queryset(responsible)
        from telemetry.views import email_responsible
        req = self.factory.post(
            '/alert/x/email/',
            data=json.dumps({'responsible_ids': [1]}),
            content_type='application/json',
        )
        req.user = self.user
        resp = email_responsible(req, self.UUID)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.content)['success'])
        self.assertEqual(mock_send.call_args.kwargs['to_email'], 'jane@example.com')
        body = mock_send.call_args.kwargs['body']
        self.assertIn('NODE: urn:wmo:md:node', body)
        self.assertIn('TITLE: HTTP Error: 502 Bad Gateway | HTTP status 502', body)
        self.assertIn('DESCRIPTION: Gateway down', body)
        self.assertIn('EVENT_TIME:', body)
        self.assertIn('INGESTED_AT:', body)
        self.assertIn('AGENT NAME: Agent One', body)
        self.assertIn('AGENT NOTE: ', body)
        self.assertNotIn('RESPONSIBLE:', body)
        self.assertEqual(mock_event.call_args.kwargs['event_type'], 'email_sent')

    @patch('telemetry.models.NodeResponsible')
    @patch('telemetry.views.IncidentEvent.objects.create')
    @patch('telemetry.views.send_email')
    @patch('django.shortcuts.get_object_or_404')
    def test_failure_returns_502_with_message(self, mock_get, mock_send, mock_event, mock_responsible):
        mock_get.return_value = self._alert()
        responsible = MagicMock()
        responsible.id = 1
        responsible.name = 'Jane Doe'
        responsible.email = 'jane@example.com'
        mock_responsible.objects.filter.return_value = self._responsible_queryset(responsible)
        mock_send.side_effect = RuntimeError('Gmail API error 403: Daily limit exceeded')
        from telemetry.views import email_responsible
        req = self.factory.post(
            '/alert/x/email/',
            data=json.dumps({'responsible_ids': [1]}),
            content_type='application/json',
        )
        req.user = self.user
        resp = email_responsible(req, self.UUID)
        self.assertEqual(resp.status_code, 502)
        self.assertIn('Daily limit exceeded', json.loads(resp.content)['error'])
        self.assertFalse(mock_event.called)

    @patch('django.shortcuts.get_object_or_404')
    def test_get_returns_405(self, mock_get):
        from telemetry.views import email_responsible
        req = self.factory.get('/alert/x/email/')
        req.user = self.user
        resp = email_responsible(req, self.UUID)
        self.assertEqual(resp.status_code, 405)
