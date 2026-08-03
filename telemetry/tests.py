import hashlib
import io
import json
import uuid
from unittest.mock import patch, MagicMock

from django.test import SimpleTestCase, RequestFactory
from django.core.management import call_command

from telemetry.views import _is_admin, get_alerts_for_user, apply_window, get_type_choices, expand_type_labels, apply_filters, apply_keyword_filter
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
