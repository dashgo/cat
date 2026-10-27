import asyncio
import base64
import json
import unittest
from unittest.mock import patch
from server import Bridge, BUTTON, MIC, PLAY, TALK, MAX_VOICE_BYTES


class RecordingTests(unittest.TestCase):
    def setUp(self):
        self.bridge = Bridge('test')
        self.queue = asyncio.Queue(maxsize=16)
        self.bridge.clients[object()] = {'queue': self.queue}

    def events(self):
        events = []
        while not self.queue.empty():
            events.append(json.loads(self.queue.get_nowait()))
        return events

    def test_button_release_creates_exactly_one_complete_voice(self):
        b = self.bridge
        pcm = bytes(range(256)) * 2 + bytes(128)
        b.receive(MIC, 0, pcm)  # no background recording
        b.receive(BUTTON, 1, b'\x01')
        for i in range(10):
            b.receive(MIC, i + 2, pcm)
        b.receive(BUTTON, 12, b'\x00')
        b.receive(BUTTON, 13, b'\x00')
        events = self.events()
        self.assertEqual([e['type'] for e in events], ['device_recording', 'device_recording', 'voice'])
        self.assertEqual(base64.b64decode(events[-1]['pcm']), pcm * 10)
        self.assertFalse(b.recording)

    def test_short_press_and_unplug_do_not_publish_partial_message(self):
        b = self.bridge
        b.receive(BUTTON, 0, b'\x01')
        b.receive(MIC, 1, bytes(640))
        b.receive(BUTTON, 2, b'\x00')
        b.receive(BUTTON, 3, b'\x01')
        b.abort_recording()
        self.assertFalse(any(e['type'] == 'voice' for e in self.events()))
        self.assertFalse(b.recording)

    def test_sixty_second_limit(self):
        b = self.bridge
        b.receive(BUTTON, 0, b'\x01')
        for i in range(MAX_VOICE_BYTES // 640 + 100):
            b.receive(MIC, i, bytes(640))
        voices = [e for e in self.events() if e['type'] == 'voice']
        self.assertEqual(len(voices), 1)
        self.assertEqual(len(base64.b64decode(voices[0]['pcm'])), MAX_VOICE_BYTES)


class PlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_paced_playback_ack_and_mute(self):
        class Socket:
            def __init__(self): self.messages = []
            async def send(self, message): self.messages.append(json.loads(message))
        b = Bridge('test'); ws = Socket(); b.owner = ws
        writes = []
        b.write = lambda kind, payload: writes.append((kind, payload))
        async def no_sleep(_): pass
        with patch('server.asyncio.sleep', no_sleep):
            await b.play_voice(ws, bytes(3300), 'id')
        self.assertEqual(writes[0], (TALK, b'\x01'))
        self.assertEqual(writes[-1], (TALK, b'\x00'))
        self.assertEqual(len([x for x in writes if x[0] == PLAY]), 6)
        self.assertTrue(all(len(p) == 640 for k, p in writes if k == PLAY))
        self.assertEqual([m['state'] for m in ws.messages], ['playing', 'played'])
        self.assertIsNone(b.owner)
