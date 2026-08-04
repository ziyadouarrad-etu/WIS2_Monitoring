import json
from django import template
from django.utils.html import escape
from django.utils.safestring import mark_safe

register = template.Library()


@register.filter
def event_type_label(value):
    if not value:
        return ''
    parts = value.split('.')
    return ' '.join(parts[-2:]) if len(parts) >= 2 else value


@register.filter
def score_level(k):
    """Map a KPI test to a 0-4 color bucket based on its percentage score.
    0 = red (0-25%), 1 = orange (25-50%), 2 = yellow (50-75%),
    3 = light green (75-<100%), 4 = green (100%). Returns None for no score."""
    if not isinstance(k, dict):
        return None
    p = k.get('percentage')
    if p is None and k.get('score') is not None and k.get('total'):
        try:
            p = (k['score'] / k['total']) * 100
        except (TypeError, ZeroDivisionError, ValueError):
            p = None
    if p is None:
        return None
    try:
        p = float(p)
    except (TypeError, ValueError):
        return None
    if p >= 100:
        return 4
    if p >= 75:
        return 3
    if p >= 50:
        return 2
    if p >= 25:
        return 1
    return 0


# ---------------------------------------------------------------------------
# WNM (WIS2 Notification Message) rendering
#
# Walks the WNM tree and maps it onto the structured layout the CSS already
# defines:
#   - flat scalar key/values  -> compact grid (.wnm-deflist / .wnm-dt-row)
#   - nested dicts/lists      -> boxed sub-panels (.wnm-nested-block)
#   - lists of plain scalars  -> pill chips (.wnm-chips), URI-aware
#   - identity / geometry / links keep their dedicated, hand-tuned renderers
# ---------------------------------------------------------------------------

_MAX_CHIPS = 20
_MAX_INLINE_LIST = 12


def _fmt_bytes(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return str(n)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} GB'


def _fmt_scalar(v):
    if isinstance(v, bool):
        return 'true' if v else 'false'
    return str(v)


def _is_empty(v):
    return v is None or v in ('', [], {})


def _looks_like_uri(v):
    return isinstance(v, str) and ('://' in v or v.startswith('urn:'))


def _render_chips(items, uri_like=False):
    """Pill-style rendering for lists of plain scalars (e.g. conformsTo URIs)."""
    cls = 'wnm-chip wnm-chip-uri' if uri_like else 'wnm-chip'
    shown = items[:_MAX_CHIPS]
    chips = ''.join(f'<span class="{cls}">{escape(_fmt_scalar(v))}</span>' for v in shown)
    if len(items) > _MAX_CHIPS:
        chips += f'<span class="wnm-chip">+{len(items) - _MAX_CHIPS} more</span>'
    return f'<div class="wnm-chips">{chips}</div>'


def _render_scalar_list(items):
    """Render a list known to contain only str/int/float/bool values."""
    if not items:
        return ''
    if all(_looks_like_uri(v) for v in items):
        return _render_chips(items, uri_like=True)
    if len(items) <= 8:
        return _render_chips(items)
    display = ', '.join(escape(_fmt_scalar(v)) for v in items[:_MAX_INLINE_LIST])
    if len(items) > _MAX_INLINE_LIST:
        display += f' &hellip; (+{len(items) - _MAX_INLINE_LIST})'
    return f'<div class="wnm-value">{display}</div>'


def _render_nested_row(key, val):
    """A single label/value line inside a .wnm-nested-block."""
    label = escape(str(key))
    if isinstance(val, dict) and val:
        return _render_nested_block(key, val)
    if isinstance(val, list) and val:
        if all(isinstance(v, (str, int, float, bool)) for v in val):
            inner = _render_scalar_list(val)
            return (
                f'<div class="wnm-nested-row"><span class="wnm-nested-label">{label}</span>'
                f'<span class="wnm-nested-value">{inner}</span></div>'
            )
        return _render_nested_block(key, val)
    if _is_empty(val):
        return ''
    value = escape(_fmt_scalar(val))
    return (
        f'<div class="wnm-nested-row"><span class="wnm-nested-label">{label}</span>'
        f'<span class="wnm-nested-value">{value}</span></div>'
    )


def _render_nested_block(key, val):
    """A boxed sub-panel for a nested dict or non-trivial list."""
    label = escape(str(key))

    if isinstance(val, dict):
        body = ''.join(_render_nested_row(k, v) for k, v in val.items() if not _is_empty(v))
        if not body:
            return ''
        return f'<div class="wnm-nested-block"><div class="wnm-nested-title">{label}</div>{body}</div>'

    if isinstance(val, list):
        if not val:
            return ''
        if all(isinstance(v, (str, int, float, bool)) for v in val):
            inner = _render_scalar_list(val)
            return f'<div class="wnm-nested-block"><div class="wnm-nested-title">{label}</div>{inner}</div>'
        items = ''.join(
            _render_nested_block(f'item {i + 1}', item) if isinstance(item, (dict, list))
            else _render_nested_row(f'item {i + 1}', item)
            for i, item in enumerate(val)
        )
        if not items:
            return ''
        return f'<div class="wnm-nested-block"><div class="wnm-nested-title">{label}</div>{items}</div>'

    return ''


def _render_props(d):
    """Render a flat dict of properties: simple scalars go in a deflist grid,
    nested dicts/lists fall through to boxed nested blocks below it."""
    simple_rows = []
    complex_blocks = []

    for k, v in d.items():
        if _is_empty(v):
            continue
        label = escape(str(k))

        if isinstance(v, dict):
            block = _render_nested_block(k, v)
            if block:
                complex_blocks.append(block)
        elif isinstance(v, list):
            if all(isinstance(item, (str, int, float, bool)) for item in v):
                inner = _render_scalar_list(v)
                simple_rows.append(
                    f'<div class="wnm-dt-row"><span class="wnm-dt">{label}</span>'
                    f'<div class="wnm-dd">{inner}</div></div>'
                )
            else:
                block = _render_nested_block(k, v)
                if block:
                    complex_blocks.append(block)
        else:
            value = escape(_fmt_scalar(v))
            simple_rows.append(
                f'<div class="wnm-dt-row"><span class="wnm-dt">{label}</span>'
                f'<span class="wnm-dd">{value}</span></div>'
            )

    html = ''
    if simple_rows:
        html += '<div class="wnm-deflist">' + ''.join(simple_rows) + '</div>'
    if complex_blocks:
        html += ''.join(complex_blocks)
    return html


def _render_identity(value):
    """Identity fields (id / type / specversion) rendered as labeled rows."""
    rows = []
    if value.get('id'):
        rows.append(
            '<div class="wnm-dt-row"><span class="wnm-dt">id</span>'
            f'<span class="wnm-dd">{escape(str(value["id"]))}</span></div>'
        )
    if value.get('type'):
        rows.append(
            '<div class="wnm-dt-row"><span class="wnm-dt">type</span>'
            f'<span class="wnm-dd wnm-badge wnm-badge-type">{escape(str(value["type"]))}</span></div>'
        )
    if value.get('specversion'):
        rows.append(
            '<div class="wnm-dt-row"><span class="wnm-dt">specversion</span>'
            f'<span class="wnm-dd wnm-badge wnm-badge-spec">{escape(str(value["specversion"]))}</span></div>'
        )
    if not rows:
        return ''
    return '<div class="wnm-deflist">' + ''.join(rows) + '</div>'


def _render_geometry(geom):
    if not isinstance(geom, dict):
        return ''
    gtype = geom.get('type', '')
    coords = geom.get('coordinates')
    if coords is None:
        return ''

    if gtype == 'Point' and len(coords) >= 2:
        try:
            lon, lat = float(coords[0]), float(coords[1])
            summary = f'Point &middot; {lat:.5f}, {lon:.5f}'
        except (TypeError, ValueError):
            summary = 'Point'
    else:
        def _count(c):
            if isinstance(c, (list, tuple)):
                if c and isinstance(c[0], (list, tuple)):
                    return sum(_count(x) for x in c)
                return 1
            return 0
        n = _count(coords)
        summary = f'{escape(str(gtype)) or "Geometry"} &middot; {n} point{"s" if n != 1 else ""}'

    raw = escape(json.dumps(geom, indent=2))
    return (
        '<div class="wnm-geo">'
        f'<span class="wnm-geo-pin">&#128205;</span><span class="wnm-geo-summary">{summary}</span>'
        f'<details class="wnm-geo-raw"><summary>raw</summary><pre>{raw}</pre></details>'
        '</div>'
    )


def _render_links(links):
    if not links:
        return ''
    cards = []
    for link in links:
        if not isinstance(link, dict):
            continue
        href = str(link.get('href') or '')
        rel = str(link.get('rel') or '')
        mime = str(link.get('type') or '')
        length = link.get('length')
        title = link.get('title')
        hreflang = link.get('hreflang')
        rel_class = 'wnm-rel-default'

        href_esc = escape(href)
        href_short = href if len(href) <= 64 else href[:34] + '\u2026' + href[-22:]
        href_short = escape(href_short)

        badges = []
        if rel:
            badges.append(f'<span class="wnm-rel-badge {rel_class}">{escape(rel)}</span>')
        if mime:
            badges.append(f'<span class="wnm-mime-badge">{escape(mime)}</span>')
        if hreflang:
            badges.append(f'<span class="wnm-mime-badge">{escape(str(hreflang))}</span>')

        meta_bits = []
        if length is not None:
            meta_bits.append(_fmt_bytes(length))
        if title:
            meta_bits.append(escape(str(title)))

        SAFE_SCHEMES = ('http://', 'https://')
        if href and not href.lower().startswith(SAFE_SCHEMES):
            link_html = f'<span class="wnm-link-href wnm-link-unsafe">{href_esc}</span>'
        elif href:
            link_html = f'<a class="wnm-link-href" href="{href_esc}" target="_blank" rel="noopener" title="{href_esc}">{href_short}</a>'
        else:
            link_html = '<span class="wnm-link-href wnm-link-href-empty">no href</span>'

        cards.append(
            '<div class="wnm-link-card">'
            '<div class="wnm-link-head">' + ''.join(badges) + '</div>'
            + link_html
            + (f'<div class="wnm-link-meta">{" &middot; ".join(meta_bits)}</div>' if meta_bits else '')
            + '</div>'
        )

    if not cards:
        return ''
    return f'<div class="wnm-links">{"".join(cards)}</div>'


@register.filter
def render_wnm(value):
    if not value:
        return mark_safe('<div class="empty-val">No WNM data.</div>')
    try:
        if isinstance(value, str):
            value = json.loads(value)
        if not isinstance(value, dict):
            return mark_safe(f'<pre class="json-block" style="margin:0">{escape(json.dumps(value, indent=2))}</pre>')

        sections = []

        # Identity section — labeled rows for id / type / specversion
        r = _render_identity(value)
        if r:
            sections.append(
                '<div class="wnm-section">'
                '<div class="wnm-section-label">Identity</div>'
                + r + '</div>'
            )

        # Every other non-empty key gets its own bordered section
        for key in value:
            if key in ('id', 'type', 'specversion'):
                continue
            v = value[key]
            if _is_empty(v):
                continue

            label = escape(str(key))

            if key == 'geometry':
                r = _render_geometry(v)
            elif key == 'links':
                r = _render_links(v)
            elif isinstance(v, dict):
                r = _render_props(v)
            elif isinstance(v, list):
                if all(isinstance(item, dict) for item in v):
                    r = ''.join(_render_nested_block(f'item {i + 1}', item) for i, item in enumerate(v))
                elif all(isinstance(item, (str, int, float, bool)) for item in v):
                    r = _render_scalar_list(v)
                else:
                    r = ''.join(
                        _render_nested_block(f'item {i + 1}', item) if isinstance(item, (dict, list))
                        else (
                            f'<div class="wnm-dt-row"><span class="wnm-dt">item {i + 1}</span>'
                            f'<span class="wnm-dd">{escape(_fmt_scalar(item))}</span></div>'
                        )
                        for i, item in enumerate(v)
                    )
            else:
                r = f'<div class="wnm-row"><span class="wnm-value">{escape(_fmt_scalar(v))}</span></div>'

            if r:
                sections.append(
                    '<div class="wnm-section">'
                    '<div class="wnm-section-label">' + label + '</div>'
                    + r + '</div>'
                )

        sections = [s for s in sections if s]
        if not sections:
            return mark_safe(f'<pre class="json-block" style="margin:0">{escape(json.dumps(value, indent=2))}</pre>')

        return mark_safe('<div class="wnm-block">' + ''.join(sections) + '</div>')
    except Exception:
        fallback = json.dumps(value, indent=2) if isinstance(value, (dict, list)) else str(value)
        return mark_safe(f'<pre class="json-block" style="margin:0">{escape(fallback)}</pre>')


def _render_error_item(error):
    if isinstance(error, str):
        return f'<div class="err-msg">{escape(error)}</div>'
    lines = []
    for k, v in error.items():
        if isinstance(v, str):
            lines.append(
                f'<div class="err-row"><span class="err-label">{escape(str(k))}</span>'
                f'<span class="err-value">{escape(v)}</span></div>'
            )
        elif isinstance(v, list):
            items = '; '.join(escape(str(x)) for x in v[:5])
            lines.append(
                f'<div class="err-row"><span class="err-label">{escape(str(k))}</span>'
                f'<span class="err-value">{items}{"..." if len(v) > 5 else ""}</span></div>'
            )
    return ''.join(lines)


@register.filter
def render_errors(value):
    if not value:
        return mark_safe('<div class="empty-val">No errors.</div>')
    try:
        if isinstance(value, str):
            value = json.loads(value)
        items = []
        if isinstance(value, list):
            for error in value:
                items.append('<div class="err-card">' + _render_error_item(error) + '</div>')
        elif isinstance(value, dict):
            items.append('<div class="err-card">' + _render_error_item(value) + '</div>')
        else:
            return mark_safe(f'<pre class="json-block json-block-errors" style="margin:0">{escape(json.dumps(value, indent=2))}</pre>')

        if not items:
            return mark_safe('<div class="empty-val">No errors.</div>')
        return mark_safe('<div class="err-block">' + ''.join(items) + '</div>')
    except Exception:
        fallback = json.dumps(value, indent=2) if isinstance(value, (dict, list)) else value
        return mark_safe(f'<pre class="json-block json-block-errors" style="margin:0">{escape(fallback)}</pre>')