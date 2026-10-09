"""Self-describing AI music tools calling the same service as Discord UI."""
import json
import logging

from tools import Tool
from .service import ACTIONS, MusicError, READ_ACTIONS

log = logging.getLogger(__name__)
SETTINGS_SCHEMA = {'type': 'object', 'additionalProperties': False, 'properties': {
    'default_channel': {'type': ['string', 'null']}, 'dj_role': {'type': ['string', 'null']},
    'listener_controls': {'type': 'boolean'}, 'idle_seconds': {'type': 'integer', 'minimum': 30, 'maximum': 3600},
    'queue_limit': {'type': 'integer', 'minimum': 1, 'maximum': 100},
    'user_queue_limit': {'type': 'integer', 'minimum': 1, 'maximum': 100}}}
PARAMETERS = {'type': 'object', 'additionalProperties': False, 'properties': {
    'action': {'type': 'string', 'enum': list(ACTIONS)},
    'query': {'type': 'string', 'maxLength': 400, 'description': 'Song title, artist or HTTPS YouTube video/playlist URL.'},
    'queries': {'type': 'array', 'minItems': 1, 'maxItems': 10, 'items': {'type': 'string', 'maxLength': 400}},
    'channel': {'type': 'string', 'maxLength': 100, 'description': 'Voice channel name, ID, or mention; omit to use requester voice channel.'},
    'mode': {'type': 'string', 'enum': ['queue', 'next', 'now'], 'default': 'queue'},
    'value': {'oneOf': [{'type': 'integer'}, {'type': 'string', 'enum': ['off', 'track', 'queue']}]},
    'track_id': {'type': 'string', 'maxLength': 32, 'description': 'Stable queued-track ID from queue/status, used by remove.'},
    'offset': {'type': 'integer', 'minimum': 0, 'maximum': 1000},
    'settings': SETTINGS_SCHEMA}, 'required': ['action']}


async def perform(service, message, action, **kwargs):
    try:
        return await service.execute(message, action, **kwargs)
    except MusicError as exc:
        return {'error': str(exc), 'succeeded': False}
    except Exception as exc:
        log.warning('Music operation failed action=%s type=%s', action, type(exc).__name__)
        return {'error': 'The music operation could not be confirmed. Check music status and try again.', 'succeeded': False}


class MusicControlTool(Tool):
    tool_name = 'music_control'
    returns_result = True
    is_destructive = True
    transports = ('discord',)
    timeout_seconds = 60

    def __init__(self, bot, service):
        super().__init__(bot)
        self.service = service

    def get_parameters(self):
        return {**PARAMETERS, 'properties': {**PARAMETERS['properties'],
                'action': {'type': 'string', 'enum': [a for a in ACTIONS if a not in READ_ACTIONS]}}}

    def get_description(self):
        return ('Control music in the requesting Discord server. play(query or queries) automatically joins their voice channel; '
                'mode=queue adds without interruption, next puts songs next, now replaces the current song only when asked. '
                'join/move optionally take channel name/ID. pause/resume/skip/shuffle; stop clears queue; leave disconnects; '
                'volume value=0..100, seek value=seconds, repeat value=off/track/queue, remove track_id from music_info queue, clear queue. '
                'configure reads/updates server music settings (server managers only). '
                'Never claim success unless the result confirms it; started means an actual TrackStart was received. '
                'If no voice channel is known, ask the user; do not hijack another session. '
                'Read music_info for live state and channels. No guild/user overrides or arbitrary audio URLs.')

    async def execute(self, message, action, **kwargs):
        if action in READ_ACTIONS:
            return json.dumps({'error': 'Use music_info for read operations.'})
        return json.dumps(await perform(self.service, message, action, **kwargs), ensure_ascii=False)


class MusicInfoTool(MusicControlTool):
    tool_name = 'music_info'
    is_destructive = False
    side_effects = False

    def get_parameters(self):
        return {'type': 'object', 'additionalProperties': False, 'properties': {
            'action': {'type': 'string', 'enum': sorted(READ_ACTIONS)},
            'query': PARAMETERS['properties']['query'], 'offset': PARAMETERS['properties']['offset']}, 'required': ['action']}

    def get_description(self):
        return ('Read on-demand music state for this Discord server: status shows current song, connected voice channel, volume, repeat and next 25 songs; '
                'queue(offset) paginates 25 songs with stable removal IDs; channels lists visible voice channels and requester voice channel; '
                'search(query) finds up to five YouTube songs without joining or playing. '
                'Use normal reasoning to choose tracks for moods/similar songs, then music_control play with the chosen title/URL. '
                'Live state is not present in the static system prompt. Never infer current playback from an earlier turn.')

    async def execute(self, message, action, **kwargs):
        if action not in READ_ACTIONS:
            return json.dumps({'error': 'Use music_control for changes.'})
        return json.dumps(await perform(self.service, message, action, **kwargs), ensure_ascii=False)
