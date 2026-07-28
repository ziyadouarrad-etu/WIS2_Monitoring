import re
import json
from django.shortcuts import render
from django.http import JsonResponse, FileResponse
from django.views.decorators.http import require_POST
from django.db.models import Q, Max, Count
from django.db.models.functions import TruncDay

from django.core.paginator import Paginator
from django.core.cache import cache
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from datetime import timedelta
from .models import Alert, Node, IncidentEvent, IncidentMute


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
FILTER_FIELDS = ['severity', 'node', 'type', 'alert', 'source']


def parse_filter_params(request):
    params = {}
    params['severity'] = [s for s in request.GET.getlist('severity') if s]
    params['node'] = [s for s in request.GET.getlist('node') if s]
    params['type'] = [s for s in request.GET.getlist('type') if s]
    params['alert'] = [s for s in request.GET.getlist('alert') if s]
    params['source'] = [s for s in request.GET.getlist('source') if s]
    params['time_from'] = request.GET.get('time_from', '').strip()
    params['time_to'] = request.GET.get('time_to', '').strip()
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
    return qs


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


def _user_queryset(user):
    if _is_admin(user):
        return Alert.objects.all()
    profile = getattr(user, 'profile', None)
    if profile is not None:
        allowed_nodes = profile.allowed_nodes.all()
        if allowed_nodes:
            return Alert.objects.filter(node__in=allowed_nodes)
    return Alert.objects.none()


def get_type_choices(user):
    suffix = '' if _is_admin(user) else f'_{user.id}'
    return get_cached_choices(
        f'dashboard_type_choices{suffix}',
        _user_queryset(user).filter(event_type__isnull=False).exclude(event_type='')
        .values_list('event_type', flat=True).distinct().order_by('event_type')[:500],
    )


def get_source_choices(user):
    suffix = '' if _is_admin(user) else f'_{user.id}'
    return get_cached_choices(
        f'dashboard_source_choices{suffix}',
        _user_queryset(user).filter(source__isnull=False).exclude(source='')
        .values_list('source', flat=True).distinct().order_by('source'),
    )


def get_alert_choices(user):
    suffix = '' if _is_admin(user) else f'_{user.id}'
    return get_cached_choices(
        f'dashboard_alert_choices{suffix}',
        _user_queryset(user).filter(display_title__isnull=False).exclude(display_title='')
        .order_by('display_title').values_list('display_title', flat=True).distinct('display_title')[:500],
    )


@login_required
def dashboard(request):
    qs = get_alerts_for_user(request.user)

    time_window = request.GET.get('window', 'all')
    now_tz = timezone.now()

    qs_cards = qs
    if time_window == '12h':
        since = now_tz - timedelta(hours=12)
        qs_cards = qs.filter(event_time__gte=since)
    elif time_window == '24h':
        since = now_tz - timedelta(hours=24)
        qs_cards = qs.filter(event_time__gte=since)
    else:
        time_window = 'all'

    sev_counts_qs = qs_cards.values('severity').annotate(cnt=Count('id'))
    severity_counts = {s: 0 for s in ['CRITICAL', 'ERROR', 'WARNING', 'INFO']}
    for row in sev_counts_qs:
        severity_counts[row['severity']] = row['cnt']

    urgent_hours = request.GET.get('urgent_hours', '24')
    urgent_from = request.GET.get('urgent_from', '')
    urgent_to = request.GET.get('urgent_to', '')
    now_tz = timezone.now()
    if urgent_hours == 'custom' and urgent_from and urgent_to:
        try:
            urgent_start = parse_datetime(urgent_from.replace('T', ' '))
            urgent_end = parse_datetime(urgent_to.replace('T', ' '))
        except Exception:
            urgent_start = now_tz - timedelta(hours=24)
            urgent_end = now_tz
    else:
        try:
            hours = max(1, int(urgent_hours))
        except (ValueError, TypeError):
            hours = 24
        urgent_start = now_tz - timedelta(hours=hours)
        urgent_end = now_tz
    urgent_alerts = qs.filter(
        severity='CRITICAL',
        event_time__gte=urgent_start,
        event_time__lte=urgent_end
    ).only('id', 'severity', 'title', 'event_type', 'node_id', 'event_time').order_by('-event_time')[:15]
    urgent_from_display = urgent_from or urgent_start.strftime('%Y-%m-%dT%H:%M')
    urgent_to_display = urgent_to or urgent_end.strftime('%Y-%m-%dT%H:%M')

    mini_alerts = qs.only('id', 'ingested_at', 'severity', 'node_id', 'title', 'display_title', 'event_type', 'event_time').order_by('-ingested_at')[:20]

    critical_alerts = qs.filter(severity='CRITICAL').only('id', 'ingested_at', 'severity', 'node_id', 'title', 'display_title', 'event_type', 'event_time').order_by('-ingested_at')[:20]

    sev_order = ['CRITICAL', 'ERROR', 'WARNING', 'INFO']

    # Event Type counts (show all types, 0 if none match)
    all_types = list(qs.values_list('event_type', flat=True).distinct().order_by('event_type'))
    type_counts_data = qs_cards.values('event_type').annotate(cnt=Count('id'))
    type_count_map = {t['event_type']: t['cnt'] for t in type_counts_data}
    type_display = [(t or 'Unknown', type_count_map.get(t, 0)) for t in all_types]

    # ETS Reports – latest per node ever that had an issue
    ets_rows = (
        qs_cards.filter(Q(summary__isnull=False) | Q(tests__isnull=False))
        .order_by('node_id', '-event_time')
        .distinct('node_id')
        .values('node_id', 'severity', 'title', 'id', 'event_time', 'summary', 'tests')
    )
    ets_data = [{
        'node_id': r['node_id'],
        'severity': r['severity'],
        'title': r['title'],
        'id': str(r['id']),
        'event_time': r['event_time'],
        'summary': r['summary'] if isinstance(r['summary'], dict) else {},
        'has_issue': False,
    } for r in ets_rows]
    for e in ets_data:
        s = e['summary']
        passed = s.get('PASSED', 0)
        total = sum(v for v in s.values() if isinstance(v, (int, float)))
        if isinstance(total, (int, float)) and isinstance(passed, (int, float)):
            e['total'] = total
            e['passed'] = passed
            e['has_issue'] = total != passed
        else:
            e['total'] = 0
            e['passed'] = 0
            e['has_issue'] = True

    type_choices = get_type_choices(request.user)
    node_choices = get_node_choices(request.user)
    source_choices = get_source_choices(request.user)

    return render(request, 'telemetry/dashboard.html', {
        'severity_counts': severity_counts,
        'urgent_alerts': urgent_alerts,
        'mini_alerts': mini_alerts,
        'critical_alerts': critical_alerts,

        'type_choices': type_choices,
        'node_choices': node_choices,
        'source_choices': source_choices,
        'urgent_hours': urgent_hours,
        'urgent_from_display': urgent_from_display,
        'urgent_to_display': urgent_to_display,
        'ets_data': ets_data,
        'ets_issue_count': sum(1 for e in ets_data if e['has_issue']),
        'ets_has_issues': any(e['has_issue'] for e in ets_data),
        'type_display': type_display,
        'time_window': time_window,
    })


@login_required
def monitor_alerts(request):
    severities = ['CRITICAL', 'ERROR', 'WARNING', 'INFO']
    params = parse_filter_params(request)
    sort = params['sort']

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
        'current_types': params['type'],
        'current_alerts': params['alert'],
        'current_sources': params['source'],
        'current_time_from': params['time_from'],
        'current_time_to': params['time_to'],
        'current_sort': sort,
    })


@login_required
def api_alerts(request):
    since = request.GET.get('since')
    try:
        offset = int(request.GET.get('offset', 0))
        limit = int(request.GET.get('limit', 50))
    except (TypeError, ValueError):
        offset, limit = 0, 50
    params = parse_filter_params(request)

    qs = get_alerts_for_user(request.user)

    if since:
        since_dt = parse_datetime(since)
        if since_dt is None:
            since = re.sub(r' (\d{2}:\d{2})$', r'+\1', since)
            since_dt = parse_datetime(since)
        if since_dt is not None:
            qs = qs.filter(ingested_at__gt=since_dt)
    qs = apply_filters(qs, params)
    qs = exclude_muted(qs, request.user)

    qs = qs.only('id', 'event_type', 'severity', 'source', 'node_id', 'title', 'display_title', 'description', 'event_time', 'subtype', 'ingested_at').order_by(f'-{params["sort"]}')[offset:offset+limit]
    data = [
        {
            'id': str(a.id),
            'event_type': a.event_type,
            'severity': a.severity,
            'source': a.source,
            'node': a.node_id,
            'node_id': a.node_id,
            'title': a.title,
            'display_title': a.display_title,
            'description': a.description,
            'event_time': a.event_time.isoformat(),
            'subtype': a.subtype,
            'ingested_at': a.ingested_at.isoformat(),
        }
        for a in qs
    ]
    return JsonResponse({'alerts': data, 'count': len(data)})


@login_required
def alert_exists(request, alert_id):
    from django.http import JsonResponse
    exists = get_alerts_for_user(request.user).filter(id=alert_id).exists()
    return JsonResponse({'exists': exists})


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

    summary_dict = alert.summary if isinstance(alert.summary, dict) else {}

    history_page_obj = None
    history_count = 0
    if alert.node_id:
        history_qs = user_qs.filter(
            node=alert.node_id,
            event_time__gte=timezone.now() - timedelta(days=30),
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
        'tests_list': tests_list,
        'summary_dict': summary_dict,
        'history_page_obj': history_page_obj,
        'history_count': history_count,
        'node_responsibles': node_responsibles,
    })


SEV_COLORS = {'CRITICAL': '#FF4444', 'ERROR': '#FF8800', 'WARNING': '#FFD600', 'INFO': '#448AFF'}
PALETTE = ['#4DD0C4', '#AB47BC', '#78909C', '#26A69A', '#EF5350', '#42A5F5', '#FFA726', '#66BB6A', '#EC407A', '#8D6E63']

def _type_label(v):
    if not v:
        return ''
    parts = v.split('.')
    return ' '.join(parts[-2:]) if len(parts) >= 2 else v


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

    qs = get_alerts_for_user(request.user)
    since = timezone.now() - timedelta(days=days)
    qs = qs.filter(event_time__gte=since)
    qs = exclude_muted(qs, request.user)
    if nodes:
        qs = qs.filter(node_id__in=nodes)

    today = timezone.now().date()
    start_date = today - timedelta(days=days - 1)
    all_date_keys = [(start_date + timedelta(days=i)).strftime('%Y-%m-%d') for i in range(days)]

    trunc = TruncDay('event_time')
    group_field = 'severity' if group_by == 'severity' else 'event_type'
    data = (
        qs.annotate(day=trunc)
        .values('day', group_field)
        .annotate(count=Count('id'))
    )

    day_map = {}
    group_set = set()
    for d in data:
        g = d[group_field] or 'Unknown'
        day_key = d['day'].strftime('%Y-%m-%d') if d['day'] else ''
        group_set.add(g)
        if day_key not in day_map:
            day_map[day_key] = {}
        day_map[day_key][g] = d['count']

    labels = all_date_keys
    all_groups = sorted(group_set)

    datasets = []
    for g in all_groups:
        color = SEV_COLORS.get(g)
        if not color:
            color = PALETTE[len(datasets) % len(PALETTE)]
        datasets.append({
            'label': _type_label(g) if group_by == 'event_type' else g,
            'data': [day_map.get(d, {}).get(g, 0) for d in labels],
            'borderColor': color,
            'backgroundColor': color + '20',
            'fill': False,
            'tension': 0.1,
        })

    return JsonResponse({'labels': labels, 'datasets': datasets})


import os
from django.conf import settings


def logo_image(request):
    path = os.path.join(settings.BASE_DIR, 'telemetry', 'static', 'telemetry', 'img.png')
    return FileResponse(open(path, 'rb'), content_type='image/png')


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
            event_time__gte=timezone.now() - timedelta(days=30),
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


