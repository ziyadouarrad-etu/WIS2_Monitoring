import os
import logging
import sys
import django
from dotenv import load_dotenv

load_dotenv()
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'wis2_monitor.settings')
django.setup()

logging.basicConfig(
    level=logging.INFO,
    format='[%(asctime)s] [%(levelname)-8s] %(name)s %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    stream=sys.stdout,
)

from telemetry.listeners import start_alert_listener


if __name__ == "__main__":
    start_alert_listener()
