import hashlib
import io
import json
import os
import tempfile
import time as _time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import requests

from django.test import SimpleTestCase, RequestFactory
from django.core.management import call_command
from django.utils import timezone

from telemetry.views import _is_admin, get_events_for_user, apply_window, get_type_choices, expand_type_labels, apply_filters, apply_keyword_filter
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

    @patch('telemetry.views.get_events_for_user')
    def test_api_events_per_day_bad_days(self, mock_get_events):
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs
        mock_qs.annotate.return_value = mock_qs
        mock_qs.values.return_value = mock_qs
        mock_qs.__iter__ = lambda s: iter([])
        mock_get_events.return_value = mock_qs
        from telemetry.views import api_events_per_day
        request = self.factory.get('/api/events/per-day/', {'days': 'abc'})
        request.user = self.user
        response = api_events_per_day(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['granularity'], 'day')

    @patch('telemetry.views.get_events_for_user')
    def test_api_events_per_day_valid_params(self, mock_get_events):
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs
        mock_qs.annotate.return_value = mock_qs
        mock_qs.values.return_value = mock_qs
        mock_qs.__iter__ = lambda s: iter([])
        mock_get_events.return_value = mock_qs
        from telemetry.views import api_events_per_day
        request = self.factory.get('/api/events/per-day/', {'days': '7'})
        request.user = self.user
        response = api_events_per_day(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(len(data['labels']), 7)
        self.assertEqual(data['granularity'], 'day')

    @patch('telemetry.views.get_events_for_user')
    def test_api_events_per_day_12h_is_hourly(self, mock_get_events):
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs
        mock_qs.annotate.return_value = mock_qs
        mock_qs.values.return_value = mock_qs
        mock_qs.__iter__ = lambda s: iter([])
        mock_get_events.return_value = mock_qs
        from telemetry.views import api_events_per_day
        request = self.factory.get('/api/events/per-day/', {'window': '12h', 'days': '30'})
        request.user = self.user
        response = api_events_per_day(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertEqual(data['granularity'], 'hour')
        mock_qs.filter.assert_called()

    @patch('telemetry.views.get_events_for_user')
    def test_api_events_per_day_custom_invalid_falls_back_to_all(self, mock_get_events):
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs
        mock_qs.annotate.return_value = mock_qs
        mock_qs.values.return_value = mock_qs
        mock_qs.__iter__ = lambda s: iter([])
        mock_get_events.return_value = mock_qs
        from telemetry.views import api_events_per_day
        request = self.factory.get('/api/events/per-day/', {'window': 'custom', 'from': 'garbage', 'to': '2026-08-03T00:00'})
        request.user = self.user
        response = api_events_per_day(request)
        data = json.loads(response.content)
        self.assertEqual(data['granularity'], 'day')

    @patch('telemetry.views.get_events_for_user')
    def test_api_events_per_day_custom_inverted_falls_back_to_all(self, mock_get_events):
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs
        mock_qs.annotate.return_value = mock_qs
        mock_qs.values.return_value = mock_qs
        mock_qs.__iter__ = lambda s: iter([])
        mock_get_events.return_value = mock_qs
        from telemetry.views import api_events_per_day
        request = self.factory.get('/api/events/per-day/', {'window': 'custom', 'from': '2026-08-05T00:00', 'to': '2026-08-03T00:00'})
        request.user = self.user
        response = api_events_per_day(request)
        data = json.loads(response.content)
        self.assertEqual(data['granularity'], 'day')

    @patch('telemetry.views.get_events_for_user')
    def test_api_events_per_day_custom_24h_is_hourly(self, mock_get_events):
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs
        mock_qs.annotate.return_value = mock_qs
        mock_qs.values.return_value = mock_qs
        mock_qs.__iter__ = lambda s: iter([])
        mock_get_events.return_value = mock_qs
        from telemetry.views import api_events_per_day
        request = self.factory.get('/api/events/per-day/', {'window': 'custom', 'from': '2026-08-01T00:00', 'to': '2026-08-02T00:00'})
        request.user = self.user
        response = api_events_per_day(request)
        data = json.loads(response.content)
        self.assertEqual(data['granularity'], 'hour')

    @patch('telemetry.views.get_events_for_user')
    def test_api_events_per_day_custom_long_is_daily(self, mock_get_events):
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs
        mock_qs.annotate.return_value = mock_qs
        mock_qs.values.return_value = mock_qs
        mock_qs.__iter__ = lambda s: iter([])
        mock_get_events.return_value = mock_qs
        from telemetry.views import api_events_per_day
        request = self.factory.get('/api/events/per-day/', {'window': 'custom', 'from': '2026-08-01T00:00', 'to': '2026-08-03T00:00'})
        request.user = self.user
        response = api_events_per_day(request)
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


class EventSearchTest(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = MagicMock()
        self.user.is_authenticated = True
        self.user.id = 1

    def test_requires_login(self):
        from telemetry.views import event_search
        request = self.factory.get('/api/event-search/?q=meteo')
        request.user = MagicMock(is_authenticated=False)
        response = event_search(request)
        self.assertEqual(response.status_code, 302)

    @patch('telemetry.views.apply_keyword_filter')
    @patch('telemetry.views.get_events_for_user')
    def test_empty_q_returns_false(self, mock_get_events, mock_kw):
        mock_get_events.return_value = MagicMock()
        from telemetry.views import event_search
        request = self.factory.get('/api/event-search/')
        request.user = self.user
        response = event_search(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertFalse(data['found'])
        self.assertEqual(data['count'], 0)
        mock_kw.assert_not_called()

    @patch('telemetry.views.apply_keyword_filter')
    @patch('telemetry.views.get_events_for_user')
    def test_match_returns_found_true(self, mock_get_events, mock_kw):
        mock_qs = MagicMock()
        mock_qs.count.return_value = 3
        mock_kw.return_value = mock_qs
        mock_get_events.return_value = MagicMock()
        from telemetry.views import event_search
        request = self.factory.get('/api/event-search/?q=meteo')
        request.user = self.user
        response = event_search(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['found'])
        self.assertEqual(data['count'], 3)

    @patch('telemetry.views.apply_keyword_filter')
    @patch('telemetry.views.get_events_for_user')
    def test_no_match_returns_found_false(self, mock_get_events, mock_kw):
        mock_qs = MagicMock()
        mock_qs.count.return_value = 0
        mock_kw.return_value = mock_qs
        mock_get_events.return_value = MagicMock()
        from telemetry.views import event_search
        request = self.factory.get('/api/event-search/?q=nope')
        request.user = self.user
        response = event_search(request)
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
            'event_type__icontains', 'source__icontains', 'subject__name__icontains',
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


class GetEventsForUserTest(SimpleTestCase):
    def test_admin_gets_all(self):
        user = MagicMock(spec=['id'])
        user.id = 1
        with patch('telemetry.views._is_admin', return_value=True):
            with patch('telemetry.views.Event') as MockEvent:
                result = get_events_for_user(user)
                MockEvent.objects.all.assert_called_once()
                self.assertEqual(result, MockEvent.objects.all.return_value)

    def test_regular_with_profile(self):
        user = MagicMock(spec=['id', 'profile'])
        user.id = 1
        profile = MagicMock()
        profile.allowed_subjects.all.return_value = ['subject1']
        user.profile = profile
        with patch('telemetry.views._is_admin', return_value=False):
            with patch('telemetry.views.Event') as MockEvent:
                result = get_events_for_user(user)
                MockEvent.objects.filter.assert_called_once_with(subject__in=['subject1'])
                self.assertEqual(result, MockEvent.objects.filter.return_value)

    def test_regular_no_profile_returns_empty(self):
        user = MagicMock(spec=['id'])
        user.id = 1
        with patch('telemetry.views._is_admin', return_value=False):
            with patch('telemetry.views.Event') as MockEvent:
                result = get_events_for_user(user)
                MockEvent.objects.none.assert_called_once()
                self.assertEqual(result, MockEvent.objects.none.return_value)


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

    def test_standalone_eof(self):
        self.assertEqual(
            _compute_display_title(
                'wis2-gdc.weather.gc.ca',
                'Get "https://wis2-gdc.weather.gc.ca:443/wis2-gdc-metrics.txt": EOF,collection time 2026-08-10T15:44:23Z',
            ),
            'Network Termination: unexpected EOF',
        )

    def test_eof_no_false_positive_on_embedded_text(self):
        self.assertEqual(
            _compute_display_title('geofence service', 'fetching geofence config'),
            'geofence service',
        )

    def test_no_route_to_host(self):
        self.assertEqual(
            _compute_display_title(
                'wis2.dwd.de',
                'Get "https://wis2.dwd.de:443/metrics/gc_metrics.txt": '
                'dial tcp 141.38.3.181:443: connect: no route to host,collection time 2026-08-06T11:32:02Z',
            ),
            'Network Error: no route to host',
        )

    def test_no_route_to_host_not_swallowed_by_unknown_rule(self):
        self.assertEqual(
            _compute_display_title(
                'wis2.dwd.de',
                'dial tcp 141.38.3.181:443: connect: no route to host,collection time 2026-08-06T11:32:02Z',
            ),
            'Network Error: no route to host',
        )

    def test_collection_time_only_unknown(self):
        self.assertEqual(
            _compute_display_title('gc.wis.cma.cn', ',collection time 0001-01-01T00:00:00Z'),
            'Unknown Error: no details',
        )

    def test_empty_description_keeps_raw_title(self):
        self.assertEqual(_compute_display_title('gc.wis.cma.cn', ''), 'gc.wis.cma.cn')

    def test_unknown_does_not_override_target(self):
        self.assertEqual(
            _compute_display_title('Target gc.wis.cma.cn:443 is down', ',collection time 0001-01-01T00:00:00Z'),
            'Target is down',
        )

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
            'type': 'ca.meteocean.wis2.event',
            'source': 'test/source',
            'subject': 'node-a',
            'time': '2026-01-01T00:00:00Z',
            'data': {
                'severity': 'WARNING',
                'content': {
                    'title': 'Test event',
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
        self.assertEqual(result[2], 'ca.meteocean.wis2.event')
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

    def test_incident_hash_differs_by_subject(self):
        r1 = parse_wmem_record(self._make_payload())
        r2 = parse_wmem_record(self._make_payload(subject='node-b'))
        self.assertNotEqual(r1[15], r2[15])

    def test_display_title_computed(self):
        result = parse_wmem_record(self._make_payload())
        self.assertEqual(result[14], 'Test event')

    def test_malformed_returns_none(self):
        result = parse_wmem_record({'id': 123, 'data': 'not a dict'})
        self.assertIsNone(result)


class PurgeEventsCommandTest(SimpleTestCase):
    def _configure_batches(self, mock_event, batches):
        mock_filter = mock_event.objects.filter.return_value
        mock_filter.values_list.return_value.__getitem__.side_effect = batches
        return mock_filter

    @patch('telemetry.management.commands.purge_events.Event')
    @patch('telemetry.management.commands.purge_events.IncidentActivity')
    @patch('telemetry.management.commands.purge_events.get_retention_days')
    def test_persistence_mode_does_nothing(self, mock_days, mock_incident, mock_event):
        mock_days.return_value = None
        out = io.StringIO()
        call_command('purge_events', stdout=out)
        self.assertIn('Persistence mode', out.getvalue())
        mock_event.objects.filter.assert_not_called()
        mock_incident.objects.filter.assert_not_called()

    @patch('telemetry.management.commands.purge_events.Event')
    @patch('telemetry.management.commands.purge_events.IncidentActivity')
    @patch('telemetry.management.commands.purge_events.transaction')
    @patch('telemetry.management.commands.purge_events.get_retention_days')
    def test_purges_in_batches_and_cascades_history(self, mock_days, mock_txn, mock_incident, mock_event):
        mock_days.return_value = 30
        batch1 = [str(uuid.uuid4()) for _ in range(5000)]
        batch2 = [str(uuid.uuid4())]
        mock_filter = self._configure_batches(mock_event, [batch1, batch2, []])
        mock_incident.objects.filter.return_value.delete.return_value = (1, {})
        mock_filter.delete.side_effect = [(3, {}), (1, {})]

        out = io.StringIO()
        call_command('purge_events', stdout=out)

        self.assertEqual(mock_incident.objects.filter.call_count, 2)
        self.assertEqual(mock_filter.delete.call_count, 2)
        self.assertIn('Done: 4 event(s) purged.', out.getvalue())

    @patch('telemetry.management.commands.purge_events.Event')
    @patch('telemetry.management.commands.purge_events.IncidentActivity')
    @patch('telemetry.management.commands.purge_events.get_retention_days')
    def test_dry_run_deletes_nothing(self, mock_days, mock_incident, mock_event):
        mock_days.return_value = 30
        mock_filter = mock_event.objects.filter.return_value
        mock_filter.count.return_value = 10

        out = io.StringIO()
        call_command('purge_events', stdout=out, dry_run=True)

        self.assertIn('would purge 10 event(s) (dry-run)', out.getvalue())
        self.assertIn('would be purged (dry-run)', out.getvalue())
        mock_filter.values_list.assert_not_called()
        mock_incident.objects.filter.assert_not_called()

    @patch('telemetry.management.commands.purge_events.Event')
    @patch('telemetry.management.commands.purge_events.IncidentActivity')
    @patch('telemetry.management.commands.purge_events.transaction')
    @patch('telemetry.management.commands.purge_events.get_retention_days')
    def test_days_override_respected(self, mock_days, mock_txn, mock_incident, mock_event):
        mock_days.return_value = None
        batch = [str(uuid.uuid4())]
        mock_filter = self._configure_batches(mock_event, [batch, []])
        mock_filter.delete.side_effect = [(1, {})]

        out = io.StringIO()
        call_command('purge_events', stdout=out, days=30)

        self.assertIn('purging events ingested before', out.getvalue())
        self.assertIn('Done: 1 event(s) purged.', out.getvalue())

    def test_get_retention_days_returns_none_without_policy(self):
        from telemetry.models import get_retention_days
        with patch('telemetry.models.EventRetentionPolicy.objects', new=MagicMock()) as mock_objects:
            mock_objects.first.return_value = None
            self.assertIsNone(get_retention_days())

    def test_get_retention_days_inactive_ignores_days(self):
        from telemetry.models import get_retention_days
        with patch('telemetry.models.EventRetentionPolicy.objects', new=MagicMock()) as mock_objects:
            obj = MagicMock()
            obj.ttl_active = False
            obj.retention_days = 30
            mock_objects.first.return_value = obj
            self.assertIsNone(get_retention_days())

    def test_get_retention_days_active_with_days(self):
        from telemetry.models import get_retention_days
        with patch('telemetry.models.EventRetentionPolicy.objects', new=MagicMock()) as mock_objects:
            obj = MagicMock()
            obj.ttl_active = True
            obj.retention_days = 30
            mock_objects.first.return_value = obj
            self.assertEqual(get_retention_days(), 30)

    def test_get_retention_days_active_without_days(self):
        from telemetry.models import get_retention_days
        with patch('telemetry.models.EventRetentionPolicy.objects', new=MagicMock()) as mock_objects:
            obj = MagicMock()
            obj.ttl_active = True
            obj.retention_days = None
            mock_objects.first.return_value = obj
            self.assertIsNone(get_retention_days())

    def test_clean_requires_days_when_active(self):
        from django.core.exceptions import ValidationError
        from telemetry.models import EventRetentionPolicy
        policy = EventRetentionPolicy(ttl_active=True, retention_days=None)
        with self.assertRaises(ValidationError):
            policy.clean()

    def test_clean_allows_active_with_days_and_inactive_without(self):
        from telemetry.models import EventRetentionPolicy
        EventRetentionPolicy(ttl_active=True, retention_days=30).clean()
        EventRetentionPolicy(ttl_active=False, retention_days=None).clean()
        EventRetentionPolicy(ttl_active=False, retention_days=30).clean()


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
        self.assertEqual(mock_call.call_args[0][0], 'purge_events')
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
    def _event(self, display=None, title=None):
        event = MagicMock()
        event.display_title = display
        event.title = title
        return event

    def test_same_titles_returns_single(self):
        self.assertEqual(jira_build_summary(self._event('X', 'X')), 'X')

    def test_different_titles_concatenated(self):
        self.assertEqual(
            jira_build_summary(self._event('Maintenance', 'CMA Global Monitor')),
            'Maintenance | CMA Global Monitor',
        )

    def test_only_title(self):
        self.assertEqual(jira_build_summary(self._event(None, 'Some title')), 'Some title')

    def test_none_falls_back(self):
        self.assertEqual(jira_build_summary(self._event(None, None)), 'Untitled Event')


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

    @patch('telemetry.views.IncidentActivity.objects.create')
    @patch('telemetry.views.jira_create_ticket')
    @patch('django.shortcuts.get_object_or_404')
    def test_success(self, mock_get, mock_jira, mock_event):
        event = MagicMock()
        event.display_title = 'HTTP Error: 502 Bad Gateway'
        event.title = 'HTTP status 502'
        event.description = 'desc'
        event.incident_hash = 'abc'
        mock_get.return_value = event
        mock_jira.return_value = ('TESTWIS-1', None)
        from telemetry.views import create_jira_ticket
        req = self.factory.post('/event/x/jira/')
        req.user = self.user
        resp = create_jira_ticket(req, self.UUID)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['key'], 'TESTWIS-1')
        self.assertEqual(mock_event.call_args.kwargs['event_type'], 'jira_ticket')

    @patch('telemetry.views.IncidentActivity.objects.create')
    @patch('telemetry.views.jira_create_ticket')
    @patch('django.shortcuts.get_object_or_404')
    def test_uses_submitted_summary_and_description(self, mock_get, mock_jira, mock_event):
        event = MagicMock()
        event.display_title = 'HTTP Error: 502 Bad Gateway'
        event.title = 'HTTP status 502'
        event.description = 'desc'
        event.incident_hash = 'abc'
        mock_get.return_value = event
        mock_jira.return_value = ('TESTWIS-1', None)
        from telemetry.views import create_jira_ticket
        req = self.factory.post(
            '/event/x/jira/',
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
        event = MagicMock()
        event.display_title = 'X'
        event.title = 'X'
        event.description = 'desc'
        event.incident_hash = 'abc'
        mock_get.return_value = event
        mock_jira.return_value = (None, 'boom')
        from telemetry.views import create_jira_ticket
        req = self.factory.post('/event/x/jira/')
        req.user = self.user
        resp = create_jira_ticket(req, self.UUID)
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(json.loads(resp.content)['error'], 'boom')

    @patch('telemetry.views.IncidentActivity.objects.create')
    @patch('telemetry.views.jira_create_ticket')
    @patch('django.shortcuts.get_object_or_404')
    def test_sends_assignee_and_priority(self, mock_get, mock_jira, mock_event):
        event = MagicMock()
        event.display_title = 'HTTP Error: 502 Bad Gateway'
        event.title = 'HTTP status 502'
        event.description = 'desc'
        event.severity = 'CRITICAL'
        event.incident_hash = 'abc'
        mock_get.return_value = event
        mock_jira.return_value = ('TESTWIS-1', None)
        from telemetry.views import create_jira_ticket
        req = self.factory.post(
            '/event/x/jira/',
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

    @patch('telemetry.views.IncidentActivity.objects.create')
    @patch('telemetry.views.jira_create_ticket')
    @patch('django.shortcuts.get_object_or_404')
    def test_priority_medium_for_error(self, mock_get, mock_jira, mock_event):
        event = MagicMock()
        event.display_title = 'X'
        event.title = 'X'
        event.description = 'desc'
        event.severity = 'ERROR'
        event.incident_hash = 'abc'
        mock_get.return_value = event
        mock_jira.return_value = ('TESTWIS-1', None)
        from telemetry.views import create_jira_ticket
        req = self.factory.post(
            '/event/x/jira/',
            data=json.dumps({'assignee': 'c8ace557-1959-498e-9c86-bab6658f086a'}),
            content_type='application/json',
        )
        req.user = self.user
        resp = create_jira_ticket(req, self.UUID)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(mock_jira.call_args.kwargs['priority'], 'Medium')

    def test_get_returns_405(self):
        from telemetry.views import create_jira_ticket
        req = self.factory.get('/event/x/jira/')
        req.user = self.user
        resp = create_jira_ticket(req, self.UUID)
        self.assertEqual(resp.status_code, 405)


class ExplainEventTest(SimpleTestCase):
    UUID = '00000000-0000-0000-0000-000000000000'

    def setUp(self):
        self.factory = RequestFactory()
        self.user = MagicMock()
        self.user.id = 1
        self.user.is_authenticated = True

    def _event(self):
        event = MagicMock()
        event.display_title = 'HTTP Error: 502 Bad Gateway'
        event.title = 'HTTP status 502'
        event.severity = 'ERROR'
        event.event_type = 'com.wmo.wis.monitor.http.error'
        event.subtype = None
        event.source = 'urn:wmo:md:not-a-gisc'
        event.subject_id = None
        event.channel = None
        event.event_time = datetime(2026, 8, 23, 12, 0, 0)
        event.description = 'Something broke.'
        event.incident_hash = None
        event.tests = None
        event.summary = None
        event.errors = None
        event.raw_json = None
        return event

    def _post(self, body=None, configured=True, reply='Here is the explanation.', error=None):
        event = self._event()
        with patch('telemetry.views.llm_is_configured', return_value=configured), \
                patch('telemetry.views.llm_chat', return_value=(reply, error)) as mock_chat, \
                patch('django.shortcuts.get_object_or_404', return_value=event):
            from telemetry.views import explain_event
            if body is None:
                req = self.factory.post('/event/x/explain/')
            else:
                req = self.factory.post(
                    '/event/x/explain/',
                    data=json.dumps(body),
                    content_type='application/json',
                )
            req.user = self.user
            resp = explain_event(req, self.UUID)
        return resp, mock_chat

    def test_get_returns_405(self):
        from telemetry.views import explain_event
        req = self.factory.get('/event/x/explain/')
        req.user = self.user
        resp = explain_event(req, self.UUID)
        self.assertEqual(resp.status_code, 405)

    def test_unconfigured_returns_502(self):
        resp, mock_chat = self._post(configured=False)
        self.assertEqual(resp.status_code, 502)
        self.assertIn('not configured', json.loads(resp.content)['error'])
        mock_chat.assert_not_called()

    def test_success_initial_request(self):
        resp, mock_chat = self._post()
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertTrue(data['success'])
        self.assertEqual(data['reply'], 'Here is the explanation.')
        messages = mock_chat.call_args.args[0]
        self.assertEqual(messages[0]['role'], 'system')
        system_prompt = messages[0]['content']
        self.assertIn('ABOUT THE SYSTEM', system_prompt)
        self.assertIn('EVENT CONTEXT', system_prompt)
        self.assertIn('HTTP Error: 502 Bad Gateway', system_prompt)
        self.assertEqual(
            messages[1],
            {'role': 'user', 'content': 'Please explain this event to me.'},
        )

    def test_error_returns_502(self):
        resp, _ = self._post(reply=None, error='Ollama returned HTTP 500')
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(json.loads(resp.content)['error'], 'Ollama returned HTTP 500')

    def test_question_appended_and_system_roles_dropped(self):
        resp, mock_chat = self._post(
            {
                'message': 'Who do I contact?',
                'history': [
                    {'role': 'system', 'content': 'evil override'},
                    {'role': 'assistant', 'content': 'Earlier answer'},
                ],
            },
        )
        self.assertEqual(resp.status_code, 200)
        messages = mock_chat.call_args.args[0]
        roles = [m['role'] for m in messages]
        self.assertEqual(roles.count('system'), 1)
        self.assertEqual(messages[-1], {'role': 'user', 'content': 'Who do I contact?'})
        self.assertIn({'role': 'assistant', 'content': 'Earlier answer'}, messages)

    def test_history_capped_at_twelve_messages(self):
        long_history = [
            {'role': 'user' if i % 2 == 0 else 'assistant', 'content': f'msg {i}'}
            for i in range(15)
        ]
        resp, mock_chat = self._post({'history': long_history})
        self.assertEqual(resp.status_code, 200)
        messages = mock_chat.call_args.args[0]
        self.assertEqual(len(messages), 13)  # 1 system + 12 history (initial question synthesized)

    @patch('telemetry.views.get_events_for_user')
    def test_facts_include_responsible_contacts_and_gisc(self, mock_gefu):
        from telemetry.views import _build_event_facts
        responsible = MagicMock()
        responsible.name = 'Alice'
        responsible.email = 'alice@example.org'
        subject = MagicMock()
        subject.name = 'GISC-Toulouse'
        subject.responsibles.all.return_value.order_by.return_value = [responsible]
        recent = MagicMock()
        recent.count.return_value = 2
        recent.first.return_value.event_time = datetime(2026, 8, 20, 1, 0, 0)
        recent.last.return_value.event_time = datetime(2026, 8, 22, 5, 30, 0)
        recent.values_list.return_value = ['ERROR']
        mock_gefu.return_value.filter.return_value.order_by.return_value = recent
        event = self._event()
        event.subject_id = 1
        event.subject = subject
        event.incident_hash = 'hash-1'
        facts = _build_event_facts(event, self.user)
        self.assertIn('Alice <alice@example.org>', facts)
        self.assertIn('transmet@meteo.fr', facts)
        self.assertIn('occurred 2 time(s)', facts)
        self.assertIn('severities observed: ERROR', facts)

    @patch('telemetry.views.kb.find_metadata_id', return_value=None)
    @patch('telemetry.views.kb.retrieve')
    def test_knowledge_excerpts_included(self, mock_retrieve, mock_meta):
        from telemetry import kb as kb_mod
        mock_retrieve.return_value = [{
            'id': 'doc:ghosting:1', 'source': 'doc', 'ref_id': 'ghosting',
            'title': 'The Ghosting Phenomenon',
            'text': 'Data looks available but downloads fail.',
            'score': 0.9,
        }]
        resp, mock_chat = self._post()
        self.assertEqual(resp.status_code, 200)
        prompt = mock_chat.call_args.args[0][0]['content']
        self.assertIn('KNOWLEDGE BASE EXCERPTS', prompt)
        self.assertIn('The Ghosting Phenomenon', prompt)
        self.assertLess(prompt.index('KNOWLEDGE BASE'), prompt.index('EVENT CONTEXT'))
        mock_retrieve.assert_called_once()
        self.assertEqual(mock_retrieve.call_args.kwargs['k'], kb_mod.RETRIEVE_TOP_K)

    @patch('telemetry.views.kb.find_by_ref')
    @patch('telemetry.views.kb.find_metadata_id', return_value='urn:wmo:md:x:y')
    @patch('telemetry.views.kb.retrieve', return_value=[])
    def test_metadata_record_pinned(self, mock_retrieve, mock_meta, mock_find):
        mock_find.return_value = {
            'id': 'gdc:urn:wmo:md:x:y', 'source': 'gdc',
            'ref_id': 'urn:wmo:md:x:y', 'title': 'Dataset X',
            'text': 'Identifier: urn:wmo:md:x:y',
        }
        resp, mock_chat = self._post()
        self.assertEqual(resp.status_code, 200)
        prompt = mock_chat.call_args.args[0][0]['content']
        self.assertIn('[gdc]', prompt)
        self.assertIn('urn:wmo:md:x:y', prompt)

    @patch('telemetry.views.kb.find_metadata_id', return_value=None)
    @patch('telemetry.views.kb.retrieve', side_effect=RuntimeError('boom'))
    def test_kb_failure_degrades_gracefully(self, mock_retrieve, mock_meta):
        resp, mock_chat = self._post()
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.content)['success'])
        self.assertNotIn('KNOWLEDGE BASE EXCERPTS', mock_chat.call_args.args[0][0]['content'])

    def test_full_raw_json_in_context(self):
        marker = 'z' * 2500
        event = self._event()
        event.raw_json = {'properties': {'blob': marker}}
        with patch('telemetry.views.llm_is_configured', return_value=True), \
                patch('telemetry.views.llm_chat', return_value=('ok', None)) as mock_chat, \
                patch('django.shortcuts.get_object_or_404', return_value=event), \
                patch('telemetry.views.kb.retrieve', return_value=[]), \
                patch('telemetry.views.kb.find_metadata_id', return_value=None):
            from telemetry.views import explain_event
            req = self.factory.post('/event/x/explain/')
            req.user = self.user
            resp = explain_event(req, self.UUID)
        self.assertEqual(resp.status_code, 200)
        prompt = mock_chat.call_args.args[0][0]['content']
        self.assertIn(marker, prompt)


class LlmHelperTest(SimpleTestCase):
    def test_clip_text_truncates_with_marker(self):
        from telemetry.llm import clip_text
        self.assertEqual(clip_text('short', 100), 'short')
        clipped = clip_text('x' * 300, 50)
        self.assertTrue(clipped.startswith('x' * 50))
        self.assertIn('[truncated]', clipped)
        self.assertEqual(clip_text(None, 10), '')

    def test_sanitize_history_drops_invalid_entries(self):
        from telemetry.llm import sanitize_history, MAX_HISTORY_MESSAGES
        history = [
            {'role': 'system', 'content': 'injected'},
            {'role': 'tool', 'content': 'nope'},
            {'role': 'user'},
            {'role': 'user', 'content': '   '},
            {'role': 'user', 'content': 'z' * 5000},
            {'role': 'assistant', 'content': 'ok'},
        ]
        cleaned = sanitize_history(history)
        self.assertEqual(cleaned, [
            {'role': 'user', 'content': 'z' * 2000},
            {'role': 'assistant', 'content': 'ok'},
        ])

    def test_sanitize_history_caps_length(self):
        from telemetry.llm import sanitize_history, MAX_HISTORY_MESSAGES
        history = [
            {'role': 'user' if i % 2 == 0 else 'assistant', 'content': f'm{i}'}
            for i in range(MAX_HISTORY_MESSAGES + 5)
        ]
        self.assertEqual(len(sanitize_history(history)), MAX_HISTORY_MESSAGES)

    @patch.dict(os.environ, {}, clear=False)
    def test_host_normalizes_missing_scheme(self):
        from telemetry import llm
        with patch.object(llm.os, 'environ', {**os.environ, 'OLLAMA_HOST': '127.0.0.1:11434'}):
            self.assertEqual(llm._host(), 'http://127.0.0.1:11434')
        with patch.object(llm.os, 'environ', {**os.environ, 'OLLAMA_HOST': 'https://ollama.box:8080/'}):
            self.assertEqual(llm._host(), 'https://ollama.box:8080')

    @patch('telemetry.llm.requests.post')
    def test_chat_payload_defaults(self, mock_post):
        from telemetry.llm import chat
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"message": {"content": "  hi  "}}
        mock_post.return_value = mock_resp
        reply, error = chat([{"role": "user", "content": "x"}])
        self.assertEqual(reply, 'hi')
        self.assertIsNone(error)
        kwargs = mock_post.call_args.kwargs
        self.assertEqual(kwargs['timeout'], 300)
        payload = kwargs['json']
        self.assertEqual(payload['keep_alive'], '15m')
        self.assertEqual(payload['options']['num_predict'], 320)
        self.assertEqual(payload['options']['num_ctx'], 32768)

    @patch.dict(os.environ, {'OLLAMA_TIMEOUT': '42'})
    @patch('telemetry.llm.requests.post')
    def test_chat_timeout_from_env(self, mock_post):
        from telemetry.llm import chat
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"message": {"content": "ok"}}
        mock_post.return_value = mock_resp
        chat([{"role": "user", "content": "x"}])
        self.assertEqual(mock_post.call_args.kwargs['timeout'], 42)


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
    @patch('django.core.mail.send_mail', side_effect=RuntimeError('smtp down'))
    def test_send_gmail_uses_raw_message(self, mock_send_mail, mock_post):
        import base64
        from email import message_from_bytes
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {'access_token': 'tok'}
        send_resp = MagicMock(status_code=200)
        send_resp.json.return_value = {}
        mock_post.side_effect = [token_resp, send_resp]
        with patch.dict(os.environ, self._gmail_env(), clear=False):
            email_sender_send('Event subject', 'Line one\nLine two', 'to@example.com')
        args, kwargs = mock_post.call_args_list[1]
        self.assertEqual(args[0], email_sender_mod.GMAIL_SEND_URL)
        self.assertEqual(kwargs['headers']['Authorization'], 'Bearer tok')
        raw = base64.urlsafe_b64decode(kwargs['json']['raw'].encode('ascii'))
        msg = message_from_bytes(raw)
        self.assertEqual(msg['Subject'], 'Event subject')
        self.assertEqual(msg['To'], 'to@example.com')
        self.assertEqual(msg['From'], 'WIS2 Monitoring <sender@gmail.com>')
        self.assertEqual(msg.get_payload(decode=True).decode('utf-8').rstrip('\n'), 'Line one\nLine two')

    @patch('telemetry.email_sender.requests.post')
    @patch('django.core.mail.send_mail', side_effect=RuntimeError('smtp down'))
    def test_send_error_raises_readable_message(self, mock_send_mail, mock_post):
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
        self.assertIn('smtp down', str(ctx.exception))

    @patch('telemetry.email_sender.requests.post')
    @patch('django.core.mail.send_mail', side_effect=RuntimeError('smtp down'))
    def test_network_error_wraps_as_runtime_error(self, mock_send_mail, mock_post):
        mock_post.side_effect = requests.RequestException('connection timeout')
        with patch.dict(os.environ, self._gmail_env(), clear=False):
            with self.assertRaises(RuntimeError) as ctx:
                email_sender_send('Sub', 'Body', 'to@example.com')
        self.assertIn('network error', str(ctx.exception))
        self.assertIn('smtp down', str(ctx.exception))

    @patch('django.core.mail.send_mail')
    def test_smtp_used_when_gmail_not_configured(self, mock_send_mail):
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

    @patch('telemetry.email_sender.requests.post')
    @patch('django.core.mail.send_mail')
    def test_smtp_success_does_not_call_gmail(self, mock_send_mail, mock_post):
        with patch.dict(os.environ, self._gmail_env(), clear=False):
            email_sender_send('Sub', 'Body', 'to@example.com')
        mock_send_mail.assert_called_once()
        mock_post.assert_not_called()

    @patch('telemetry.email_sender.requests.post')
    @patch('django.core.mail.send_mail', side_effect=RuntimeError('smtp down'))
    def test_smtp_failure_falls_back_to_gmail_success(self, mock_send_mail, mock_post):
        token_resp = MagicMock(status_code=200)
        token_resp.json.return_value = {'access_token': 'tok'}
        send_resp = MagicMock(status_code=200)
        send_resp.json.return_value = {}
        mock_post.side_effect = [token_resp, send_resp]
        with patch.dict(os.environ, self._gmail_env(), clear=False):
            email_sender_send('Sub', 'Body', 'to@example.com')
        self.assertEqual(mock_post.call_args_list[-1].args[0], email_sender_mod.GMAIL_SEND_URL)

    @patch('telemetry.email_sender.requests.post')
    @patch('django.core.mail.send_mail', side_effect=RuntimeError('smtp down'))
    def test_smtp_failure_without_gmail_config_raises(self, mock_send_mail, mock_post):
        with patch.dict(os.environ, {}, clear=False):
            for key in email_sender_mod.GMAIL_REQUIRED_ENV:
                os.environ.pop(key, None)
            with self.assertRaises(RuntimeError) as ctx:
                email_sender_send('Sub', 'Body', 'to@example.com')
        self.assertIn('smtp down', str(ctx.exception))
        self.assertIn('not configured', str(ctx.exception))
        mock_post.assert_not_called()


class EmailResponsibleGmailTest(SimpleTestCase):
    UUID = '00000000-0000-0000-0000-000000000000'

    def setUp(self):
        self.factory = RequestFactory()
        self.user = MagicMock()
        self.user.id = 1
        self.user.is_authenticated = True
        self.user.get_full_name.return_value = 'Agent One'

    def _event(self):
        event = MagicMock()
        event.id = 'x'
        event.subject_id = 'urn:wmo:md:subject'
        event.display_title = 'HTTP Error: 502 Bad Gateway'
        event.title = 'HTTP status 502'
        event.event_type = 'http'
        event.event_time = timezone.now()
        event.description = 'Gateway down'
        event.errors = None
        event.tests = None
        event.summary = None
        event.ingested_at = timezone.now()
        event.incident_hash = 'abc'
        return event

    def _responsible_queryset(self, responsible):
        qs = MagicMock()
        qs.exists.return_value = True
        qs.__iter__.return_value = iter([responsible])
        return qs

    @patch('telemetry.models.SubjectResponsible')
    @patch('telemetry.views.IncidentActivity.objects.create')
    @patch('telemetry.views.send_email')
    @patch('django.shortcuts.get_object_or_404')
    def test_success_sends_and_creates_event(self, mock_get, mock_send, mock_event, mock_responsible):
        mock_get.return_value = self._event()
        responsible = MagicMock()
        responsible.id = 1
        responsible.name = 'Jane Doe'
        responsible.email = 'jane@example.com'
        mock_responsible.objects.filter.return_value = self._responsible_queryset(responsible)
        from telemetry.views import email_responsible
        req = self.factory.post(
            '/event/x/email/',
            data=json.dumps({'responsible_ids': [1]}),
            content_type='application/json',
        )
        req.user = self.user
        resp = email_responsible(req, self.UUID)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(json.loads(resp.content)['success'])
        self.assertEqual(mock_send.call_args.kwargs['to_email'], 'jane@example.com')
        body = mock_send.call_args.kwargs['body']
        self.assertIn('SUBJECT: urn:wmo:md:subject', body)
        self.assertIn('TITLE: HTTP Error: 502 Bad Gateway | HTTP status 502', body)
        self.assertIn('DESCRIPTION: Gateway down', body)
        self.assertIn('EVENT_TIME:', body)
        self.assertIn('INGESTED_AT:', body)
        self.assertIn('AGENT NAME: Agent One', body)
        self.assertIn('AGENT NOTE: ', body)
        self.assertNotIn('RESPONSIBLE:', body)
        self.assertEqual(mock_event.call_args.kwargs['event_type'], 'email_sent')

    @patch('telemetry.models.SubjectResponsible')
    @patch('telemetry.views.IncidentActivity.objects.create')
    @patch('telemetry.views.send_email')
    @patch('django.shortcuts.get_object_or_404')
    def test_failure_returns_502_with_message(self, mock_get, mock_send, mock_event, mock_responsible):
        mock_get.return_value = self._event()
        responsible = MagicMock()
        responsible.id = 1
        responsible.name = 'Jane Doe'
        responsible.email = 'jane@example.com'
        mock_responsible.objects.filter.return_value = self._responsible_queryset(responsible)
        mock_send.side_effect = RuntimeError('Gmail API error 403: Daily limit exceeded')
        from telemetry.views import email_responsible
        req = self.factory.post(
            '/event/x/email/',
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
        req = self.factory.get('/event/x/email/')
        req.user = self.user
        resp = email_responsible(req, self.UUID)
        self.assertEqual(resp.status_code, 405)


class KnowledgeBaseTest(SimpleTestCase):
    def _kb(self):
        from telemetry import kb
        return kb

    def test_doc_chunks_parses_catalogue(self):
        kb = self._kb()
        chunks = kb.doc_chunks()
        self.assertGreater(len(chunks), 20)
        joined = ' '.join(c['title'] + ' ' + c['text'] for c in chunks)
        self.assertIn('Ghosting', joined)
        ids = [c['id'] for c in chunks]
        self.assertEqual(len(set(ids)), len(ids))
        for chunk in chunks:
            self.assertTrue(chunk['text'].strip())
            self.assertLessEqual(len(chunk['text']), kb.CHUNK_MAX_CHARS)
            self.assertIn(chunk['source'], ('doc', 'gdc'))

    def test_split_long_text(self):
        kb = self._kb()
        self.assertEqual(kb._split_text('short', 600), ['short'])
        parts = kb._split_text('a' * 500 + '\n\n' + 'b' * 500, 600)
        self.assertEqual(len(parts), 2)
        self.assertTrue(all(len(part) <= 600 for part in parts))
        huge = kb._split_text('x' * 1500, 1000)
        self.assertGreaterEqual(len(huge), 2)
        self.assertTrue(all(len(part) <= 1000 for part in huge))

    def test_gdc_record_chunk_maps_fields(self):
        kb = self._kb()
        feature = {
            'id': 'abc',
            'properties': {
                'identifier': 'urn:wmo:md:ca-eccc-msc:test',
                'title': 'Test Dataset',
                'description': 'A <b>dataset</b> about weather.',
                'publisher': 'ECCC-MSC',
                'contacts': [{
                    'organization': 'ECCC',
                    'role': 'pointOfContact',
                    'emails': [{'value': 'foo@bar.ca'}],
                }],
                'themes': [{'concepts': [{'id': 'temperature'}]}],
                'temporal': {'extent': {'interval': [['2024-01-01', None]]}},
            },
            'links': [{'href': 'https://example.org/file', 'rel': 'canonical', 'title': 'File'}],
        }
        chunk = kb.gdc_record_chunk(feature)
        self.assertEqual(chunk['id'], 'gdc:urn:wmo:md:ca-eccc-msc:test')
        self.assertEqual(chunk['source'], 'gdc')
        self.assertIn('Identifier: urn:wmo:md:ca-eccc-msc:test', chunk['text'])
        self.assertIn('A dataset about weather.', chunk['text'])
        self.assertIn('foo@bar.ca', chunk['text'])
        self.assertIn('temperature', chunk['text'])
        self.assertIn('2024-01-01', chunk['text'])
        self.assertIn('https://example.org/file', chunk['text'])
        self.assertIsNone(kb.gdc_record_chunk(None))

    def test_gdc_record_without_identifier_falls_back_to_slug(self):
        kb = self._kb()
        chunk = kb.gdc_record_chunk({'properties': {'title': 'Only Title!'}})
        self.assertTrue(chunk['ref_id'].startswith('gdc:'))
        self.assertEqual(chunk['id'], f"gdc:{chunk['ref_id']}")

    def test_cosine(self):
        kb = self._kb()
        self.assertAlmostEqual(kb._cosine([1, 0], [1, 0]), 1.0)
        self.assertAlmostEqual(kb._cosine([1, 0], [0, 1]), 0.0)
        self.assertAlmostEqual(kb._cosine([2, 0], [3, 0]), 1.0)
        self.assertEqual(kb._cosine([], []), 0.0)

    def test_keyword_score(self):
        kb = self._kb()
        chunk = {'title': 'Ghosting', 'text': 'data looks available but downloads fail cache'}
        self.assertGreater(kb._keyword_score('explain ghosting cache issue', chunk), 0)
        self.assertEqual(kb._keyword_score('quantum banana', chunk), 0)

    def test_index_roundtrip_and_retrieval(self):
        kb = self._kb()
        chunks = [{
            'id': 'gdc:urn:x', 'source': 'gdc', 'ref_id': 'urn:x',
            'title': 'Ghosting dataset',
            'text': 'cache ghosting downloads fail',
            'embedding': [1.0, 0.0],
        }, {
            'id': 'doc:other:1', 'source': 'doc', 'ref_id': 'other',
            'title': 'Unrelated', 'text': 'totally different words here',
            'embedding': None,
        }]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'kb.json'
            with patch.object(kb, 'index_path', return_value=target):
                path = kb.save_index(chunks, embed_model_name='test-model')
                self.assertEqual(path, target)
                index = kb.load_index(force=True)
                self.assertEqual(index['embed_model'], 'test-model')
                self.assertEqual(len(index['chunks']), 2)
                hit = kb.find_by_ref('urn:x')
                self.assertIsNotNone(hit)
                self.assertEqual(hit['ref_id'], 'urn:x')
                self.assertIsNone(kb.find_by_ref('urn:missing'))
                with patch.object(kb, 'embed_texts', side_effect=RuntimeError('down')):
                    hits = kb.retrieve('ghosting cache', k=2)
                self.assertEqual(len(hits), 1)
                self.assertEqual(hits[0]['ref_id'], 'urn:x')

    def test_retrieve_without_index_returns_empty(self):
        kb = self._kb()
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(kb, 'index_path', return_value=Path(tmp) / 'missing.json'):
                self.assertEqual(kb.retrieve('anything'), [])
                self.assertIsNone(kb.load_index())

    def test_find_metadata_id_nested(self):
        kb = self._kb()
        raw = {'specversion': '1.0', 'data': {'properties': {'metadata_id': 'urn:wmo:md:a:b'}}}
        self.assertEqual(kb.find_metadata_id(raw), 'urn:wmo:md:a:b')
        self.assertIsNone(kb.find_metadata_id({'a': [{'b': 1}]}))
        self.assertIsNone(kb.find_metadata_id(None))

    def test_format_excerpts_labels_sources(self):
        kb = self._kb()
        out = kb.format_excerpts([
            {'source': 'gdc', 'ref_id': 'urn:x', 'title': 'T', 'text': 'body'},
            {'source': 'doc', 'ref_id': 'sec', 'title': 'D', 'text': 'doc body'},
        ])
        self.assertIn('[gdc] urn:x - T\nbody', out)
        self.assertIn('[doc] sec - D\ndoc body', out)


class LlmRagConfigTest(SimpleTestCase):
    def test_system_prompt_without_knowledge_matches_legacy_shape(self):
        from telemetry.llm import build_system_prompt
        prompt = build_system_prompt('FACTS')
        self.assertTrue(prompt.startswith('You are the built-in assistant'))
        self.assertIn('ABOUT THE SYSTEM', prompt)
        self.assertIn('=== EVENT CONTEXT ===\nFACTS', prompt)
        self.assertNotIn('KNOWLEDGE BASE EXCERPTS', prompt)

    def test_system_prompt_with_knowledge_section_ordering(self):
        from telemetry.llm import build_system_prompt
        prompt = build_system_prompt('FACTS', knowledge='KB CHUNK')
        self.assertIn('KB CHUNK', prompt)
        self.assertLess(prompt.index('ABOUT THE SYSTEM'), prompt.index('KNOWLEDGE BASE EXCERPTS'))
        self.assertLess(prompt.index('KNOWLEDGE BASE EXCERPTS'), prompt.index('EVENT CONTEXT'))

    def test_raw_json_excerpt_unlimited_by_default(self):
        from telemetry.llm import raw_json_excerpt
        event = MagicMock()
        event.raw_json = {'k': 'v' * 3000}
        text = raw_json_excerpt(event)
        self.assertNotIn('[truncated]', text)
        self.assertGreater(len(text), 3000)
        self.assertIn('[truncated]', raw_json_excerpt(event, limit=100))
        self.assertEqual(raw_json_excerpt(MagicMock(raw_json=None)), '')

    def test_num_ctx_default_and_override(self):
        from telemetry import llm
        saved = os.environ.pop('OLLAMA_NUM_CTX', None)
        try:
            self.assertEqual(llm._num_ctx(), llm.DEFAULT_NUM_CTX)
        finally:
            if saved is not None:
                os.environ['OLLAMA_NUM_CTX'] = saved
        with patch.dict(os.environ, {'OLLAMA_NUM_CTX': '4096'}):
            self.assertEqual(llm._num_ctx(), 4096)
        with patch.dict(os.environ, {'OLLAMA_NUM_CTX': 'bogus'}):
            self.assertEqual(llm._num_ctx(), llm.DEFAULT_NUM_CTX)


class BuildLlmKbCommandTest(SimpleTestCase):
    def _run(self, *args):
        out = io.StringIO()
        call_command('build_llm_kb', *args, stdout=out)
        return out.getvalue()

    @patch('telemetry.kb.harvest_gdc')
    @patch('telemetry.kb.embed_texts', return_value=[[0.1, 0.2]])
    @patch('telemetry.kb.doc_chunks')
    def test_docs_only_build(self, mock_docs, mock_embed, mock_harvest):
        mock_docs.return_value = [{
            'id': 'doc:a:1', 'source': 'doc', 'ref_id': 'a',
            'title': 'A', 'text': 'alpha',
        }]
        with patch('telemetry.kb.save_index', return_value=Path('x')) as mock_save:
            output = self._run('--skip-gdc')
        mock_harvest.assert_not_called()
        saved_chunks = mock_save.call_args.args[0]
        self.assertEqual(saved_chunks[0]['embedding'], [0.1, 0.2])
        self.assertEqual(mock_save.call_args.kwargs['embed_model_name'], 'nomic-embed-text')
        self.assertIn('Documentation: extracted 1 chunk(s)', output)
        self.assertIn('Done: wrote 1 chunk(s)', output)

    @patch('telemetry.kb.embed_texts', side_effect=RuntimeError('ollama down'))
    @patch('telemetry.kb.doc_chunks')
    def test_embed_failure_writes_keyword_only_index(self, mock_docs, mock_embed):
        mock_docs.return_value = [{
            'id': 'doc:a:1', 'source': 'doc', 'ref_id': 'a',
            'title': 'A', 'text': 'alpha',
        }]
        with patch('telemetry.kb.save_index', return_value=Path('x')) as mock_save:
            output = self._run('--skip-gdc')
        saved_chunks = mock_save.call_args.args[0]
        self.assertNotIn('embedding', saved_chunks[0])
        self.assertIsNone(mock_save.call_args.kwargs['embed_model_name'])
        self.assertIn('Embedding failed', output)
        self.assertIn('keyword retrieval only', output)

    @patch('telemetry.kb.embed_texts', return_value=[[0.0], [0.0]])
    @patch('telemetry.kb.doc_chunks')
    def test_gdc_records_merged_and_deduped(self, mock_docs, mock_embed):
        mock_docs.return_value = [{
            'id': 'doc:a:1', 'source': 'doc', 'ref_id': 'a',
            'title': 'A', 'text': 'alpha',
        }]
        gdc_chunk = {
            'id': 'gdc:urn:u', 'source': 'gdc', 'ref_id': 'urn:u',
            'title': 'U', 'text': 'uu',
        }
        with patch('telemetry.kb.harvest_gdc', return_value=[dict(gdc_chunk), dict(gdc_chunk)]), \
                patch('telemetry.kb.save_index', return_value=Path('x')) as mock_save:
            output = self._run()
        saved_chunks = mock_save.call_args.args[0]
        self.assertEqual(len(saved_chunks), 2)
        self.assertIn('harvested 2 WCMP2 record(s)', output)
        self.assertIn('Deduplicated 1 duplicate chunk(s)', output)
