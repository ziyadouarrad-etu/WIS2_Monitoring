import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
import json
import os
import django
from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'wis2_monitor.settings')
django.setup()
import ssl
import signal
import threading
import queue
import time
import sys
import psycopg2
from psycopg2.extras import execute_values, Json
from datetime import datetime, timezone
import logging
import hashlib
import uuid
import re
from pathlib import Path

from telemetry.purge_scheduler import purge_scheduler_worker


# =====================================================================
# CONFIGURATION DU LOGGING COULEUR (TERMINAL)
# =====================================================================
class ColoredFormatter(logging.Formatter):
    grey = "\x1b[38;20m"
    blue = "\x1b[34;20m"
    green = "\x1b[32;20m"
    yellow = "\x1b[33;20m"
    red = "\x1b[31;20m"
    bold_red = "\x1b[31;1m"
    reset = "\x1b[0m"
    format_str = "[%(asctime)s] [%(levelname)-8s] [%(threadName)-10s] %(message)s"

    FORMATS = {
        logging.DEBUG: grey + format_str + reset,
        logging.INFO: green + format_str + reset,
        logging.WARNING: yellow + format_str + reset,
        logging.ERROR: red + format_str + reset,
        logging.CRITICAL: bold_red + format_str + reset
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno)
        formatter = logging.Formatter(log_fmt, datefmt='%Y-%m-%d %H:%M:%S')
        return formatter.format(record)


logger = logging.getLogger("WIS2_Ingestion")
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(ColoredFormatter())
logger.addHandler(console_handler)

# =====================================================================
# CONFIGURATION SYSTEME
# =====================================================================
BROKER_HOST = os.environ['MQTT_BROKER_HOST']
BROKER_PORT = int(os.environ['MQTT_BROKER_PORT'])
TRANSPORT_TYPE = os.environ['MQTT_TRANSPORT']
WEBSOCKET_PATH = os.environ['MQTT_WEBSOCKET_PATH']
USERNAME = os.environ['MQTT_USERNAME']
PASSWORD = os.environ['MQTT_PASSWORD']
TOPIC = os.environ['MQTT_TOPIC']

DB_CONFIG = {
    "dbname": os.environ['DB_NAME'],
    "user": os.environ['DB_USER'],
    "password": os.environ['DB_PASSWORD'],
    "host": os.environ.get('DB_HOST', 'localhost'),
    "port": os.environ.get('DB_PORT', '5432'),
}

BATCH_SIZE = 250
FLUSH_INTERVAL_SEC = 5
DB_RECONNECT_DELAY_SEC = 5
DB_MAX_RECONNECT_ATTEMPTS = 300
DEAD_LETTER_PATH = Path(__file__).parent / 'dead_letter.jsonl'

telemetry_queue = queue.Queue(maxsize=10000)
SYSTEM_RUNNING = True
DB_CONNECTED = True


# =====================================================================
# MOTEUR D'EXTRACTION & HACHAGE
# =====================================================================


def _write_dead_letter(batch, error):
    """Write failed batch records to dead_letter.jsonl for manual recovery."""
    try:
        with open(DEAD_LETTER_PATH, 'a', encoding='utf-8') as f:
            for record in batch:
                entry = {
                    'timestamp': datetime.now(timezone.utc).isoformat(),
                    'error': str(error),
                    'alert_id': str(record[0]) if record else None,
                }
                f.write(json.dumps(entry) + '\n')
        logger.warning(f"DEAD LETTER: {len(batch)} records written to {DEAD_LETTER_PATH}")
    except Exception as e:
        logger.error(f"Failed to write dead letter: {e}")



def _compute_display_title(title, description):
    t = title or ''
    d = description or ''
    tl = t.lower()
    # Maintenance Categorization
    if 'Template:' in t:
        return 'Maintenance'
    if 'un-wmo-global-test' in t:
        return 'Maintenance'
    if 'CMA Global Monitor' in t:
        return 'Maintenance'
    if 'CMA Global Services' in t:
        return 'Maintenance'
    if 'CMA Global Broker' in t:
        return 'Maintenance'
    if 'GISC Beijing' in t:
        return 'Maintenance'
    if 'DWD Service' in t:
        return 'Maintenance'
    if 'maintenance' in tl:
        return 'Maintenance'
    # Endpoint Errors
    if 'context deadline exceeded' in d:
        return 'Timeout: context deadline exceeded'
    if 'unexpected EOF' in d:
        return 'Network Termination: unexpected EOF'
    if 'GOAWAY' in d:
        return 'Network Termination: server sent GOAWAY'
    if 'client connection lost' in d:
        return 'Network Termination: client connection lost'
    if 'connection refused' in d:
        return 'Connection Refused'
    if 'HTTP status 502' in d:
        return 'HTTP Error: 502 Bad Gateway'
    if 'HTTP status 403' in d:
        return 'HTTP Error: 403 Forbidden'
    if 'expected a valid start token' in d:
        return 'Invalid Response'
    if 'connection reset by peer' in d:
        return 'Network Termination: connection reset by peer'
    if 'i/o timeout' in d:
        return 'Timeout: i/o timeout'
    if t.startswith('Target ') and ' is down' in t:
        return 'Target is down'
    return t


def parse_wmem_record(payload_json):
    """Extrait les champs et génère le hachage cryptographique de l'incident."""
    try:
        event_id = payload_json.get("id")
        if not event_id: return None

        specversion = payload_json.get("specversion", "1.0")
        event_type = payload_json.get("type", "UNKNOWN_TYPE")
        source = payload_json.get("source")
        node = payload_json.get("subject", "UNKNOWN_NODE")
        event_time = payload_json.get("time")
        datacontenttype = payload_json.get("datacontenttype", "application/json")
        dataschema = payload_json.get("dataschema")

        data = payload_json.get("data", {})
        conforms_to = data.get("conformsTo", [])
        severity = data.get("severity", "UNKNOWN")
        subtype = data.get("subtype")

        content = data.get("content", {})
        channel = data.get("channel") or content.get("channel")
        title = content.get("title")
        description = content.get("description")
        wnm = content.get("wnm")
        errors = content.get("errors")
        tests = content.get("tests")
        summary = content.get("summary")
        links = data.get("links") or content.get("links") or payload_json.get("links")

        display_title = _compute_display_title(title, description)

        # ---------------------------------------------------------
        # GENERATION DU INCIDENT HASH (Règle Métier)
        # ---------------------------------------------------------
        safe_title = title if title else "UNTITLED"
        safe_node = node if node else "UNKNOWN_NODE"
        hash_base = f"{safe_title}:{safe_node}"

        incident_hash = hashlib.sha256(hash_base.encode('utf-8')).hexdigest()
        # ---------------------------------------------------------

        return (
            event_id, specversion, event_type, source, node, event_time,
            datacontenttype, dataschema,
            Json(conforms_to) if conforms_to else None,
            severity, subtype, channel, title, description,
            display_title,
            incident_hash,
            Json(wnm) if wnm else None,
            Json(errors) if errors else None,
            Json(tests) if tests else None,
            Json(summary) if summary else None,
            Json(links) if links else None,
            Json(payload_json) if payload_json else None
        )
    except Exception as e:
        logger.warning(f"Parse Warning: Structure invalide - {e}")
        return None


# =====================================================================
# THREAD DE BASE DE DONNÉES (BACKGROUND)
# =====================================================================
def _connect_db():
    """Establish a PostgreSQL connection with retry logic."""
    global SYSTEM_RUNNING
    for attempt in range(1, DB_MAX_RECONNECT_ATTEMPTS + 1):
        if not SYSTEM_RUNNING:
            return None, None
        try:
            logger.info(f"Connexion à PostgreSQL '{DB_CONFIG['dbname']}' (tentative {attempt}/{DB_MAX_RECONNECT_ATTEMPTS})...")
            conn = psycopg2.connect(**DB_CONFIG)
            cursor = conn.cursor()
            logger.info("Connexion PostgreSQL établie avec succès.")
            return conn, cursor
        except Exception as e:
            logger.error(f"Echec de connexion DB: {e}")
            if attempt < DB_MAX_RECONNECT_ATTEMPTS:
                time.sleep(DB_RECONNECT_DELAY_SEC)
    logger.critical("Nombre maximal de tentatives de reconnexion atteint. Arrêt du pipeline.")
    global DB_CONNECTED
    DB_CONNECTED = False
    SYSTEM_RUNNING = False
    return None, None


def db_writer_worker():
    threading.current_thread().name = "DB-Writer"
    global SYSTEM_RUNNING

    conn, cursor = _connect_db()
    if conn is None:
        return

    insert_query = """
                   INSERT INTO alerts (id, specversion, event_type, source, node, event_time, \
                                                       datacontenttype, dataschema, conforms_to, severity, subtype, \
                                                       channel, \
                                                       title, description, display_title, \
                                                       incident_hash, wnm, errors, tests, summary, \
                                                       links, raw_json) \
                   VALUES %s
                   ON CONFLICT (id) DO NOTHING; \
                   """

    batch = []

    while SYSTEM_RUNNING or not telemetry_queue.empty():
        try:
            record = telemetry_queue.get(timeout=FLUSH_INTERVAL_SEC)
            batch.append(record)
        except queue.Empty:
            pass

        if len(batch) >= BATCH_SIZE or (len(batch) > 0 and telemetry_queue.empty()):
            try:
                uuids = [str(r[0]) for r in batch]
                execute_values(cursor, insert_query, batch)
                NOTIFY_LIMIT = 7900
                chunk, chunk_size = [], 2  # start at 2 for []
                for uid in uuids:
                    entry_len = len(uid) + 2 + (1 if chunk else 0)
                    if chunk and chunk_size + entry_len > NOTIFY_LIMIT:
                        cursor.execute("SELECT pg_notify('wis2_alerts_updates', %s)", (json.dumps(chunk),))
                        chunk, chunk_size = [], 2
                    chunk.append(uid)
                    chunk_size += entry_len
                if chunk:
                    cursor.execute("SELECT pg_notify('wis2_alerts_updates', %s)", (json.dumps(chunk),))
                conn.commit()
                logger.info(f"Sync DB réussi: {len(batch)} logs insérés. (File d'attente: {telemetry_queue.qsize()})")
                batch = []
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"Connexion DB perdue: {e}. Tentative de reconnexion...")
                conn, cursor = _connect_db()
                if conn is None:
                    if batch:
                        _write_dead_letter(batch, 'DB connection permanently lost')
                    break
            except psycopg2.errors.ForeignKeyViolation as e:
                try:
                    conn.rollback()
                except Exception:
                    pass
                match = re.search(r'Key\s*\(node\)=\s*\(([^)]+)\)', str(e))
                if match:
                    missing_node = match.group(1)
                    logger.info(f"Noeud manquant détecté: '{missing_node}'. Ajout dans la table nodes...")
                    try:
                        cursor.execute("INSERT INTO nodes (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (missing_node,))
                        conn.commit()
                        logger.info(f"Noeud '{missing_node}' ajouté avec succès. Nouvelle tentative d'insertion du lot...")
                        continue
                    except Exception as insert_err:
                        logger.error(f"Echec d'ajout du noeud '{missing_node}': {insert_err}")
                        try:
                            conn.rollback()
                        except Exception:
                            pass
                        _write_dead_letter(batch, insert_err)
                        batch = []
                        continue
                else:
                    logger.error(f"Impossible d'extraire le noeud manquant de l'erreur: {e}")
                    _write_dead_letter(batch, e)
                    batch = []
                    continue
            except Exception as e:
                logger.error(f"ECHEC D'INSERTION - Rollback de la transaction: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass
                _write_dead_letter(batch, e)
                batch = []

    if conn:
        cursor.close()
        conn.close()
    logger.info("Connexion DB fermée proprement.")


# =====================================================================
# CALLBACKS MQTT
# =====================================================================
def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        logger.info(f"Tunnel WebSocket établi vers {BROKER_HOST}.")
        client.subscribe(TOPIC)
        logger.info(f"Abonnement aux topics: {TOPIC}")
    else:
        logger.error(f"Echec d'authentification (Code: {reason_code})")


def on_message(client, userdata, msg):
    try:
        if not DB_CONNECTED:
            return
        payload_str = msg.payload.decode('utf-8')
        payload_json = json.loads(payload_str)

        db_record = parse_wmem_record(payload_json)

        if db_record:
            telemetry_queue.put(db_record)
            logger.debug(f"Capturé: {msg.topic}")

    except json.JSONDecodeError:
        logger.warning(f"Payload non-JSON ignoré sur le topic: {msg.topic}")
    except Exception as e:
        logger.error(f"Exception dans on_message: {e}")


def on_disconnect(client, userdata, flags, reason_code, properties):
    if reason_code != 0:
        logger.warning(f"Déconnexion inattendue du WebSocket (Code: {reason_code}). Reconnexion...")


# =====================================================================
# INITIALISATION
# =====================================================================
def start_ingestion_node():
    global SYSTEM_RUNNING
    threading.current_thread().name = "Main-MQTT"

    print("=" * 70)
    print("   NOEUD DE TELEMETRIE WIS2 (MQTT -> POSTGRESQL)")
    print("=" * 70)

    db_thread = threading.Thread(target=db_writer_worker, daemon=True)
    db_thread.start()

    purge_hour = int(os.environ.get('PURGE_HOUR', '3') or '3')
    purge_on_startup = os.environ.get('PURGE_ON_STARTUP', '').strip().lower() in ('1', 'true', 'yes', 'on')
    purge_thread = threading.Thread(
        target=purge_scheduler_worker,
        kwargs={
            'stop_fn': lambda: not SYSTEM_RUNNING,
            'hour': purge_hour,
            'on_startup': purge_on_startup,
        },
        name="Purge-Scheduler",
        daemon=True,
    )
    purge_thread.start()
    logger.info(
        f"Nightly purge scheduler active (hour={purge_hour:02d}:00, on_startup={purge_on_startup})."
    )

    # Generate a dynamic 8-character hash for the client ID
    unique_node_id = f"wis2-telemetry-node-{uuid.uuid4().hex[:8]}"

    client = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id=unique_node_id,
        transport=TRANSPORT_TYPE
    )

    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.username_pw_set(USERNAME, PASSWORD)
    client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS)
    client.ws_set_options(path=WEBSOCKET_PATH)

    def _handle_sigterm(signum, frame):
        logger.warning("SIGTERM reçu. Arrêt en cours...")
        client.disconnect()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        logger.info("Exécution du handshake WebSocket...")
        client.connect(BROKER_HOST, BROKER_PORT, keepalive=60)
        logger.info("Boucle réseau active... Appuyez sur Ctrl+C pour arrêter.")
        client.loop_forever()
    except KeyboardInterrupt:
        print("\n")
        logger.warning("Signal d'arrêt reçu. Fermeture de la boucle réseau...")
    except Exception as e:
        logger.critical(f"Erreur fatale de connexion MQTT: {e}")
    finally:
        SYSTEM_RUNNING = False
        client.disconnect()
        logger.info("Attente que le thread DB écrive les dernières données...")
        db_thread.join(timeout=30)
        purge_thread.join(timeout=5)
        logger.info("Arrêt complet du système.")


if __name__ == "__main__":
    start_ingestion_node()
