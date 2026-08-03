# WIS2 Monitoring Events

Real-time alert monitoring system for the **WMO Information System 2 (WIS2)** global infrastructure. Ingests WIS2 Notification Messages (WNMs) from the global MQTT broker, stores them in PostgreSQL, and streams them to a web dashboard via WebSockets.

## Architecture

```
┌─────────────────────────────┐
│  WIS2 Global Broker         │  globalbroker.meteo.fr:443
│  MQTT over WebSocket (TLS)  │  topic: monitor/a/wis2/#
└──────────────┬──────────────┘
               │
               ▼
┌─────────────────────────────┐
│  wis2_ingestion.py          │  Standalone daemon (MQTT client)
│  - Parses CloudEvents/WNMs  │  Two threads: MQTT loop + DB writer
│  - Batch inserts (250/5s)   │
│  - Sends pg_notify on insert│
└──────────────┬──────────────┘
               │ INSERT + NOTIFY
               ▼
┌─────────────────────────────┐
│  PostgreSQL                 │  Database: wis2_alerts
│  - alerts table             │  Channel: wis2_alerts_updates
│  - nodes table              │
└───────┬──────────┬──────────┘
        │          │
        │          ▼  LISTEN wis2_alerts_updates
        │  ┌───────────────────────┐
        │  │ telemetry/listeners.py│  Standalone process (wis2_listener.py)
        │  │ - Receives NOTIFY     │  Fetches full rows by UUID
        │  │ - Hydrates alerts     │  Broadcasts via channel_layer.group_send()
        │  └──────────┬────────────┘
        │             │ group_send('alerts_live')
        │             ▼
        │  ┌───────────────────────┐
        │  │ channels_postgres     │  PostgresChannelLayer (NOTIFY/LISTEN)
        │  │ Channel Layer         │  Internal message + group tables
        │  └──────────┬────────────┘
        │             │
        │             ▼
        │  ┌───────────────────────┐
        │  │ telemetry/consumers.py│  AlertConsumer (WebSocket)
        │  │ - RBAC filtering      │  Pushes to connected clients
        │  │ - admin sees all      │
        │  │ - users see own nodes │
        │  └──────────┬────────────┘
        │             │
        ▼             ▼
┌─────────────────────────────┐
│  Django (Daphne ASGI)       │  HTTP + WebSocket
│  /                          │  Dashboard, alerts, incident mgmt
│  /ws/alerts/                │  Live WebSocket stream
└─────────────────────────────┘
```

## Data Pipeline

### Stage 1: MQTT Ingestion (`wis2_ingestion.py`)

- Connects to WIS2 Global Broker via MQTT/WebSocket with TLS
- Subscribes to `monitor/a/wis2/#` (all WIS2 notifications)
- Parses CloudEvents-compliant payloads: extracts `id`, `type`, `source`, `subject` (node), `severity`, `title`, `description`, `wnm`, `errors`, `tests`, `summary`, `links`
- Computes a `display_title` by categorizing known patterns (maintenance, timeouts, HTTP errors, etc.)
- Generates an `incident_hash` (SHA-256 of `title:node`) for grouping related alerts
- Batches inserts every **250 records or 5 seconds** using `psycopg2.extras.execute_values`
- After each batch, sends `pg_notify('wis2_alerts_updates', <uuid_list>)` to wake the listener
- Handles `ForeignKeyViolation` by auto-creating missing nodes
- Writes failed batches to `dead_letter.jsonl` for recovery

### Stage 2: NOTIFY Listener (`telemetry/listeners.py`)

- Standalone process (`python wis2_listener.py`)
- Opens a `psycopg2` connection with `ISOLATION_LEVEL_AUTOCOMMIT` and issues `LISTEN wis2_alerts_updates`
- On notification: parses UUID payload, fetches full alert rows, and calls `channel_layer.group_send('alerts_live', ...)`
- On reconnect: runs a catch-up query for any alerts inserted during the offline window
- Auto-reconnects with backoff on connection loss

### Stage 3: WebSocket Consumer (`telemetry/consumers.py`)

- `AlertConsumer` (AsyncWebsocketConsumer) at `/ws/alerts/`
- On connect: joins `alerts_live` group, checks admin status and allowed nodes
- On `new_alerts` event: filters alerts by user permissions (admin sees all, others see only assigned nodes), sends JSON to client
- Requires authentication (anonymous connections are rejected)

## Project Structure

```
PythonProject2/
├── manage.py
├── requirements.txt
├── .env                          # Environment variables (not committed)
├── .env.example                  # Template for .env
│
├── wis2_ingestion.py             # MQTT → PostgreSQL ingestion daemon
├── wis2_listener.py              # PostgreSQL NOTIFY → WebSocket broadcast
│
├── wis2_monitor/                 # Django project config
│   ├── settings.py               # DB, channels, cache, email config
│   ├── urls.py                   # Root URL routing
│   ├── asgi.py                   # ASGI application (Daphne)
│   └── wsgi.py                   # WSGI fallback
│
├── telemetry/                    # Django app
│   ├── models.py                 # Alert, Node, NodeResponsible, Profile, IncidentEvent, IncidentMute
│   ├── views.py                  # Dashboard, alert list, detail, incident management, APIs
│   ├── consumers.py              # WebSocket consumer (AlertConsumer)
│   ├── listeners.py              # PostgreSQL NOTIFY listener + broadcast logic
│   ├── routing.py                # WebSocket URL routing
│   ├── urls.py                   # HTTP URL routing
│   ├── admin.py                  # Django admin config
│   ├── templatetags/             # Custom template filters
│   ├── migrations/               # Schema + indexes
│   └── templates/                # Dark-themed HTML templates
│
└── staticfiles/                  # Collected static assets
```

## Database Schema

**PostgreSQL** database `wis2_alerts`. Tables are created via SQL migrations (not Django migrations for core tables).

### `alerts` (managed=False — created by SQL migration)

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID | Primary key (CloudEvents `id`) |
| `specversion` | varchar(10) | CloudEvents spec version |
| `event_type` | varchar(255) | WIS2 event type URI |
| `source` | varchar(255) | Event source |
| `node` | varchar(255) | FK → `nodes.name` |
| `event_time` | timestamptz | Event timestamp (UTC) |
| `severity` | varchar(50) | CRITICAL / ERROR / WARNING / INFO |
| `title` | text | Raw alert title |
| `display_title` | text | Categorized title (Maintenance, Timeout, etc.) |
| `description` | text | Alert description |
| `incident_hash` | text | SHA-256(`title:node`) — groups related alerts |
| `wnm` | jsonb | Full WIS2 Notification Message |
| `errors` | jsonb | Quality/validation errors |
| `tests` | jsonb | ETS test results |
| `summary` | jsonb | Test summary (PASSED/FAILED/WARNING/SKIPPED) |
| `links` | jsonb | Related resource links |
| `ingested_at` | timestamptz | Auto-set on insert |

### `nodes` / `node_responsibles` / `node_responsible_mapping`

Unmanaged tables for node metadata and responsible person assignments. The ingestion daemon auto-creates missing nodes on FK violations.

### `incident_events` / `incident_mutes`

Managed by Django for incident tracking: comments, email logs, notes, mute/unmute, view tracking.

## Role-Based Access Control

- **Admin group**: Sees all alerts, full node list, `is_staff` auto-synced
- **Regular users**: See only alerts for nodes assigned via `Profile.allowed_nodes` (M2M)
- WebSocket consumer filters alerts per-user in real time
- Dashboard filter choices are scoped to user's visible nodes

## Setup

### Prerequisites

- Python 3.13+
- PostgreSQL 14+
- SMTP account (Gmail app password for email notifications)

### 1. Clone and install

```bash
git clone <repo-url> && cd PythonProject2
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate    # Linux/macOS
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your values. Key variables:

```env
DJANGO_SECRET_KEY=<generate with: python -c "from django.core.management.utils import get_random_secret_key; print(get_random_secret_key())">
DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1

DB_NAME=wis2_alerts
DB_USER=wis2_admin
DB_PASSWORD=<your-password>

MQTT_BROKER_HOST=globalbroker.meteo.fr
MQTT_BROKER_PORT=443
MQTT_TRANSPORT=websockets
MQTT_WEBSOCKET_PATH=/mqtt
MQTT_USERNAME=everyone
MQTT_PASSWORD=everyone
MQTT_TOPIC=monitor/a/wis2/#
```

### 3. Initialize database

```sql
CREATE DATABASE wis2_alerts;
CREATE USER wis2_admin WITH PASSWORD '<password>';
GRANT ALL PRIVILEGES ON DATABASE wis2_alerts TO wis2_admin;
```

Then run Django migrations:

```bash
python manage.py migrate
python manage.py createsuperuser
```

The `nodes` and `alerts` tables are created by SQL migrations in `telemetry/migrations/`. The ingestion daemon also auto-creates missing nodes on FK violations.

### 4. Start the services

You need **three separate terminals**:

```bash
# Terminal 1 — MQTT ingestion daemon
python wis2_ingestion.py

# Terminal 2 — PostgreSQL NOTIFY listener (broadcasts to WebSockets)
python wis2_listener.py

# Terminal 3 — Django web server (Daphne ASGI)
python manage.py runserver
```

The dashboard is available at `http://localhost:8000/`.

## Alert retention (TTL)

By default alerts are kept forever. You can enable a time-to-live from the Django admin panel:

- Go to **Alert retention policy** in the admin.
- Check **TTL active** to enable purging, then enter a number of days.
- Uncheck **TTL active** to keep alerts forever (the day count is ignored while disabled).
- An impact preview shows roughly how many alerts the current setting would purge.

The cleanup runs via a management command:

```bash
# Preview what would be deleted (recommended first)
python manage.py purge_alerts --dry-run

# Actually purge
python manage.py purge_alerts

# One-off overrides
python manage.py purge_alerts --days 30 --dry-run
python manage.py purge_alerts --limit 5000
```

Purging deletes alerts whose `ingested_at` is older than the cutoff (in batches of 5000),
along with their incident history (`incident_events`). User mute preferences are kept.
Purging only runs when **TTL active** is checked in the admin; otherwise the command is a no-op.

To schedule it nightly on the server, install a systemd timer (adjust the paths to your deploy):

```ini
# /etc/systemd/system/wis2-purge.service
[Unit]
Description=Purge expired WIS2 alerts
[Service]
Type=oneshot
WorkingDirectory=/opt/wis2_monitor
ExecStart=/opt/wis2_monitor/.venv/bin/python manage.py purge_alerts
```

```ini
# /etc/systemd/system/wis2-purge.timer
[Unit]
Description=Run WIS2 alert purge nightly
[Timer]
OnCalendar=*-*-* 03:00:00
RandomizedDelaySec=30min
Persistent=true
[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wis2-purge.timer
```

## API Endpoints

All endpoints require authentication (`@login_required`).

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Dashboard (severity counts, ETS data, KPI data, charts) |
| GET | `/monitor_alerts/` | Paginated alert list with filters |
| GET | `/alert/<uuid>/` | Alert detail (tabs: Tests, Summary, WNM, Errors, Node History) |
| GET | `/api/alerts/` | JSON alert feed (`?since=&offset=&limit=&severity=&node=&type=`) |
| GET | `/api/alerts/per-day/` | Daily aggregated chart data (`?days=14&group_by=severity`) |
| GET | `/api/alert-search/` | Keyword search (`?q=`) -> JSON `{found, count}` |
| GET | `/api/alert/<uuid>/history/` | Paginated node-history HTML fragment |
| POST | `/alert/<uuid>/comment/` | Add incident comment |
| POST | `/alert/<uuid>/note/` | Add timed note (`{"text": "...", "duration": 3600}`) |
| POST | `/alert/<uuid>/mute/` | Mute incident (`{"duration": 7200}`) |
| POST | `/alert/<uuid>/unmute/` | Unmute incident |
| POST | `/alert/<uuid>/email/` | Email responsible person (`{"responsible_ids": [...], "note": "..."}`) |
| GET | `/alert/<uuid>/activity/` | Incident activity feed (paginated) |
| WS | `/ws/alerts/` | WebSocket — real-time alert stream (JSON) |

## Key Design Decisions

- **No Redis/Celery**: The channel layer uses `channels_postgres` (PostgreSQL NOTIFY/LISTEN) instead of Redis. The ingestion → listener pipeline also uses PostgreSQL NOTIFY directly. This keeps the stack to one database.
- **Batch inserts with NOTIFY**: The ingestion daemon batches 250 records and sends a single `pg_notify` with the UUID list, keeping the listener lightweight (it only fetches full rows when notified).
- **Incident hashing**: `SHA-256(title:node)` groups recurring alerts from the same node into a single incident timeline.
- **Dead letter queue**: Failed batches are written to `dead_letter.jsonl` for manual recovery instead of being dropped.
