import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
import json
import os
import ssl
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


logger = logging.getLogger("WIS2_Node")
logger.setLevel(logging.INFO)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(ColoredFormatter())
logger.addHandler(console_handler)

# =====================================================================
# CONFIGURATION SYSTEME
# =====================================================================
BROKER_HOST =  "globalbroker.meteo.fr"
BROKER_PORT = 443
TRANSPORT_TYPE = "websockets"
WEBSOCKET_PATH = "/mqtt"
USERNAME = "everyone"
PASSWORD = "everyone"
TOPIC = "monitor/a/wis2/#"

DB_CONFIG = {
    "dbname": "wis2_alerts",
    "user": "wis2_admin",
    "password": "marocmeteo@",
    "host": "localhost",
    "port":"5432",
}

BATCH_SIZE = 250
FLUSH_INTERVAL_SEC = 5
DB_RECONNECT_DELAY_SEC = 5
DB_MAX_RECONNECT_ATTEMPTS = 10

telemetry_queue = queue.Queue()
SYSTEM_RUNNING = True


# =====================================================================
# MOTEUR D'EXTRACTION & HACHAGE
# =====================================================================
def parse_wmem_record(payload_json):
    """Extrait les champs et génère le hachage cryptographique de l'incident."""
    try:
        event_id = payload_json.get("id")
        if not event_id: return None

        specversion = payload_json.get("specversion", "1.0")
        event_type = payload_json.get("type", "UNKNOWN_TYPE")
        source = payload_json.get("source")
        subject = payload_json.get("subject", "UNKNOWN_SUBJECT")
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

        # ---------------------------------------------------------
        # GENERATION DU INCIDENT HASH (Règle Métier)
        # ---------------------------------------------------------
        safe_title = title if title else "UNTITLED"
        safe_subject = subject if subject else "UNKNOWN_SUBJECT"
        hash_base = f"{safe_title}:{safe_subject}"

        incident_hash = hashlib.sha256(hash_base.encode('utf-8')).hexdigest()
        # ---------------------------------------------------------

        return (
            event_id, specversion, event_type, source, subject, event_time,
            datacontenttype, dataschema,
            Json(conforms_to) if conforms_to else None,
            severity, subtype, channel, title, description,
            incident_hash,  # <-- Injection du hash dans la base de données
            Json(wnm) if wnm else None,
            Json(errors) if errors else None,
            Json(tests) if tests else None,
            Json(summary) if summary else None,
            Json(links) if links else None
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
    SYSTEM_RUNNING = False
    return None, None


def db_writer_worker():
    threading.current_thread().name = "DB-Writer"
    global SYSTEM_RUNNING

    conn, cursor = _connect_db()
    if conn is None:
        return

    insert_query = """
                   INSERT INTO alerts (id, specversion, event_type, source, subject, event_time, \
                                                      datacontenttype, dataschema, conforms_to, severity, subtype, \
                                                      channel, \
                                                      title, description, incident_hash, wnm, errors, tests, summary, \
                                                      links) \
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
                execute_values(cursor, insert_query, batch)
                conn.commit()
                logger.info(f"Sync DB réussi: {len(batch)} logs insérés. (File d'attente: {telemetry_queue.qsize()})")
                batch = []
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
                logger.warning(f"Connexion DB perdue: {e}. Tentative de reconnexion...")
                conn, cursor = _connect_db()
                if conn is None:
                    break
                # Keep batch for retry on next iteration
            except Exception as e:
                logger.error(f"ECHEC D'INSERTION - Rollback de la transaction: {e}")
                try:
                    conn.rollback()
                except Exception:
                    pass
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
        logger.info("Arrêt complet du système.")


if __name__ == "__main__":
    start_ingestion_node()