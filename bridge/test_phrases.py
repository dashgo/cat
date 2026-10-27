import asyncio
import base64
import json
import unittest
import uuid

from local_ai import LocalTranslator, QUICK_PHRASES, pcm_float
from server import Bridge, PLAY
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve


class PhraseSynthesisTests(unittest.TestCase):
    def test_all_eight_presets_generate_bounded_audio_and_correct_intent(self):
        translator = LocalTranslator()
        self.assertEqual(len(QUICK_PHRASES), 8)
        for key,(text,intent) in QUICK_PHRASES.items():
            pcm,result = translator.phrase_to_cat(key)
            audio = pcm_float(pcm)
            self.assertEqual((result['text'],result['intent']),(text,intent))
            self.assertGreater(len(audio),1600)
            self.assertLess(len(audio),16000*6)
            self.assertLessEqual(float(abs(audio).max()),.551)
        with self.assertRaises(ValueError): translator.phrase_to_cat('unknown')


class PhraseProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_generate_once_then_repeat_same_audio_twice_without_asr(self):
        class Generator:
            ready=True
            error=''
            def __init__(self): self.calls=0
            def phrase_to_cat(self,preset):
                self.calls+=1
                return bytes([42,0])*1600, {'text':'一起玩耍','label':'玩耍邀请','note':'合成互动音'}
            def human_to_cat(self,*_): raise AssertionError('常用词不应进行语音识别')
        generator=Generator();bridge=Bridge('test',generator);writes=[]
        bridge.write=lambda k,p:writes.append((k,p))
        async with serve(bridge.handler,'127.0.0.1',0) as server:
            async with connect(f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}') as ws:
                await ws.recv();mid=str(uuid.uuid4())
                await ws.send(json.dumps({'type':'phrase_send','id':mid,'phrase_id':'play'}))
                events=[json.loads(await asyncio.wait_for(ws.recv(),3)) for _ in range(4)]
                self.assertEqual([e['state'] for e in events],['generating','ready','playing','played'])
                self.assertTrue(all(e['id']==mid for e in events))
                self.assertIn('一起玩耍',events[1]['text'])
                pcm=base64.b64decode(events[1]['cat_pcm'])
                self.assertEqual(b''.join(p for k,p in writes if k==PLAY),pcm)
                for _ in range(2):
                    writes.clear();mid=str(uuid.uuid4())
                    await ws.send(json.dumps({'type':'cat_send','id':mid,'pcm':base64.b64encode(pcm).decode()}))
                    events=[json.loads(await asyncio.wait_for(ws.recv(),3)) for _ in range(2)]
                    self.assertEqual([e['state'] for e in events],['playing','played'])
                    self.assertEqual(b''.join(p for k,p in writes if k==PLAY),pcm)
                self.assertEqual(generator.calls,1)

    async def test_invalid_preset_does_not_play_and_releases_owner(self):
        translator=LocalTranslator();translator.ready=True
        bridge=Bridge('test',translator);writes=[]
        bridge.write=lambda k,p:writes.append((k,p))
        async with serve(bridge.handler,'127.0.0.1',0) as server:
            async with connect(f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}') as ws:
                await ws.recv()
                await ws.send(json.dumps({'type':'phrase_send','id':str(uuid.uuid4()),'phrase_id':'invalid'}))
                events=[json.loads(await asyncio.wait_for(ws.recv(),3)) for _ in range(3)]
                self.assertEqual([e['state'] for e in events],['generating','failed','failed'])
                self.assertFalse(any(k==PLAY for k,p in writes))
                # Stop/mute finishes before the next playback can claim ownership.
                await asyncio.sleep(.02)
                self.assertIsNone(bridge.owner)

    async def test_busy_or_disabled_synthesis_reports_delivery_failure(self):
        bridge=Bridge('test')
        async with serve(bridge.handler,'127.0.0.1',0) as server:
            async with connect(f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}') as ws:
                await ws.recv()
                for busy in (True,False):
                    bridge.recording=busy
                    await ws.send(json.dumps({'type':'phrase_send','id':str(uuid.uuid4()),'phrase_id':'play'}))
                    self.assertEqual(json.loads(await ws.recv())['state'],'failed')
                    self.assertEqual(json.loads(await ws.recv())['type'],'error')
                self.assertIsNone(bridge.owner)

if __name__=='__main__':unittest.main()
