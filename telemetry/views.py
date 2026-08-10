import uuid
from django.shortcuts import render
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from django.db.models import Q, Max, Count, TextField
from django.db.models.functions import TruncDay, TruncHour, Cast

from django.core.paginator import Paginator
from django.core.cache import cache
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from datetime import timedelta
from .models import Alert, Node, IncidentEvent, IncidentMute
from .templatetags.monitor_extras import event_type_label

from .jira import (
    build_summary as jira_build_summary,
    is_configured as jira_is_configured,
    create_jira_ticket as jira_create_ticket,
)

def _is_admin(user):
    if not hasattr(user, '_is_admin_cached'):
        user._is_admin_cached = user.is_superuser or user.groups.filter(name='Admin').exists()
    return user._is_admin_cached


def get_alerts_for_user(user):
    if _is_admin(user):
        return Alert.objects.all()
    profile = getattr(user, 'profile', None)
    if profile is not None:
        allowed_nodes = profile.allowed_nodes.all()
        if allowed_nodes:
            return Alert.objects.filter(node__in=allowed_nodes)
    return Alert.objects.none()


def exclude_muted(qs, user):
    muted_hashes = list(IncidentMute.objects.filter(
        user=user, muted_until__gt=timezone.now()
    ).values_list('incident_hash', flat=True))
    if muted_hashes:
        qs = qs.exclude(incident_hash__in=muted_hashes, incident_hash__isnull=False)
    return qs


CACHE_TTL = 60


def parse_filter_params(request):
    params = {}
    params['severity'] = [s for s in request.GET.getlist('severity') if s]
    params['node'] = [s for s in request.GET.getlist('node') if s]
    params['type'] = [s for s in request.GET.getlist('type') if s]
    params['alert'] = [s for s in request.GET.getlist('alert') if s]
    params['source'] = [s for s in request.GET.getlist('source') if s]
    params['time_from'] = request.GET.get('time_from', '').strip()
    params['time_to'] = request.GET.get('time_to', '').strip()
    params['q'] = request.GET.get('q', '').strip()
    params['sort'] = request.GET.get('sort', 'ingested_at')
    if params['sort'] not in ('event_time', 'ingested_at'):
        params['sort'] = 'ingested_at'
    return params


def apply_filters(qs, params):
    if params.get('severity'):
        qs = qs.filter(severity__in=params['severity'])
    if params.get('node'):
        qs = qs.filter(node__in=params['node'])
    if params.get('type'):
        qs = qs.filter(event_type__in=params['type'])
    if params.get('alert'):
        qs = qs.filter(display_title__in=params['alert'])
    if params.get('source'):
        qs = qs.filter(source__in=params['source'])
    if params.get('time_from'):
        qs = qs.filter(event_time__gte=params['time_from'])
    if params.get('time_to'):
        qs = qs.filter(event_time__lte=params['time_to'])
    if params.get('q'):
        qs = apply_keyword_filter(qs, params['q'])
    return qs


def apply_keyword_filter(qs, q):
    """Filter alerts whose text fields or full JSON blob contain the keyword."""
    q = (q or '').strip()
    if not q:
        return qs
    text_q = (
        Q(event_type__icontains=q)
        | Q(source__icontains=q)
        | Q(node__name__icontains=q)
        | Q(title__icontains=q)
        | Q(display_title__icontains=q)
        | Q(description__icontains=q)
        | Q(subtype__icontains=q)
        | Q(channel__icontains=q)
        | Q(dataschema__icontains=q)
        | Q(incident_hash__icontains=q)
    )
    try:
        uuid.UUID(q)
    except (ValueError, AttributeError, TypeError):
        pass
    else:
        text_q |= Q(id=q)
    qs = qs.filter(text_q)
    return qs.annotate(
        _searchable_blob=Cast('raw_json', output_field=TextField())
    ).filter(_searchable_blob__icontains=q)


def apply_window(qs, time_window, from_str='', to_str=''):
    """Apply the dashboard time window; returns (filtered_qs, time_window)."""
    if time_window == '12h':
        return qs.filter(event_time__gte=timezone.now() - timedelta(hours=12)), '12h'
    if time_window == '24h':
        return qs.filter(event_time__gte=timezone.now() - timedelta(hours=24)), '24h'
    if time_window == 'custom':
        from_dt = parse_datetime(from_str) if from_str else None
        to_dt = parse_datetime(to_str) if to_str else None
        if from_dt is None or to_dt is None or from_dt > to_dt:
            return qs, 'all'
        return qs.filter(event_time__gte=from_str, event_time__lte=to_str), 'custom'
    return qs, 'all'


def get_cached_choices(cache_key, queryset, ttl=None):
    if ttl is None:
        ttl = CACHE_TTL
    val = cache.get(cache_key)
    if val is None:
        val = list(queryset)
        cache.set(cache_key, val, ttl)
    return val


def get_node_choices(user):
    if _is_admin(user):
        return get_cached_choices(
            'dashboard_node_choices',
            Node.objects.values_list('name', flat=True).order_by('name'),
        )
    profile = getattr(user, 'profile', None)
    if profile is None:
        return []
    return list(
        profile.allowed_nodes.values_list('name', flat=True).order_by('name')
    )


def _raw_type_choices(user):
    suffix = '' if _is_admin(user) else f'_{user.id}'
    return get_cached_choices(
        f'dashboard_type_choices{suffix}',
        get_alerts_for_user(user).filter(event_type__isnull=False).exclude(event_type='')
        .values_list('event_type', flat=True).distinct().order_by('event_type')[:500],
    )


def get_type_choices(user):
    return sorted({event_type_label(t) for t in _raw_type_choices(user)})


def expand_type_labels(user, labels):
    if not labels:
        return []
    label_set = set(labels)
    return [t for t in _raw_type_choices(user) if event_type_label(t) in label_set]


def get_source_choices(user):
    suffix = '' if _is_admin(user) else f'_{user.id}'
    return get_cached_choices(
        f'dashboard_source_choices{suffix}',
        get_alerts_for_user(user).filter(source__isnull=False).exclude(source='')
        .values_list('source', flat=True).distinct().order_by('source'),
    )


def get_alert_choices(user):
    suffix = '' if _is_admin(user) else f'_{user.id}'
    return get_cached_choices(
        f'dashboard_alert_choices{suffix}',
        get_alerts_for_user(user).filter(display_title__isnull=False).exclude(display_title='')
        .order_by('display_title').values_list('display_title', flat=True).distinct('display_title')[:500],
    )


@login_required
def dashboard(request):
    qs = get_alerts_for_user(request.user)

    time_window = request.GET.get('window', 'all')
    window_from = request.GET.get('from', '').strip()
    window_to = request.GET.get('to', '').strip()

    qs_cards, time_window = apply_window(qs, time_window, window_from, window_to)
    if time_window != 'custom':
        window_from = window_to = ''

    sev_counts_qs = qs_cards.values('severity').annotate(cnt=Count('id'))
    severity_counts = {s: 0 for s in ['CRITICAL', 'ERROR', 'WARNING', 'INFO']}
    for row in sev_counts_qs:
        severity_counts[row['severity']] = row['cnt']

    mini_alerts = qs.only('id', 'ingested_at', 'severity', 'node_id', 'title', 'display_title', 'event_type', 'event_time').order_by('-ingested_at')[:20]

    critical_alerts = qs.filter(severity='CRITICAL').only('id', 'ingested_at', 'severity', 'node_id', 'title', 'display_title', 'event_type', 'event_time').order_by('-ingested_at')[:20]

    # ETS Reports – latest per node ever that had an issue
    ets_rows = (
        qs_cards.filter(Q(summary__isnull=False) | Q(tests__isnull=False))
        .order_by('node_id', '-event_time')
        .distinct('node_id')
        .values('node_id', 'severity', 'title', 'id', 'event_time', 'summary', 'tests')
    )

    def _is_kpi_summary(s):
        return isinstance(s, dict) and any(k in s for k in ('grade', 'score', 'percentage'))

    def _is_ets_summary(s):
        return isinstance(s, dict) and any(k in s for k in ('PASSED', 'FAILED', 'SKIPPED', 'WARNING'))

    ets_data = []
    kpi_data = []
    for r in ets_rows:
        s = r['summary'] if isinstance(r['summary'], dict) else {}
        if _is_kpi_summary(s):
            score = s.get('score')
            total = s.get('total')
            kpi_data.append({
                'node_id': r['node_id'],
                'severity': r['severity'],
                'title': r['title'],
                'id': str(r['id']),
                'event_time': r['event_time'],
                'grade': s.get('grade'),
                'score': score,
                'total': total,
                'percentage': s.get('percentage'),
                'has_issue': isinstance(score, (int, float)) and isinstance(total, (int, float)) and score < total,
            })
        elif _is_ets_summary(s):
            passed = s.get('PASSED', 0)
            total = sum(v for v in s.values() if isinstance(v, (int, float)))
            if isinstance(total, (int, float)) and isinstance(passed, (int, float)):
                e = {
                    'node_id': r['node_id'],
                    'severity': r['severity'],
                    'title': r['title'],
                    'id': str(r['id']),
                    'event_time': r['event_time'],
                    'summary': s,
                    'total': total,
                    'passed': passed,
                    'has_issue': total != passed,
                }
            else:
                e = {
                    'node_id': r['node_id'],
                    'severity': r['severity'],
                    'title': r['title'],
                    'id': str(r['id']),
                    'event_time': r['event_time'],
                    'summary': s,
                    'total': 0,
                    'passed': 0,
                    'has_issue': True,
                }
            ets_data.append(e)

    type_choices = get_type_choices(request.user)
    node_choices = get_node_choices(request.user)
    source_choices = get_source_choices(request.user)

    return render(request, 'telemetry/dashboard.html', {
        'severity_counts': severity_counts,
        'mini_alerts': mini_alerts,
        'critical_alerts': critical_alerts,
        'type_choices': type_choices,
        'node_choices': node_choices,
        'source_choices': source_choices,
        'ets_data': ets_data,
        'ets_issue_count': sum(1 for e in ets_data if e['has_issue']),
        'ets_has_issues': any(e['has_issue'] for e in ets_data),
        'kpi_data': kpi_data,
        'kpi_issue_count': sum(1 for e in kpi_data if e['has_issue']),
        'kpi_has_issues': any(e['has_issue'] for e in kpi_data),
        'time_window': time_window,
        'window_from': window_from,
        'window_to': window_to,
    })


@login_required
def monitor_alerts(request):
    params = parse_filter_params(request)
    sort = params['sort']

    type_labels = params['type']
    params['type'] = expand_type_labels(request.user, type_labels)

    qs = get_alerts_for_user(request.user)
    qs = apply_filters(qs, params)
    qs = exclude_muted(qs, request.user)
    qs = qs.order_by(f'-{sort}')
    latest_ingested = qs.aggregate(Max('ingested_at'))['ingested_at__max']
    latest_ingested_iso = latest_ingested.isoformat() if latest_ingested else ''

    paginator = Paginator(qs, 10)
    page_number = request.GET.get('page', 1)
    try:
        page_obj = paginator.get_page(page_number)
    except Exception:
        page_obj = paginator.get_page(1)

    base_params = request.GET.copy()
    base_params.pop('page', None)
    base_query = base_params.urlencode()

    type_choices = get_type_choices(request.user)
    node_choices = get_node_choices(request.user)
    source_choices = get_source_choices(request.user)
    alert_choices = get_alert_choices(request.user)

    return render(request, 'telemetry/alert_list.html', {
        'alerts': page_obj.object_list,
        'page_obj': page_obj,
        'latest_ingested_iso': latest_ingested_iso,
        'base_query': base_query,
        'type_choices': type_choices,
        'node_choices': node_choices,
        'source_choices': source_choices,
        'alert_choices': alert_choices,
        'current_severities': params['severity'],
        'current_nodes': params['node'],
        'current_types': type_labels,
        'current_alerts': params['alert'],
        'current_sources': params['source'],
        'current_time_from': params['time_from'],
        'current_time_to': params['time_to'],
        'current_q': params['q'],
        'current_sort': sort,
    })


@login_required
def alert_search(request):
    q = request.GET.get('q', '').strip()
    if not q:
        return JsonResponse({'found': False, 'count': 0})
    count = apply_keyword_filter(get_alerts_for_user(request.user), q).count()
    return JsonResponse({'found': count > 0, 'count': count})


@login_required
def incident_comment(request, alert_id):
    from django.shortcuts import get_object_or_404
    import json
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    alert = get_object_or_404(get_alerts_for_user(request.user), id=alert_id)
    if not alert.incident_hash:
        return JsonResponse({'error': 'Alert has no incident hash'}, status=400)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        data = request.POST
    text = (data.get('text') or '').strip()
    if not text:
        return JsonResponse({'error': 'text is required'}, status=400)
    event = IncidentEvent.objects.create(
        incident_hash=alert.incident_hash,
        alert=alert,
        user=request.user,
        event_type='comment',
        text=text,
    )
    return JsonResponse({'id': event.id, 'created_at': event.created_at.isoformat()})


@login_required
def incident_note(request, alert_id):
    from django.shortcuts import get_object_or_404
    import json
    from django.utils.dateparse import parse_datetime
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    alert = get_object_or_404(get_alerts_for_user(request.user), id=alert_id)
    if not alert.incident_hash:
        return JsonResponse({'error': 'Alert has no incident hash'}, status=400)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        data = request.POST
    text = (data.get('text') or '').strip()
    if not text:
        return JsonResponse({'error': 'text is required'}, status=400)
    duration = data.get('duration')
    if not duration:
        return JsonResponse({'error': 'duration is required (e.g. 3600 for 1 hour)'}, status=400)
    try:
        expires_at = timezone.now() + timedelta(seconds=int(duration))
    except (TypeError, ValueError):
        return JsonResponse({'error': 'invalid duration'}, status=400)
    event = IncidentEvent.objects.create(
        incident_hash=alert.incident_hash,
        alert=alert,
        user=request.user,
        event_type='note_added',
        text=text,
        expires_at=expires_at,
    )
    return JsonResponse({'id': event.id, 'expires_at': expires_at.isoformat()})


@login_required
@require_POST
def incident_note_remove(request, alert_id, event_id):
    from django.shortcuts import get_object_or_404
    alert = get_object_or_404(get_alerts_for_user(request.user), id=alert_id)
    if not alert.incident_hash:
        return JsonResponse({'error': 'Alert has no incident hash'}, status=400)
    event = get_object_or_404(IncidentEvent, id=event_id, incident_hash=alert.incident_hash, event_type='note_added', active=True)
    event.active = False
    event.save(update_fields=['active'])
    IncidentEvent.objects.create(
        incident_hash=alert.incident_hash,
        alert=alert,
        user=request.user,
        event_type='note_removed',
        text=f"Note removed: {event.text}",
    )
    return JsonResponse({'success': True})


@login_required
def incident_mute(request, alert_id):
    from django.shortcuts import get_object_or_404
    import json
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    alert = get_object_or_404(get_alerts_for_user(request.user), id=alert_id)
    if not alert.incident_hash:
        return JsonResponse({'error': 'Alert has no incident hash'}, status=400)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        data = request.POST
    duration = data.get('duration')
    if not duration:
        return JsonResponse({'error': 'duration is required'}, status=400)
    try:
        muted_until = timezone.now() + timedelta(seconds=int(duration))
    except (TypeError, ValueError):
        return JsonResponse({'error': 'invalid duration'}, status=400)
    mute, created = IncidentMute.objects.update_or_create(
        incident_hash=alert.incident_hash,
        user=request.user,
        defaults={'muted_until': muted_until},
    )
    if created:
        IncidentEvent.objects.create(
            incident_hash=alert.incident_hash,
            alert=alert,
            user=request.user,
            event_type='muted',
            text=f"Muted until {muted_until.strftime('%Y-%m-%d %H:%M UTC')}",
        )
    return JsonResponse({'muted_until': muted_until.isoformat()})


@login_required
def incident_unmute(request, alert_id):
    from django.shortcuts import get_object_or_404
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    alert = get_object_or_404(get_alerts_for_user(request.user), id=alert_id)
    if not alert.incident_hash:
        return JsonResponse({'error': 'Alert has no incident hash'}, status=400)
    deleted, _ = IncidentMute.objects.filter(
        incident_hash=alert.incident_hash, user=request.user
    ).delete()
    if deleted:
        IncidentEvent.objects.create(
            incident_hash=alert.incident_hash,
            alert=alert,
            user=request.user,
            event_type='unmuted',
            text="Alert unmuted",
        )
        IncidentEvent.objects.create(
            incident_hash=alert.incident_hash,
            alert=alert,
            user=request.user,
            event_type='comment',
            text="Alert unmuted",
        )
    return JsonResponse({'success': True})


@login_required
def incident_activity(request, alert_id):
    from django.shortcuts import get_object_or_404
    alert = get_object_or_404(get_alerts_for_user(request.user), id=alert_id)
    if not alert.incident_hash:
        return JsonResponse({'events': [], 'notes': [], 'muted_until': None, 'total': 0})
    events_qs = IncidentEvent.objects.filter(
        incident_hash=alert.incident_hash
    ).select_related('user').order_by('-created_at')
    total = events_qs.count()
    try:
        offset = int(request.GET.get('offset', 0))
        limit = int(request.GET.get('limit', 10))
    except (TypeError, ValueError):
        offset, limit = 0, 10
    events = events_qs[offset:offset+limit]
    muted_until = None
    try:
        mute = IncidentMute.objects.get(incident_hash=alert.incident_hash, user=request.user)
        if mute.muted_until > timezone.now():
            muted_until = mute.muted_until.isoformat()
    except IncidentMute.DoesNotExist:
        pass
    notes = IncidentEvent.objects.filter(
        incident_hash=alert.incident_hash,
        event_type='note_added',
        active=True,
        expires_at__gt=timezone.now(),
    ).select_related('user').order_by('-created_at')
    return JsonResponse({
        'events': [
            {
                'id': e.id,
                'event_type': e.event_type,
                'text': e.text,
                'user': e.user.username,
                'user_full': e.user.get_full_name() or e.user.username,
                'created_at': e.created_at.isoformat(),
                'expires_at': e.expires_at.isoformat() if e.expires_at else None,
                'active': e.active,
            }
            for e in events
        ],
        'notes': [
            {
                'id': n.id,
                'text': n.text,
                'user': n.user.username,
                'user_full': n.user.get_full_name() or n.user.username,
                'created_at': n.created_at.isoformat(),
                'expires_at': n.expires_at.isoformat(),
            }
            for n in notes
        ],
        'muted_until': muted_until,
        'total': total,
        'offset': offset,
        'limit': limit,
    })


@login_required
def email_responsible(request, alert_id):
    from django.shortcuts import get_object_or_404
    from django.core.mail import send_mail
    from django.http import JsonResponse
    from .models import NodeResponsible
    import json
    alert = get_object_or_404(get_alerts_for_user(request.user), id=alert_id)
    if request.method != 'POST':
        return JsonResponse({'error': 'POST required'}, status=405)
    rate_key = f'email_responsible:{request.user.id}'
    rate_count = cache.get(rate_key, 0)
    if rate_count >= 10:
        return JsonResponse({'error': 'Rate limit: max 10 emails per minute'}, status=429)
    cache.set(rate_key, rate_count + 1, timeout=60)
    try:
        data = json.loads(request.body)
    except json.JSONDecodeError:
        data = request.POST
    responsible_ids = data.get('responsible_ids') or ([data['responsible_id']] if data.get('responsible_id') else [])
    if not responsible_ids:
        return JsonResponse({'error': 'responsible_ids is required'}, status=400)
    responsible_ids = responsible_ids[:10]
    responsibles = NodeResponsible.objects.filter(id__in=responsible_ids)
    if not responsibles.exists():
        return JsonResponse({'error': 'Responsible person(s) not found'}, status=404)
    note = (data.get('note') or '').strip()
    sent_names = []
    for responsible in responsibles:
        lines = []
        lines.append(f"NODE: {alert.node_id}")
        lines.append(f"RESPONSIBLE: {responsible.name} <{responsible.email}>")
        lines.append(f"TITLE: {alert.title or ''}")
        lines.append(f"TIME: {alert.event_time.strftime('%Y-%m-%d %H:%M:%S UTC') if alert.event_time else ''}")
        if alert.description:
            lines.append(f"DESCRIPTION: {alert.description}")
        if alert.errors:
            lines.append(f"ERRORS: {json.dumps(alert.errors, indent=2)}")
        if alert.tests:
            lines.append(f"TESTS: {json.dumps(alert.tests, indent=2)}")
        if alert.summary:
            lines.append(f"SUMMARY: {json.dumps(alert.summary, indent=2)}")
        lines.append("")
        lines.append(f"AGENT NAME: {request.user.get_full_name() or request.user.username}")
        lines.append(f"INGESTION TIME: {alert.ingested_at.strftime('%Y-%m-%d %H:%M:%S UTC') if alert.ingested_at else ''}")
        lines.append(f"AGENT NOTE: {note}")
        send_mail(
            subject=f"[WIS2 Alert] {alert.node_id} - {alert.title or alert.event_type}",
            message="\n".join(lines),
            from_email=None,
            recipient_list=[responsible.email],
            fail_silently=False,
        )
        sent_names.append(f"{responsible.name} <{responsible.email}>")
    if alert.incident_hash:
        email_text = f"Email sent to {', '.join(sent_names)}"
        if note:
            email_text += f" — {note}"
        IncidentEvent.objects.create(
            incident_hash=alert.incident_hash,
            alert=alert,
            user=request.user,
            event_type='email_sent',
            text=email_text,
        )
    return JsonResponse({'success': True, 'sent': len(sent_names)})


@login_required
def alert_detail(request, alert_id):
    from django.shortcuts import get_object_or_404
    user_qs = get_alerts_for_user(request.user).select_related('node').prefetch_related('node__responsibles')
    alert = get_object_or_404(user_qs, id=alert_id)

    tests_list = []
    if alert.tests:
        if isinstance(alert.tests, list):
            tests_list = alert.tests
        elif isinstance(alert.tests, dict):
            tests_list = [alert.tests]

    ets_tests = []
    kpi_tests = []
    for t in tests_list:
        if isinstance(t, dict) and (
            'score' in t or 'percentage' in t or '/kpi/' in str(t.get('id', ''))
        ):
            kpi_tests.append(t)
        else:
            ets_tests.append(t)

    is_kpi_report = bool(kpi_tests)

    kpi_overall = {}
    if is_kpi_report and isinstance(alert.summary, dict):
        for key in ('grade', 'score', 'total', 'percentage'):
            if key in alert.summary:
                kpi_overall[key] = alert.summary[key]

    summary_dict = alert.summary if isinstance(alert.summary, dict) else {}

    history_page_obj = None
    history_count = 0
    if alert.node_id:
        history_qs = user_qs.filter(
            node=alert.node_id,
        ).exclude(id=alert.id).order_by('-event_time')
        history_paginator = Paginator(history_qs, 6)
        history_count = history_paginator.count
        try:
            history_page = int(request.GET.get('history_page', 1))
        except (TypeError, ValueError):
            history_page = 1
        try:
            history_page_obj = history_paginator.get_page(history_page)
        except Exception:
            history_page_obj = history_paginator.get_page(1)

    jira_summary = jira_build_summary(alert)
    jira_description = alert.description or jira_summary
    jira_configured = jira_is_configured()

    node_responsibles = list(alert.node.responsibles.all().order_by('name')) if alert.node_id else []

    if alert.incident_hash:
        cutoff = timezone.now() - timedelta(minutes=5)
        has_recent_view = IncidentEvent.objects.filter(
            incident_hash=alert.incident_hash,
            user=request.user,
            event_type='viewed',
            created_at__gt=cutoff,
        ).exists()
        if not has_recent_view:
            IncidentEvent.objects.create(
                incident_hash=alert.incident_hash,
                alert=alert,
                user=request.user,
                event_type='viewed',
                text=f"{request.user.get_full_name() or request.user.username} viewed alert {alert.id}",
            )

    return render(request, 'telemetry/detail.html', {
        'alert': alert,
        'ets_tests': ets_tests,
        'kpi_tests': kpi_tests,
        'kpi_overall': kpi_overall,
        'is_kpi_report': is_kpi_report,
        'summary_dict': summary_dict,
        'history_page_obj': history_page_obj,
        'history_count': history_count,
        'jira_summary': jira_summary,
        'jira_description': jira_description,
        'jira_configured': jira_configured,
        'node_responsibles': node_responsibles,
        'raw_json': alert.raw_json if isinstance(alert.raw_json, (dict, list)) else {},
    })


SEV_COLORS = {'CRITICAL': '#FF4444', 'ERROR': '#FF8800', 'WARNING': '#FFD600', 'INFO': '#448AFF'}
PALETTE = ['#4DD0C4', '#AB47BC', '#78909C', '#26A69A', '#EF5350', '#42A5F5', '#FFA726', '#66BB6A', '#EC407A', '#8D6E63']


@login_required
def api_alerts_per_day(request):
    try:
        days = int(request.GET.get('days', 14))
    except (TypeError, ValueError):
        days = 14
    if days not in (7, 14, 30):
        days = 14
    nodes = request.GET.getlist('node')
    group_by = request.GET.get('group_by', 'severity')
    if group_by not in ('severity', 'event_type'):
        group_by = 'severity'

    window = request.GET.get('window', 'all')
    from_str = request.GET.get('from', '').strip()
    to_str = request.GET.get('to', '').strip()

    qs = get_alerts_for_user(request.user)
    if nodes:
        qs = qs.filter(node_id__in=nodes)

    now = timezone.now()

    if window == '12h':
        from_dt = now - timedelta(hours=12)
        to_dt = now
    elif window == '24h':
        from_dt = now - timedelta(hours=24)
        to_dt = now
    elif window == 'custom':
        from_dt = parse_datetime(from_str) if from_str else None
        to_dt = parse_datetime(to_str) if to_str else None
        if from_dt is None or to_dt is None or from_dt > to_dt:
            window = 'all'
        else:
            from_dt = timezone.make_aware(from_dt)
            to_dt = timezone.make_aware(to_dt)
    else:
        window = 'all'

    if window == 'all':
        qs = qs.filter(event_time__gte=now - timedelta(days=days))
        granularity = 'day'
        labels = [(now.date() - timedelta(days=days - 1 - i)).strftime('%Y-%m-%d') for i in range(days)]
        trunc = TruncDay('event_time')
    else:
        qs = qs.filter(event_time__gte=from_dt)
        qs = qs.filter(event_time__lte=to_dt)
        if (to_dt - from_dt) <= timedelta(hours=24):
            granularity = 'hour'
            trunc = TruncHour('event_time')
            start = from_dt.replace(minute=0, second=0, microsecond=0)
            end = to_dt.replace(minute=0, second=0, microsecond=0)
            labels = []
            k = start
            while k <= end:
                labels.append(k.strftime('%Y-%m-%d %H:%M'))
                k += timedelta(hours=1)
        else:
            granularity = 'day'
            trunc = TruncDay('event_time')
            labels = []
            d = from_dt.date()
            while d <= to_dt.date():
                labels.append(d.strftime('%Y-%m-%d'))
                d += timedelta(days=1)

    group_field = 'severity' if group_by == 'severity' else 'event_type'
    data = (
        qs.annotate(day=trunc)
        .values('day', group_field)
        .annotate(count=Count('id'))
    )

    day_map = {}
    group_set = set()
    key_fmt = '%Y-%m-%d %H:%M' if granularity == 'hour' else '%Y-%m-%d'
    for d in data:
        g = d[group_field] or 'Unknown'
        day_key = d['day'].strftime(key_fmt) if d['day'] else ''
        group_set.add(g)
        if day_key not in day_map:
            day_map[day_key] = {}
        day_map[day_key][g] = d['count']

    all_groups = sorted(group_set)

    datasets = []
    for g in all_groups:
        color = SEV_COLORS.get(g)
        if not color:
            color = PALETTE[len(datasets) % len(PALETTE)]
        datasets.append({
            'label': event_type_label(g) if group_by == 'event_type' else g,
            'data': [day_map.get(d, {}).get(g, 0) for d in labels],
            'borderColor': color,
            'backgroundColor': color + '20',
            'fill': False,
            'tension': 0.1,
        })

    return JsonResponse({'granularity': granularity, 'labels': labels, 'datasets': datasets})


@login_required
def account_view(request):
    profile = getattr(request.user, 'profile', None)
    allowed_nodes = profile.allowed_nodes.all().order_by('name') if profile else []
    user = request.user
    ctx = {
        'username': user.username,
        'full_name': user.get_full_name() or user.username,
        'email': user.email or '—',
        'allowed_nodes': allowed_nodes,
        'is_admin': _is_admin(user),
    }
    return render(request, 'telemetry/account.html', ctx)


@login_required
def alarms_catalogue(request):
    return render(request, 'telemetry/catalogue.html')


@login_required
def alert_history_fragment(request, alert_id):
    from django.shortcuts import get_object_or_404
    from django.http import HttpResponse
    user_qs = get_alerts_for_user(request.user)
    alert = get_object_or_404(user_qs, id=alert_id)

    history_page_obj = None
    history_count = 0
    if alert.node_id:
        history_qs = user_qs.filter(
            node=alert.node_id,
        ).exclude(id=alert.id).order_by('-event_time')
        history_paginator = Paginator(history_qs, 6)
        history_count = history_paginator.count
        try:
            history_page = int(request.GET.get('history_page', 1))
        except (TypeError, ValueError):
            history_page = 1
        try:
            history_page_obj = history_paginator.get_page(history_page)
        except Exception:
            history_page_obj = history_paginator.get_page(1)

    ctx = {
        'history_page_obj': history_page_obj,
        'history_count': history_count,
    }
    return render(request, 'telemetry/history_fragment.html', ctx)


@login_required
def create_jira_ticket(request, alert_id):
    from django.shortcuts import get_object_or_404
    import json

    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=405)

    alert = get_object_or_404(
        get_alerts_for_user(request.user),
        id=alert_id,
    )

    data = {}
    if request.body:
        try:
            data = json.loads(request.body)
        except json.JSONDecodeError:
            data = {}

    summary = (data.get("summary") or "").strip() or jira_build_summary(alert)
    description = (data.get("description") or "").strip() or (alert.description or summary)

    key, error = jira_create_ticket(
        summary,
        description,
    )

    if error is not None or not key:
        return JsonResponse(
            {"error": error or "Failed to create Jira ticket"},
            status=502,
        )

    if alert.incident_hash:
        IncidentEvent.objects.create(
            incident_hash=alert.incident_hash,
            alert=alert,
            user=request.user,
            event_type="jira_ticket",
            text=f"Jira ticket created: {key}",
        )

    return JsonResponse(
        {"success": True, "key": key},
    )