import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
from .models import IncidentMute


class EventConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get('user')
        if not self.user or not self.user.is_authenticated:
            await self.close()
            return
        self.group_name = 'events_live'
        self.joined = False
        self._is_admin_flag = await self._check_admin()
        self._allowed_subjects = await self._check_allowed_subjects()
        self._muted_hashes = await self._muted_hashes()
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        self.joined = True
        await self.accept()

    async def disconnect(self, close_code):
        if getattr(self, 'joined', False):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data):
        pass

    async def new_events(self, event):
        events = event['events']
        if not self._is_admin_flag:
            allowed = self._allowed_subjects
            if allowed is not None:
                events = [a for a in events if (a.get('subject') or a.get('subject_id')) in allowed]
        if self._muted_hashes:
            events = [a for a in events if a.get('incident_hash') not in self._muted_hashes]
        if not events:
            return
        await self.send(text_data=json.dumps({
            'type': 'new_events',
            'events': events,
        }))

    @database_sync_to_async
    def _muted_hashes(self):
        return set(IncidentMute.objects.filter(
            user=self.user, muted_until__gt=timezone.now()
        ).values_list('incident_hash', flat=True))

    @database_sync_to_async
    def _check_admin(self):
        return self.user.is_superuser or self.user.groups.filter(name='Admin').exists()

    @database_sync_to_async
    def _check_allowed_subjects(self):
        profile = getattr(self.user, 'profile', None)
        if profile is None:
            return set()
        subjects = set(profile.allowed_subjects.values_list('name', flat=True))
        return subjects if subjects else set()
