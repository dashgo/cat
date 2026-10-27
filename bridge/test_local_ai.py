"""AI protocol regression tests; no board or downloaded models required."""
import asyncio
import base64
import json
import unittest
import uuid
from unittest.mock import patch

import numpy as np
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

from local_ai import PROFILES, pcm_float, synthesize_meow, text_intent
from server import Bridge, PLAY


class AudioTests(unittest.TestCase):
    def test_all_profiles_generate_bounded_mono_pcm(self):
        for profile in PROFILES:
            audio = pcm_float(synthesize_meow(profile))
            self.assertGreater(len(audio), 1600)
            self.assertLess(len(audio), 16000 * 5)
            self.assertLessEqual(float(np.max(np.abs(audio))), .551)
            self.assertTrue(np.isfinite(audio).all())
            self.assertGreater(float(np.max(np.abs(audio))), .1)

    def test_negation_and_common_intents(self):
        for text, intent in [('不要吃', 'boundary'), ('过来吃饭', 'food'), ('陪我玩', 'play'),
                             ('别怕', 'comfort'), ('你好', 'greeting'), ('过来', 'call')]:
            self.assertEqual(text_intent(text), intent)

    def test_invalid_pcm_rejected(self):
        for pcm in (b'', b'a', bytes(16000 * 2 * 60 + 2)):
            with self.assertRaises(ValueError): pcm_float(pcm)


class Translator:
    ready = True
    error = ''
    def describe_cat(self, pcm): return {'text': '检测到喵叫', 'label': '喵叫'}
    def human_to_cat(self, pcm): return bytes([2, 0]) * 1600, {'text': '你好', 'label': '打招呼', 'note': '合成互动音'}


class ProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_device_original_then_matching_translation(self):
        bridge = Bridge('test', Translator())
        queue = asyncio.Queue(maxsize=16)
        bridge.clients[object()] = {'queue': queue}
        bridge.voice_pcm.extend(bytes(3200))
        bridge.finish_voice()
        await asyncio.gather(*bridge.translation_tasks)
        events = [json.loads(queue.get_nowait()) for _ in range(queue.qsize())]
        voice, result = events[-2:]
        self.assertEqual(voice['type'], 'voice')
        self.assertEqual(voice['translation_state'], 'pending')
        self.assertEqual(base64.b64decode(voice['pcm']), bytes(3200))
        self.assertEqual(result['id'], voice['id'])
        self.assertEqual(result['text'], '检测到喵叫')

    async def test_bounded_device_backlog(self):
        bridge = Bridge('test', Translator())
        queue = asyncio.Queue(maxsize=32)
        bridge.clients[object()] = {'queue': queue}
        for _ in range(5):
            bridge.voice_pcm.extend(bytes(3200)); bridge.finish_voice()
        self.assertEqual(len(bridge.translation_tasks), 4)
        await asyncio.gather(*bridge.translation_tasks)
        events = [json.loads(queue.get_nowait()) for _ in range(queue.qsize())]
        self.assertEqual(sum(e.get('state') == 'failed' for e in events), 1)

    async def test_websocket_human_generates_cat_and_plays_generated_bytes_only(self):
        bridge = Bridge('test', Translator()); writes = []
        bridge.write = lambda kind, payload: writes.append((kind, payload))
        async with serve(bridge.handler, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(f'ws://127.0.0.1:{port}') as ws:
                self.assertTrue(json.loads(await ws.recv())['ai_ready'])
                mid = str(uuid.uuid4())
                await ws.send(json.dumps({'type': 'voice_send', 'id': mid,
                                          'pcm': base64.b64encode(bytes([1, 0])*1600).decode()}))
                events = []
                while not events or events[-1].get('state') != 'played':
                    events.append(json.loads(await asyncio.wait_for(ws.recv(), 3)))
                self.assertEqual([e.get('state') for e in events], ['translating', 'ready', 'playing', 'played'])
                self.assertTrue(all(e['id'] == mid for e in events))
                generated = base64.b64decode(events[1]['cat_pcm'])
                self.assertEqual(b''.join(p for k, p in writes if k == PLAY), generated)
                self.assertIsNone(bridge.owner)

    async def test_failed_asr_never_plays_original_and_releases_owner(self):
        class Failed(Translator):
            def human_to_cat(self, pcm): raise ValueError('未识别到清晰人声')
        bridge = Bridge('test', Failed()); writes = []
        bridge.write = lambda kind, payload: writes.append((kind, payload))
        class Socket:
            def __init__(self): self.events = []
            async def send(self, message): self.events.append(json.loads(message))
        ws = Socket(); bridge.owner = ws
        await bridge.translate_and_play(ws, bytes(3200), 'id')
        self.assertFalse(any(k == PLAY for k, _ in writes))
        self.assertIsNone(bridge.owner)
        self.assertEqual(ws.events[-1]['state'], 'failed')

    async def test_legacy_binary_cannot_bypass_translation(self):
        bridge = Bridge('test', Translator()); writes = []
        bridge.write = lambda *args: writes.append(args)
        async with serve(bridge.handler, '127.0.0.1', 0) as server:
            port = server.sockets[0].getsockname()[1]
            async with connect(f'ws://127.0.0.1:{port}') as ws:
                await ws.recv(); bridge.owner = ws
                await ws.send(bytes(640))
                self.assertEqual(json.loads(await ws.recv())['type'], 'error')
                self.assertFalse(any(k == PLAY for k, _ in writes))

if __name__ == '__main__': unittest.main()
