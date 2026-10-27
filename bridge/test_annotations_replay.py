import asyncio
import base64
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import uuid
import wave

from annotations import AnnotationStore, annotation_server
from server import Bridge, PLAY, TALK
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve


class AnnotationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'annotations.sqlite3'
        self.store = AnnotationStore(self.path)
        self.mid = str(uuid.uuid4())
        self.pcm = bytes([17, 0]) * 1600
        self.store.add(self.mid, 1234, self.pcm)

    def tearDown(self): self.temp.cleanup()

    def test_annotations_survive_restart_and_late_prediction(self):
        self.store.annotate(self.mid, '进食前', '在食盆边')
        self.store.prediction(self.mid, {'text': '模型提示'})
        self.store.add(self.mid, 9000, bytes(3200))
        restored = AnnotationStore(self.path)
        row = restored.list()['items'][0]
        self.assertEqual((row['label'], row['context'], row['prediction']['text']),
                         ('进食前', '在食盆边', '模型提示'))
        self.assertEqual(row['timestamp'], 1234)
        self.assertEqual(restored.list(pending=True)['total'], 0)
        with wave.open(io.BytesIO(restored.audio(self.mid))) as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth()), (16000, 1, 2))
            self.assertEqual(wav.readframes(wav.getnframes()), self.pcm)
        restored.annotate(self.mid, '', '暂不确定')
        self.assertEqual(restored.list(pending=True)['total'], 1)

    def test_pagination_and_validation(self):
        for i in range(26): self.store.add(str(uuid.uuid4()), i, self.pcm)
        self.assertEqual(len(self.store.list()['items']), 25)
        self.assertEqual(len(self.store.list(page=1)['items']), 2)
        with self.assertRaises(ValueError): self.store.annotate(self.mid, 'a'*101, '')
        with self.assertRaises(ValueError): self.store.annotate(self.mid, [], '')
        with self.assertRaises(KeyError): self.store.annotate(str(uuid.uuid4()), '喵叫', '')

    def test_http_playback_save_filter_and_origin_checks(self):
        server = annotation_server(self.store, 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        url = f'http://127.0.0.1:{server.server_port}'
        try:
            with urlopen(url + '/') as res: self.assertIn('猫叫录音标注', res.read().decode())
            body = json.dumps({'label': '呼唤', 'context': '门边'}).encode()
            request = Request(url + '/api/annotations/' + self.mid, data=body,
                              headers={'Content-Type': 'application/json', 'Origin': url})
            with urlopen(request) as res: self.assertEqual(json.load(res)['label'], '呼唤')
            with urlopen(url+'/api/recordings?pending=1') as res: self.assertEqual(json.load(res)['total'], 0)
            with urlopen(url+'/audio/'+self.mid+'.wav') as res: self.assertEqual(res.read()[:4], b'RIFF')
            for headers in ({'Content-Type': 'application/json', 'Origin': 'https://example.com'},
                            {'Content-Type': 'text/plain'}):
                with self.assertRaises(HTTPError) as error:
                    urlopen(Request(url+'/api/annotations/'+self.mid, data=body, headers=headers))
                self.assertEqual(error.exception.code, 403)
        finally:
            server.shutdown(); server.server_close(); worker.join()


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_completion_cleanup_does_not_mute_next_send(self):
        bridge = Bridge('test'); writes = []
        bridge.write = lambda kind, payload: writes.append((kind, payload))
        class Socket:
            async def send(self, message):
                if json.loads(message).get('state') == 'played':
                    # A new request can arrive immediately after the completion ACK.
                    bridge.owner = self
        ws = Socket(); bridge.owner = ws
        await bridge.play_voice(ws, bytes(3200), 'first')
        self.assertIs(bridge.owner, ws)
        self.assertEqual(sum(k == TALK and p == b'\x00' for k, p in writes), 1)

    async def test_same_generated_pcm_sent_three_times_without_model(self):
        class NoTranscription:
            ready = True
            error = ''
            def human_to_cat(self, *_): raise AssertionError('重复发送不可再次调用模型')
        bridge = Bridge('test', NoTranscription()); writes = []
        bridge.write = lambda k, p: writes.append((k, p))
        pcm = bytes([42, 0]) * 1600
        async with serve(bridge.handler, '127.0.0.1', 0) as server:
            async with connect(f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}') as ws:
                await ws.recv()
                for _ in range(3):
                    mid = str(uuid.uuid4()); writes.clear()
                    await ws.send(json.dumps({'type': 'cat_send', 'id': mid,
                                              'pcm': base64.b64encode(pcm).decode()}))
                    events = [json.loads(await asyncio.wait_for(ws.recv(), 3)) for _ in range(2)]
                    self.assertEqual([e['state'] for e in events], ['playing', 'played'])
                    self.assertTrue(all(e['id'] == mid for e in events))
                    self.assertEqual(b''.join(p for k, p in writes if k == PLAY), pcm)
                    self.assertEqual(writes[-1], (TALK, b'\x00'))
                    self.assertIsNone(bridge.owner)

    async def test_replay_busy_and_invalid_audio_report_failure(self):
        bridge = Bridge('test'); writes = []
        bridge.write = lambda k, p: writes.append((k, p))
        async with serve(bridge.handler, '127.0.0.1', 0) as server:
            async with connect(f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}') as ws:
                await ws.recv()
                bridge.recording = True
                await ws.send(json.dumps({'type':'cat_send','id':'busy','pcm':base64.b64encode(bytes(3200)).decode()}))
                self.assertEqual(json.loads(await ws.recv())['state'], 'failed')
                self.assertIn('忙', json.loads(await ws.recv())['message'])
                bridge.recording = False
                for pcm in ('xxx', 123, base64.b64encode(bytes(3)).decode()):
                    await ws.send(json.dumps({'type':'cat_send','id':'invalid','pcm':pcm}))
                    self.assertEqual(json.loads(await ws.recv())['state'], 'failed')
                    self.assertEqual(json.loads(await ws.recv())['type'], 'error')
                self.assertFalse(any(k == PLAY for k, _ in writes))

    async def test_archive_even_without_ai_and_without_app_client(self):
        with tempfile.TemporaryDirectory() as temp:
            store = AnnotationStore(Path(temp)/'test.sqlite3')
            bridge = Bridge('test', annotations=store)
            bridge.voice_pcm.extend(bytes(3200)); bridge.finish_voice()
            await asyncio.gather(*bridge.archive_tasks)
            self.assertEqual(store.list()['total'], 1)

if __name__ == '__main__': unittest.main()
