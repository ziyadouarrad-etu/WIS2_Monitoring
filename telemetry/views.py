from django.shortcuts import render
from django.http import JsonResponse
from django.db.models import Q, Max
from django.core.paginator import Paginator
from django.core.cache import cache
from .models import Alert


def dashboard(request):
    severities = ['CRITICAL', 'ERROR', 'WARNING', 'INFO']
    severity = request.GET.getlist('severity')
    severity = [s for s in severity if s]
    search_subject = request.GET.getlist('subject')
    search_subject = [s for s in search_subject if s]
    search_type = request.GET.getlist('type')
    search_type = [s for s in search_type if s]
    search_title = request.GET.getlist('title')
    search_title = [s for s in search_title if s]
    search_source = request.GET.getlist('source')
    search_source = [s for s in search_source if s]
    time_from = request.GET.get('time_from', '').strip()
    time_to = request.GET.get('time_to', '').strip()
    effective_type = request.GET.getlist('effective_type')
    effective_type = [s for s in effective_type if s]
    sort = request.GET.get('sort', 'event_time')
    if sort not in ('event_time', 'ingested_at'):
        sort = 'event_time'

    qs = Alert.objects.all()

    if severity:
        qs = qs.filter(severity__in=severity)

    if search_subject:
        qs = qs.filter(subject__in=search_subject)

    if search_type:
        qs = qs.filter(event_type__in=search_type)

    if search_title:
        qs = qs.filter(title__in=search_title)

    if search_source:
        qs = qs.filter(source__in=search_source)

    if time_from:
        qs = qs.filter(event_time__gte=time_from)
    if time_to:
        qs = qs.filter(event_time__lte=time_to)

    if effective_type:
        etq = Q()
        if 'down_nodes' in effective_type:
            etq |= Q(title__icontains='is down')
        if 'total_disconnections' in effective_type:
            etq |= Q(title__icontains='from all Global Brokers')
        if 'partial_disconnections' in effective_type:
            etq |= Q(title__icontains='from multiple Global Brokers')
        if 'single_disconnections' in effective_type:
            etq |= Q(title__icontains='from one Global Broker')
        if 'silenced_data' in effective_type:
            etq |= Q(title__icontains='no cache is receiving data') | Q(title__icontains='no data is received')
        if 'ets_reports' in effective_type:
            etq |= Q(summary__isnull=False) | Q(tests__isnull=False)
        if 'global_cache' in effective_type:
            etq |= Q(title__icontains='Global Cache') | Q(title__icontains='Global cache')
        if 'maintenance' in effective_type:
            etq |= Q(title__icontains='maintenance') | Q(description__icontains='maintenance')
        if 'quality_alerts' in effective_type:
            etq |= Q(errors__isnull=False)
        qs = qs.filter(etq)

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

    CACHE_TTL = 86400

    type_choices = cache.get('dashboard_type_choices')
    if type_choices is None:
        type_choices = list(
            Alert.objects
                .filter(event_type__isnull=False).exclude(event_type='')
                .values_list('event_type', flat=True).distinct().order_by('event_type')[:500]
        )
        cache.set('dashboard_type_choices', type_choices, CACHE_TTL)

    subject_choices = cache.get('dashboard_subject_choices')
    if subject_choices is None:
        subject_choices = list(
            Alert.objects
                .filter(subject__isnull=False).exclude(subject='')
                .values_list('subject', flat=True).distinct().order_by('subject')
        )
        cache.set('dashboard_subject_choices', subject_choices, CACHE_TTL)

    source_choices = cache.get('dashboard_source_choices')
    if source_choices is None:
        source_choices = list(
            Alert.objects
                .filter(source__isnull=False).exclude(source='')
                .values_list('source', flat=True).distinct().order_by('source')
        )
        cache.set('dashboard_source_choices', source_choices, CACHE_TTL)

    title_choices = cache.get('dashboard_title_choices')
    if title_choices is None:
        title_choices = list(
            Alert.objects
                .filter(title__isnull=False).exclude(title='')
                .order_by('title').values_list('title', flat=True).distinct('title')[:500]
        )
        cache.set('dashboard_title_choices', title_choices, CACHE_TTL)

    return render(request, 'telemetry/dashboard.html', {
        'alerts': page_obj.object_list,
        'page_obj': page_obj,
        'latest_ingested_iso': latest_ingested_iso,
        'base_query': base_query,
        'type_choices': type_choices,
        'subject_choices': subject_choices,
        'source_choices': source_choices,
        'title_choices': title_choices,
        'current_severities': severity,
        'current_subjects': search_subject,
        'current_types': search_type,
        'current_titles': search_title,
        'current_sources': search_source,
        'current_time_from': time_from,
        'current_time_to': time_to,
        'current_effective_types': effective_type,
        'current_sort': sort,
    })


def api_alerts(request):
    since = request.GET.get('since')
    offset = int(request.GET.get('offset', 0))
    limit = int(request.GET.get('limit', 50))
    severity = request.GET.getlist('severity')
    severity = [s for s in severity if s]
    search_subject = request.GET.getlist('subject')
    search_subject = [s for s in search_subject if s]
    search_type = request.GET.getlist('type')
    search_type = [s for s in search_type if s]
    search_title = request.GET.getlist('title')
    search_title = [s for s in search_title if s]
    search_source = request.GET.getlist('source')
    search_source = [s for s in search_source if s]
    time_from = request.GET.get('time_from', '').strip()
    time_to = request.GET.get('time_to', '').strip()
    effective_type = request.GET.getlist('effective_type')
    effective_type = [s for s in effective_type if s]
    sort = request.GET.get('sort', 'event_time')
    if sort not in ('event_time', 'ingested_at'):
        sort = 'event_time'

    qs = Alert.objects.all()

    if since:
        qs = qs.filter(ingested_at__gt=since)
    if time_from:
        qs = qs.filter(event_time__gte=time_from)
    if time_to:
        qs = qs.filter(event_time__lte=time_to)
    if severity:
        qs = qs.filter(severity__in=severity)
    if search_subject:
        qs = qs.filter(subject__in=search_subject)
    if search_type:
        qs = qs.filter(event_type__in=search_type)
    if search_title:
        qs = qs.filter(title__in=search_title)
    if search_source:
        qs = qs.filter(source__in=search_source)
    if effective_type:
        etq = Q()
        if 'down_nodes' in effective_type:
            etq |= Q(title__icontains='is down')
        if 'total_disconnections' in effective_type:
            etq |= Q(title__icontains='from all Global Brokers')
        if 'partial_disconnections' in effective_type:
            etq |= Q(title__icontains='from multiple Global Brokers')
        if 'single_disconnections' in effective_type:
            etq |= Q(title__icontains='from one Global Broker')
        if 'silenced_data' in effective_type:
            etq |= Q(title__icontains='no cache is receiving data') | Q(title__icontains='no data is received')
        if 'ets_reports' in effective_type:
            etq |= Q(summary__isnull=False) | Q(tests__isnull=False)
        if 'global_cache' in effective_type:
            etq |= Q(title__icontains='Global Cache') | Q(title__icontains='Global cache')
        if 'maintenance' in effective_type:
            etq |= Q(title__icontains='maintenance') | Q(description__icontains='maintenance')
        if 'quality_alerts' in effective_type:
            etq |= Q(errors__isnull=False)
        qs = qs.filter(etq)

    qs = qs.order_by(f'-{sort}')[offset:offset+limit]
    data = [
        {
            'id': str(a.id),
            'event_type': a.event_type,
            'severity': a.severity,
            'source': a.source,
            'subject': a.subject,
            'title': a.title,
            'description': a.description,
            'event_time': a.event_time.isoformat(),
            'subtype': a.subtype,
            'ingested_at': a.ingested_at.isoformat(),
        }
        for a in qs
    ]
    return JsonResponse({'alerts': data, 'count': len(data)})


def alert_detail(request, alert_id):
    from django.shortcuts import get_object_or_404
    alert = get_object_or_404(Alert, id=alert_id)

    tests_list = []
    if alert.tests:
        if isinstance(alert.tests, list):
            tests_list = alert.tests
        elif isinstance(alert.tests, dict):
            tests_list = [alert.tests]

    summary_dict = alert.summary if isinstance(alert.summary, dict) else {}

    history_qs = Alert.objects.none()
    history_page_obj = None
    history_count = 0
    if alert.subject:
        history_qs = Alert.objects.filter(
            subject=alert.subject
        ).exclude(id=alert.id).order_by('-event_time')
        history_count = history_qs.count()
        history_paginator = Paginator(history_qs, 6)
        try:
            history_page = int(request.GET.get('history_page', 1))
        except (TypeError, ValueError):
            history_page = 1
        try:
            history_page_obj = history_paginator.get_page(history_page)
        except Exception:
            history_page_obj = history_paginator.get_page(1)

    return render(request, 'telemetry/detail.html', {
        'alert': alert,
        'tests_list': tests_list,
        'summary_dict': summary_dict,
        'history_page_obj': history_page_obj,
        'history_count': history_count,
    })
