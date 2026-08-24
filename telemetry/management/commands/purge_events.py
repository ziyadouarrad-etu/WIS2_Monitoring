from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from telemetry.models import Event, IncidentActivity, get_retention_days

BATCH_SIZE = 5000


class Command(BaseCommand):
    help = (
        "Delete events older than the configured retention period (EventRetentionPolicy "
        "in the admin panel). No-op when the policy is unset or empty (persistence mode)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=None,
            help='Override the configured retention in days for this run.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report how many events would be purged without deleting anything.',
        )
        parser.add_argument(
            '--limit', type=int, default=None,
            help='Cap the total number of events purged in this run.',
        )

    def handle(self, *args, **options):
        if options['days'] is not None and options['days'] <= 0:
            raise CommandError('--days must be a positive integer.')
        if options['limit'] is not None and options['limit'] <= 0:
            raise CommandError('--limit must be a positive integer.')

        days = options['days'] or get_retention_days()
        dry_run = options['dry_run']
        limit = options['limit']

        if not days:
            self.stdout.write(self.style.WARNING(
                'Persistence mode: no retention configured, nothing to purge.'
            ))
            return

        cutoff = timezone.now() - timedelta(days=days)
        self.stdout.write(f'Retention: purging events ingested before {cutoff.isoformat()} '
                          f'(older than {days} days).')

        if dry_run:
            total = Event.objects.filter(ingested_at__lt=cutoff).count()
            if limit is not None:
                total = min(total, limit)
            self.stdout.write(f'  would purge {total} event(s) (dry-run)')
            self.stdout.write(self.style.SUCCESS(
                f'Done: {total} event(s) would be purged (dry-run).'
            ))
            return

        purged = 0
        while limit is None or purged < limit:
            batch_ids = list(
                Event.objects.filter(ingested_at__lt=cutoff)
                .values_list('id', flat=True)[:BATCH_SIZE]
            )
            if not batch_ids:
                break
            batch_ids = batch_ids[: limit - purged] if limit else batch_ids
            if not batch_ids:
                break

            with transaction.atomic():
                IncidentActivity.objects.filter(event_id__in=batch_ids).delete()
                deleted, _ = Event.objects.filter(id__in=batch_ids).delete()
            purged += deleted
            self.stdout.write(f'  purged {deleted} event(s)')
            if len(batch_ids) < BATCH_SIZE:
                break

        if purged:
            self.stdout.write(self.style.SUCCESS(f'Done: {purged} event(s) purged.'))
        else:
            self.stdout.write('No events to purge.')
