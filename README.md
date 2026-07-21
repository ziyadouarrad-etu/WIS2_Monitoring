# WIS2 Global Telemetry Monitor

A real-time telemetry monitoring system for the **WIS2 (WMO Information System 2)** global infrastructure. The system ingests WIS2 Notification Messages (WNM) from a MQTT broker, stores them in PostgreSQL, and provides a web dashboard for visualization, filtering, and incident analysis.

## Architecture

```
MQTT Global Broker ──► wis2_ingestion.py ──► PostgreSQL ──► Django Web App
(WebSocket/MQTT)         (Python daemon)     (Database)     (Dashboard + API)
```

## Components

### `wis2_ingestion.py`
Standalone Python daemon that:
- Connects to the WIS2 Global Broker (`globalbroker.meteo.fr`) via MQTT over WebSockets (TLS)
- Subscribes to `monitor/a/wis2/#` topics
- Parses incoming CloudEvents/WIS2 Notification Messages
- Batches inserts into PostgreSQL (every 250 records or 5 seconds)
- Runs the MQTT loop and DB writer on separate threads

### `wis2_monitor/`
Django project configuration:
- **settings.py**: PostgreSQL connection, Django 6.0 config, `telemetry` app, in-memory cache
- **urls.py**: Routes `/admin/` to Django Admin and `/` to the telemetry app

### `telemetry/`
Django app providing the web interface:
- **models.py**: `Alert` model (UUID PK, event metadata, JSON fields for WNM/errors/tests/summary/links)
- **views.py**: Three views:
  - `dashboard` — Paginated alert list with filters (severity, type, node, source, time range, effective type), sorting, and live polling
  - `api_alerts` — JSON endpoint for real-time polling (supports `since`, `offset`/`limit`, and all dashboard filters)
  - `alert_detail` — Full incident detail with tabs (Tests, Summary, WNM, Errors) and node history
- **templates/**: Dark-themed, responsive HTML with custom CSS (monospace/display fonts, severity-colored indicators, animated live badge)
- **templatetags/**: Custom template filters for:
  - `event_type_label` — Human-readable event type names
  - `render_wnm` — Structured WNM tree rendering (identity chips, geometry, links, nested blocks)
  - `render_errors` — Error card rendering
- **admin.py**: Django admin with list display/filter/search
- **migrations/**: Schema + performance indexes (event_time DESC, ingested_at, severity/event_time composite, event_type, partial index on errors)

## Database

PostgreSQL database `wis2_alerts` with table `alerts`:

| Column | Type | Description |
|--------|------|-------------|
| `id` | UUID | Primary key (from CloudEvents `id`) |
| `specversion` | varchar(10) | CloudEvents spec version |
| `event_type` | varchar(255) | WIS2 event type URI |
| `source` | varchar(255) | Event source identifier |
| `node` | varchar(255) | Node identifier |
| `event_time` | timestamptz | Event timestamp (UTC) |
| `severity` | varchar(50) | CRITICAL, ERROR, WARNING, INFO |
| `title` | text | Alert title |
| `description` | text | Alert description |
| `incident_hash` | text | SHA-256 deduplication hash |
| `wnm` | jsonb | Full WIS2 Notification Message |
| `errors` | jsonb | Quality/validation errors |
| `tests` | jsonb | ETS test results |
| `summary` | jsonb | Test summary (PASSED/FAILED/WARNING/SKIPPED counts) |
| `links` | jsonb | Related resource links |
| `ingested_at` | timestamptz | Auto-set ingestion timestamp |

## Setup

1. **Prerequisites**: Python 3.13+, PostgreSQL, virtual environment
2. **Install dependencies**:
   ```bash
   pip install django psycopg2 paho-mqtt
   ```
3. **Configure database** in `wis2_monitor/settings.py` and `wis2_ingestion.py`
4. **Run migrations**:
   ```bash
   python manage.py migrate
   ```
5. **Start the ingestion daemon**:
   ```bash
   python wis2_ingestion.py
   ```
6. **Start the web server**:
   ```bash
   python manage.py runserver
   ```

## Dashboard Features

- **Live polling**: Automatically fetches new alerts every 5 seconds via `/api/alerts/`
- **Filters**: Severity, type, node, source, time range, and effective type (down nodes, disconnections, silenced data, ETS reports, global cache, maintenance, quality alerts)
- **Sorting**: By event time or ingestion time
- **Pagination**: 10 alerts per page
- **Incident detail**: Tabbed view (Tests, Summary, WNM, Errors) with node history pagination
