"""Nightly alert-purge scheduler, hosted inside the ingestion process.

Kept in its own module with no import-time side effects so it can be unit
tested without the MQTT/DB environment variables that wis2_ingestion.py
requires at import time.
"""

import logging
import time
from datetime import timedelta
from io import StringIO

from django.core.management import call_command
from django.db import close_old_connections
from django.utils import timezone

PURGE_SCHEDULER_LOGGER = logging.getLogger('WIS2_PurgeScheduler')


def next_purge_target(now, hour=3):
    """Next run time at ``hour``:00 local time.

    Returns today at ``hour``:00 if ``now`` is before it, otherwise tomorrow.
    """
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now >= target:
        target = target + timedelta(days=1)
    return target


def sleep_until(target, stop_fn=None, chunk_secs=60):
    """Sleep until ``target`` (timezone-aware), checking ``stop_fn()`` periodically."""
    stop_fn = stop_fn or (lambda: False)
    while not stop_fn():
        remaining = (target - timezone.now()).total_seconds()
        if remaining <= 0:
            return
        time.sleep(min(chunk_secs, remaining))


def run_purge(logger=None):
    """Execute the purge_alerts management command, never raising."""
    logger = logger or PURGE_SCHEDULER_LOGGER
    try:
        close_old_connections()
        out = StringIO()
        call_command('purge_alerts', stdout=out)
        lines = [ln.strip() for ln in out.getvalue().splitlines() if ln.strip()]
        logger.info('Nightly purge: ' + ' | '.join(lines))
        return True
    except Exception as e:
        logger.error('Nightly purge failed: %s', e)
        return False
    finally:
        close_old_connections()


def purge_scheduler_worker(stop_fn=None, logger=None, hour=3, on_startup=False):
    """Loop: run the purge daily at ``hour``:00 until ``stop_fn()`` becomes True."""
    logger = logger or PURGE_SCHEDULER_LOGGER
    stop_fn = stop_fn or (lambda: False)
    hour = max(0, min(23, int(hour)))

    if on_startup:
        logger.info('PURGE_ON_STARTUP enabled: running purge now...')
        run_purge(logger)

    while not stop_fn():
        now = timezone.localtime(timezone.now())
        target = next_purge_target(now, hour=hour)
        wait_secs = max(0, int((target - timezone.now()).total_seconds()))
        logger.info(
            'Next alert purge at %s (in %dh %dm).',
            target.isoformat(), wait_secs // 3600, (wait_secs % 3600) // 60,
        )
        sleep_until(target, stop_fn)
        if stop_fn():
            break
        run_purge(logger)
