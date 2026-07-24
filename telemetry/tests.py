import hashlib
import json
import uuid
from unittest.mock import patch, MagicMock

from django.test import SimpleTestCase, RequestFactory

from telemetry.views import _is_admin, get_alerts_for_user
from wis2_ingestion import _compute_display_title, parse_wmem_record


class IsAdminCachingTest(SimpleTestCase):
    def _make_user(self, is_admin=False):
        user = MagicMock(spec=['id', 'groups'])
        user.id = 1
        user.groups.filter.return_value.exists.return_value = is_admin
        return user

    def test_admin_returns_true(self):
        user = self._make_user(is_admin=True)
        self.assertTrue(_is_admin(user))

    def test_regular_returns_false(self):
        user = self._make_user(is_admin=False)
        self.assertFalse(_is_admin(user))

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

    @patch('telemetry.views.exclude_muted')
    @patch('telemetry.views.get_alerts_for_user')
    def test_api_alerts_bad_offset(self, mock_get_alerts, mock_exclude):
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs
        mock_qs.only.return_value = mock_qs
        mock_qs.order_by.return_value = mock_qs
        mock_exclude.return_value = mock_qs
        mock_get_alerts.return_value = mock_qs
        from telemetry.views import api_alerts
        request = self.factory.get('/api/alerts/', {'offset': 'abc', 'limit': 'xyz'})
        request.user = self.user
        response = api_alerts(request)
        self.assertEqual(response.status_code, 200)

    @patch('telemetry.views.exclude_muted')
    @patch('telemetry.views.get_alerts_for_user')
    def test_api_alerts_bad_limit(self, mock_get_alerts, mock_exclude):
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs
        mock_qs.only.return_value = mock_qs
        mock_qs.order_by.return_value = mock_qs
        mock_exclude.return_value = mock_qs
        mock_get_alerts.return_value = mock_qs
        from telemetry.views import api_alerts
        request = self.factory.get('/api/alerts/', {'limit': 'notanumber'})
        request.user = self.user
        response = api_alerts(request)
        self.assertEqual(response.status_code, 200)

    @patch('telemetry.views.exclude_muted')
    @patch('telemetry.views.get_alerts_for_user')
    def test_api_alerts_per_day_bad_days(self, mock_get_alerts, mock_exclude):
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs
        mock_qs.annotate.return_value = mock_qs
        mock_qs.values.return_value = mock_qs
        mock_qs.__iter__ = lambda s: iter([])
        mock_exclude.return_value = mock_qs
        mock_get_alerts.return_value = mock_qs
        from telemetry.views import api_alerts_per_day
        request = self.factory.get('/api/alerts/per-day/', {'days': 'abc'})
        request.user = self.user
        response = api_alerts_per_day(request)
        self.assertEqual(response.status_code, 200)

    @patch('telemetry.views.exclude_muted')
    @patch('telemetry.views.get_alerts_for_user')
    def test_api_alerts_valid_params(self, mock_get_alerts, mock_exclude):
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs
        mock_qs.only.return_value = mock_qs
        mock_qs.order_by.return_value = mock_qs
        mock_exclude.return_value = mock_qs
        mock_get_alerts.return_value = mock_qs
        from telemetry.views import api_alerts
        request = self.factory.get('/api/alerts/', {'offset': '0', 'limit': '10'})
        request.user = self.user
        response = api_alerts(request)
        self.assertEqual(response.status_code, 200)

    @patch('telemetry.views.exclude_muted')
    @patch('telemetry.views.get_alerts_for_user')
    def test_api_alerts_per_day_valid_params(self, mock_get_alerts, mock_exclude):
        mock_qs = MagicMock()
        mock_qs.filter.return_value = mock_qs
        mock_qs.annotate.return_value = mock_qs
        mock_qs.values.return_value = mock_qs
        mock_qs.__iter__ = lambda s: iter([])
        mock_exclude.return_value = mock_qs
        mock_get_alerts.return_value = mock_qs
        from telemetry.views import api_alerts_per_day
        request = self.factory.get('/api/alerts/per-day/', {'days': '7'})
        request.user = self.user
        response = api_alerts_per_day(request)
        self.assertEqual(response.status_code, 200)


class AlertExistsTest(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = MagicMock()
        self.user.is_authenticated = True
        self.user.id = 1

    def test_requires_login(self):
        from telemetry.views import alert_exists
        request = self.factory.get(f'/api/alert-exists/{uuid.uuid4()}/')
        request.user = MagicMock(is_authenticated=False)
        response = alert_exists(request, alert_id=uuid.uuid4())
        self.assertEqual(response.status_code, 302)

    @patch('telemetry.views.get_alerts_for_user')
    def test_nonexistent_alert_returns_false(self, mock_get_alerts):
        mock_qs = MagicMock()
        mock_qs.filter.return_value.exists.return_value = False
        mock_get_alerts.return_value = mock_qs
        from telemetry.views import alert_exists
        request = self.factory.get(f'/api/alert-exists/{uuid.uuid4()}/')
        request.user = self.user
        response = alert_exists(request, alert_id=uuid.uuid4())
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertFalse(data['exists'])


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
