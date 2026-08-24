# WIS2 Monitoring Events

Real-time event monitoring system for the **WMO Information System 2 (WIS2)** global infrastructure. Ingests WIS2 Notification Messages (WNMs) from the global MQTT broker, stores them in PostgreSQL, and streams them to a web dashboard via WebSockets.

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
│  - events table             │  Channel: wis2_events_updates
│  - subjects table           │
└───────┬──────────┬──────────┘
        │          │
        │          ▼  LISTEN wis2_events_updates
        │  ┌───────────────────────┐
        │  │ telemetry/listeners.py│  Standalone process (wis2_listener.py)
        │  │ - Receives NOTIFY     │  Fetches full rows by UUID
        │  │ - Hydrates events     │  Broadcasts via channel_layer.group_send()
        │  └──────────┬────────────┘
        │             │ group_send('events_live')
        │             ▼
        │  ┌───────────────────────┐
        │  │ channels_postgres     │  PostgresChannelLayer (NOTIFY/LISTEN)
        │  │ Channel Layer         │  Internal message + group tables
        │  └──────────┬────────────┘
        │             │
        │             ▼
        │  ┌───────────────────────┐
        │  │ telemetry/consumers.py│  EventConsumer (WebSocket)
        │  │ - RBAC filtering      │  Pushes to connected clients
         │  │ - admin sees all      │
         │  │ - users see own subjects │
        │  └──────────┬────────────┘
        │             │
        ▼             ▼
┌─────────────────────────────┐
│  Django (Daphne ASGI)       │  HTTP + WebSocket
│  /                          │  Dashboard, events, incident mgmt
│  /ws/events/                │  Live WebSocket stream
└─────────────────────────────┘
```

## Data Pipeline

### Stage 1: MQTT Ingestion (`wis2_ingestion.py`)

- Connects to WIS2 Global Broker via MQTT/WebSocket with TLS
- Subscribes to `monitor/a/wis2/#` (all WIS2 notifications)
- Parses CloudEvents-compliant payloads: extracts `id`, `type`, `source`, `subject`, `severity`, `title`, `description`, `wnm`, `errors`, `tests`, `summary`, `links`
- Computes a `display_title` by categorizing known patterns (maintenance, timeouts, HTTP errors, etc.)
- Generates an `incident_hash` (SHA-256 of `title:subject`) for grouping related events
- Batches inserts every **250 records or 5 seconds** using `psycopg2.extras.execute_values`
- After each batch, sends `pg_notify('wis2_events_updates', <uuid_list>)` to wake the listener
- Handles `ForeignKeyViolation` by auto-creating missing subjects

### Stage 2: NOTIFY Listener (`telemetry/listeners.py`)

- Standalone process (`python wis2_listener.py`)
- Opens a `psycopg2` connection with `ISOLATION_LEVEL_AUTOCOMMIT` and issues `LISTEN wis2_events_updates`
- On notification: parses UUID payload, fetches full event rows, and calls `channel_layer.group_send('events_live', ...)`
- On reconnect: runs a catch-up query for any events inserted during the offline window
- Auto-reconnects with backoff on connection loss

### Stage 3: WebSocket Consumer (`telemetry/consumers.py`)

- `EventConsumer` (AsyncWebsocketConsumer) at `/ws/events/`
- On connect: joins `events_live` group, checks admin status and allowed subjects
- On `new_events` event: filters events by user permissions (admin sees all, others see only assigned subjects), sends JSON to client
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
│   ├── models.py                 # Event, Subject, SubjectResponsible, Profile, IncidentActivity, IncidentMute
│   ├── views.py                  # Dashboard, event list, detail, incident management, APIs
│   ├── consumers.py              # WebSocket consumer (EventConsumer)
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

### `events` (managed=False — created by SQL migration)

| Column | Type | Notes |
|--------|------|-------|
| `id` | UUID | Primary key (CloudEvents `id`) |
| `specversion` | varchar(10) | CloudEvents spec version |
| `event_type` | varchar(255) | WIS2 event type URI |
| `source` | varchar(255) | Event source |
| `subject` | varchar(255) | FK → `subjects.name` |
| `event_time` | timestamptz | Event timestamp (UTC) |
| `severity` | varchar(50) | CRITICAL / ERROR / WARNING / INFO |
| `title` | text | Raw event title |
| `display_title` | text | Categorized title (Maintenance, Timeout, etc.) |
| `description` | text | Event description |
| `incident_hash` | text | SHA-256(`title:subject`) — groups related events |
| `wnm` | jsonb | Full WIS2 Notification Message |
| `errors` | jsonb | Quality/validation errors |
| `tests` | jsonb | ETS test results |
| `summary` | jsonb | Test summary (PASSED/FAILED/WARNING/SKIPPED) |
| `links` | jsonb | Related resource links |
| `ingested_at` | timestamptz | Auto-set on insert |

### `subjects` / `subject_responsibles` / `subject_responsible_mapping`

Unmanaged tables for subject metadata and responsible person assignments. The ingestion daemon auto-creates missing subjects on FK violations.

### `incident_events` / `incident_mutes`

Managed by Django for incident tracking: comments, email logs, notes, mute/unmute, view tracking.

## Role-Based Access Control

- **Admin group**: Sees all events, full subject list, `is_staff` auto-synced
- **Regular users**: See only events for subjects assigned via `Profile.allowed_subjects` (M2M)
- WebSocket consumer filters events per-user in real time
- Dashboard filter choices are scoped to user's visible subjects

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

The `subjects` and `events` tables are created by SQL migrations in `telemetry/migrations/`. The ingestion daemon also auto-creates missing subjects on FK violations.

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

## Event retention (TTL)

By default events are kept forever. You can enable a time-to-live from the Django admin panel:

- Go to **Event retention policy** in the admin.
- Check **TTL active** to enable purging, then enter a number of days.
- Uncheck **TTL active** to keep events forever (the day count is ignored while disabled).
- An impact preview shows roughly how many events the current setting would purge.

The cleanup runs via a management command:

```bash
# Preview what would be deleted (recommended first)
python manage.py purge_events --dry-run

# Actually purge
python manage.py purge_events

# One-off overrides
python manage.py purge_events --days 30 --dry-run
python manage.py purge_events --limit 5000
```

Purging deletes events whose `ingested_at` is older than the cutoff (in batches of 5000),
along with their incident history (`incident_events`). User mute preferences are kept.
Purging only runs when **TTL active** is checked in the admin; otherwise the command is a no-op.

### Automatic nightly run (in-app scheduler)

The purge runs automatically every night at **03:00** (server time, UTC by default),
scheduled **inside the ingestion process** (`wis2-ingestion`). The scheduler is a
dedicated thread in `wis2_ingestion.py` — no systemd timer or external scheduler is
needed. On the server, `sudo systemctl restart wis2-ingestion` picks up the code; locally,
restart the ingestion script the same way you normally would.

Configuration (environment variables):

| Variable | Default | Description |
|----------|---------|-------------|
| `PURGE_HOUR` | `3` | Hour of day (0-23, local time) at which the purge runs. |
| `PURGE_ON_STARTUP` | `false` | When `true`, run the purge once as soon as the ingestion starts (handy to clear an existing backlog after enabling TTL; keep `false` otherwise). |

Example (`/etc/systemd/system/wis2-ingestion.service` Environment= or `.env`):

```bash
PURGE_HOUR=3
PURGE_ON_STARTUP=false
```

To clear the backlog immediately without waiting for 03:00, run
`python manage.py purge_events --dry-run` first, then `python manage.py purge_events`.

## API Endpoints

All endpoints require authentication (`@login_required`).

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Dashboard (severity counts, ETS data, KPI data, charts) |
| GET | `/monitor_events/` | Paginated event list with filters |
| GET | `/event/<uuid>/` | Event detail (tabs: Tests, Summary, WNM, Errors, Subject History) |
| GET | `/api/events/` | JSON event feed (`?since=&offset=&limit=&severity=&subject=&type=`) |
| GET | `/api/events/per-day/` | Daily aggregated chart data (`?days=14&group_by=severity`) |
| GET | `/api/event-search/` | Keyword search (`?q=`) -> JSON `{found, count}` |
| GET | `/api/event/<uuid>/history/` | Paginated subject-history HTML fragment |
| POST | `/event/<uuid>/comment/` | Add incident comment |
| POST | `/event/<uuid>/note/` | Add timed note (`{"text": "...", "duration": 3600}`) |
| POST | `/event/<uuid>/mute/` | Mute incident (`{"duration": 7200}`) |
| POST | `/event/<uuid>/unmute/` | Unmute incident |
| POST | `/event/<uuid>/email/` | Email responsible person (`{"responsible_ids": [...], "note": "..."}`) |
| POST | `/event/<uuid>/jira/` | Create a Jira ticket for the event (summary = display_title + title when different, description = event description) |
| POST | `/event/<uuid>/explain/` | Conversational explanation of the event via a local Ollama LLM (`{"message": "...", "history": [...]}` → `{"reply": "..."}`); answers are grounded in event facts, incident history, and responsible contacts; returns 502 when Ollama is not configured |
| GET | `/event/<uuid>/activity/` | Incident activity feed (paginated) |
| WS | `/ws/events/` | WebSocket — real-time event stream (JSON) |

## Key Design Decisions

- **No Redis/Celery**: The channel layer uses `channels_postgres` (PostgreSQL NOTIFY/LISTEN) instead of Redis. The ingestion → listener pipeline also uses PostgreSQL NOTIFY directly. This keeps the stack to one database.
- **Batch inserts with NOTIFY**: The ingestion daemon batches 250 records and sends a single `pg_notify` with the UUID list, keeping the listener lightweight (it only fetches full rows when notified).
- **Incident hashing**: `SHA-256(title:subject)` groups recurring events from the same subject into a single incident timeline.

## Services
- wis2-daphne
- wis2-ingestion
- wis2-listener

## Display Title Maintenance

`display_title` is computed **once, at ingestion time** by `_compute_display_title()` (`wis2_ingestion.py`). Rules are matched top-to-bottom (first match wins); an event matching no rule keeps the publisher's raw title (e.g., hostname). Editing rules never fixes already-stored rows — that is what the backfill script is for.

### When a new alert shows an unreadable title

**Case A — a recurring error pattern has no rule yet**

1. Open the event detail page → `{ } See Full Raw JSON` → read the real description text.
2. Add a branch to `_compute_display_title()` in `wis2_ingestion.py`, placed **before** more generic branches. The `Unknown Error: no details` branch must stay **last**.
3. Add a test case in `ComputeDisplayTitleTest` (`telemetry/tests.py`).
4. Mirror the branch in `scripts/recompute_display_titles.sql` — the CASE logic exists in **three places** there (preview block ×2 and the UPDATE block) and must stay in the same order as the Python function.
5. Deploy and heal history:

```bash
git push                      # local
# on the server:
git pull && sudo systemctl restart wis2-ingestion
psql -h localhost -U wis2_admin -d wis2_alerts -f scripts/recompute_display_titles.sql
```

**Case B — the pattern is already covered by a generic rule**

Do nothing for future events; they categorize automatically at ingest. Only rows stored *before* the rule was deployed need a backfill re-run (same `psql` command as above).

**Case C — one-off junk that matches no rule**

Ignore it. The raw-title fallback is intentional; only add a rule if the same junk recurs across multiple events/subjects.

### Backfill safety notes

- The script is idempotent: it only updates rows whose recomputed title differs (`IS DISTINCT FROM` guard).
- Regex quoting gotcha: `\yEOF\y` must stay a plain string literal (no `E''` prefix) so Postgres receives literal word-boundary backslashes. See the header of the script for details.

## WIS2 Systemd Service Management

### 1. Modify a service

Service files are located in:

```bash
/etc/systemd/system/
```

Edit a service with:

```bash
sudo nano /etc/systemd/system/wis2-daphne.service
sudo nano /etc/systemd/system/wis2-ingestion.service
sudo nano /etc/systemd/system/wis2-listener.service
```

After modifying a service file, always reload systemd:

```bash
sudo systemctl daemon-reload
```

If you modified the `[Unit]` or `[Install]` sections, the service should contain:

```ini
[Unit]
PartOf=wis2.target
```

and:

```ini
[Install]
WantedBy=wis2.target
```

---

### 2. Start services

**Individual service:**

```bash
sudo systemctl start wis2-daphne.service
sudo systemctl start wis2-ingestion.service
sudo systemctl start wis2-listener.service
```

**All three through the umbrella target:**

```bash
sudo systemctl start wis2.target
```

**Multiple individual services:**

```bash
sudo systemctl start wis2-daphne.service wis2-ingestion.service
```

---

### 3. Stop services

**Individual service:**

```bash
sudo systemctl stop wis2-daphne.service
sudo systemctl stop wis2-ingestion.service
sudo systemctl stop wis2-listener.service
```

**The umbrella target:**

```bash
sudo systemctl stop wis2.target
```

---

### 4. Restart services

**Individual service:**

```bash
sudo systemctl restart wis2-daphne.service
sudo systemctl restart wis2-ingestion.service
sudo systemctl restart wis2-listener.service
```

**Entire WIS2 stack:**

```bash
sudo systemctl restart wis2.target
```

---

### 5. Check status

**Individual service:**

```bash
sudo systemctl status wis2-daphne.service
sudo systemctl status wis2-ingestion.service
sudo systemctl status wis2-listener.service
```

**All three at once:**

```bash
sudo systemctl status wis2-daphne.service wis2-ingestion.service wis2-listener.service
```

**Umbrella target:**

```bash
sudo systemctl status wis2.target
```

Look for:

```text
Active: active (running)
```

for services, and:

```text
Active: active
```

for the target.

---

### 6. Enable / disable at boot

The individual services remain independently enabled:

```bash
sudo systemctl enable wis2-daphne.service
sudo systemctl enable wis2-ingestion.service
sudo systemctl enable wis2-listener.service
```

The umbrella target is also enabled:

```bash
sudo systemctl enable wis2.target
```

To prevent an individual service from starting automatically at boot:

```bash
sudo systemctl disable wis2-daphne.service
```

**Note:** `disable` does not stop a currently running service. Use `stop` separately if needed.

---

### 7. After editing systemd files

The standard sequence is:

```bash
sudo systemctl daemon-reload
sudo systemctl restart <service>
sudo systemctl status <service>
```

For example:

```bash
sudo systemctl daemon-reload
sudo systemctl restart wis2-ingestion.service
sudo systemctl status wis2-ingestion.service
```

### Quick reference

| Action             | Command                                      |
| ------------------ | -------------------------------------------- |
| Start one          | `sudo systemctl start wis2-daphne.service`   |
| Stop one           | `sudo systemctl stop wis2-daphne.service`    |
| Restart one        | `sudo systemctl restart wis2-daphne.service` |
| Status one         | `sudo systemctl status wis2-daphne.service`  |
| Start everything   | `sudo systemctl start wis2.target`           |
| Stop everything    | `sudo systemctl stop wis2.target`            |
| Restart everything | `sudo systemctl restart wis2.target`         |
| Status everything  | `sudo systemctl status wis2.target`          |
| Reload systemd     | `sudo systemctl daemon-reload`               |
| Enable at boot     | `sudo systemctl enable <service>`            |
| Disable at boot    | `sudo systemctl disable <service>`           |
