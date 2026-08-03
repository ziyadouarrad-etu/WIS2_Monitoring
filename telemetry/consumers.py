import json
from channels.generic.websocket import AsyncWebsocketConsumer
from channels.db import database_sync_to_async
from django.utils import timezone
from .models import IncidentMute


class AlertConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.user = self.scope.get('user')
        if not self.user or not self.user.is_authenticated:
            await self.close()
            return
        self.group_name = 'alerts_live'
        self.joined = False
        self._is_admin_flag = await self._check_admin()
        self._allowed_nodes = await self._check_allowed_nodes()
        self._muted_hashes = await self._muted_hashes()
        await self.channel_layer.group_add(self.group_name, self.channel_name)
        self.joined = True
        await self.accept()

    async def disconnect(self, close_code):
        if getattr(self, 'joined', False):
            await self.channel_layer.group_discard(self.group_name, self.channel_name)

    async def receive(self, text_data):
        pass

    async def new_alerts(self, event):
        alerts = event['alerts']
        if not self._is_admin_flag:
            allowed = self._allowed_nodes
            if allowed is not None:
                alerts = [a for a in alerts if (a.get('node') or a.get('node_id')) in allowed]
        if self._muted_hashes:
            alerts = [a for a in alerts if a.get('incident_hash') not in self._muted_hashes]
        if not alerts:
            return
        await self.send(text_data=json.dumps({
            'type': 'new_alerts',
            'alerts': alerts,
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
    def _check_allowed_nodes(self):
        profile = getattr(self.user, 'profile', None)
        if profile is None:
            return set()
        nodes = set(profile.allowed_nodes.values_list('name', flat=True))
        return nodes if nodes else set()
