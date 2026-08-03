import json
import os
import signal
import time
import asyncio
import logging
import threading
from datetime import datetime, timezone, timedelta

import psycopg2
from channels.layers import get_channel_layer

logger = logging.getLogger("WIS2_AlertListener")
_running = True

ALERT_COLUMNS = (
    'id', 'event_type', 'severity', 'source', 'node', 'title',
    'display_title', 'description', 'event_time',
    'subtype', 'ingested_at', 'incident_hash',
    'channel', 'dataschema',
)


def _hydrate_and_broadcast(conn, uuids, channel_layer, loop):
    """Fetch full alert rows by UUID and broadcast to WebSocket clients."""
    cur = conn.cursor()
    try:
        if uuids:
            cur.execute(
                f"SELECT {', '.join(ALERT_COLUMNS)} FROM alerts WHERE id = ANY(%s::uuid[]) ORDER BY ingested_at ASC",
                (uuids,),
            )
        else:
            return
        rows = cur.fetchall()
    finally:
        cur.close()

    alerts = []
    for row in rows:
        ingested_at = row[10]
        alerts.append({
            'id': str(row[0]),
            'event_type': row[1],
            'severity': row[2],
            'source': row[3],
            'node': str(row[4]) if row[4] else None,
            'node_id': str(row[4]) if row[4] else None,
            'title': row[5],
            'display_title': row[6],
            'description': row[7],
            'event_time': row[8].isoformat() if row[8] else None,
            'subtype': row[9],
            'ingested_at': ingested_at.isoformat() if ingested_at else None,
            'incident_hash': row[11],
            'channel': row[12],
            'dataschema': row[13],
        })

    if alerts:
        loop.run_until_complete(
            channel_layer.group_send('alerts_live', {'type': 'new_alerts', 'alerts': alerts})
        )
        logger.info(f"Broadcast {len(alerts)} alerts to WebSocket clients")


def _catch_up_broadcast(conn, last_seen_at, channel_layer, loop):
    """After reconnection, broadcast any alerts inserted during the offline window."""
    if last_seen_at is None:
        return
    cur = conn.cursor()
    try:
        cur.execute(
            f"SELECT {', '.join(ALERT_COLUMNS)} FROM alerts WHERE ingested_at > %s ORDER BY ingested_at ASC LIMIT 500",
            (last_seen_at,),
        )
        rows = cur.fetchall()
    finally:
        cur.close()

    if not rows:
        return

    uuids = [str(row[0]) for row in rows]
    _hydrate_and_broadcast(conn, uuids, channel_layer, loop)
    logger.info(f"Catch-up: broadcast {len(rows)} alerts missed during disconnection")


def start_alert_listener():
    """Background thread: listens for PostgreSQL NOTIFY and broadcasts to WebSocket clients."""
    global _running
    _running = True

    def _handle_sigterm(signum, frame):
        global _running
        logger.info("SIGTERM reçu. Arrêt en cours...")
        _running = False

    signal.signal(signal.SIGTERM, _handle_sigterm)

    DB_CONFIG = {
        'dbname': os.environ['DB_NAME'],
        'user': os.environ['DB_USER'],
        'password': os.environ['DB_PASSWORD'],
        'host': os.environ.get('DB_HOST', 'localhost'),
        'port': os.environ.get('DB_PORT', '5432'),
    }

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    channel_layer = get_channel_layer()
    last_seen_at = None

    while _running:
        conn = None
        consecutive_errors = 0
        try:
            logger.info("Connecting to PostgreSQL for NOTIFY listener...")
            conn = psycopg2.connect(**DB_CONFIG)
            conn.set_isolation_level(psycopg2.extensions.ISOLATION_LEVEL_AUTOCOMMIT)
            cur = conn.cursor()
            cur.execute("LISTEN wis2_alerts_updates;")
            cur.close()
            logger.info("Listening for wis2_alerts_updates notifications...")

            _catch_up_broadcast(conn, last_seen_at, channel_layer, loop)

            while _running:
                conn.poll()
                while conn.notifies:
                    notify = conn.notifies.pop(0)
                    try:
                        if notify.channel == 'wis2_alerts_updates':
                            try:
                                uuids = json.loads(notify.payload) if notify.payload else []
                            except (json.JSONDecodeError, TypeError):
                                uuids = []
                            _hydrate_and_broadcast(conn, uuids, channel_layer, loop)
                            consecutive_errors = 0
                            last_seen_at = datetime.now(timezone.utc)
                    except Exception as e:
                        logger.error(f"Broadcast error: {e}")
                        consecutive_errors += 1
                        if consecutive_errors >= 3:
                            raise
                time.sleep(0.1)

            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            logger.info("Listener arrêté.")

        except KeyboardInterrupt:
            logger.info("Shutdown requested. Closing listener...")
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            break
        except Exception as e:
            logger.error(f"Listener error: {e}. Reconnecting in 5s...")
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
            time.sleep(5)
