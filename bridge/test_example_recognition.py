import asyncio
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid
import wave

import numpy as np
from annotations import AnnotationStore, annotation_server
from sound_examples import ExampleRecognizer, audio_feature
from server import Bridge


def call(frequency=580):
    t = np.arange(24000)/16000
    wave = np.sin(2*np.pi*(frequency*t+170*t*t)) * np.sin(np.pi*t/1.5)**2
    return (wave*10000).astype('<i2').tobytes()


class ExamplesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'archive.sqlite3'
        self.store = AnnotationStore(self.path)
        self.mid = str(uuid.uuid4()); self.pcm = call()
        self.store.add(self.mid, 1234, self.pcm)
        self.store.annotate(self.mid, '进食前', '饭盆旁')
        self.matcher = ExampleRecognizer(self.store)
        self.fallback = {'label': '喵叫', 'text': '模型原提示', 'score': .8}

    def tearDown(self): self.temp.cleanup()

    def test_similar_recording_uses_saved_label_and_preserves_model(self):
        quiet = (np.frombuffer(self.pcm,dtype='<i2')*.5).astype('<i2').tobytes()
        result = self.matcher.recognize(quiet, self.fallback)
        self.assertEqual(result['label'], '进食前')
        self.assertEqual(result['example_id'], self.mid)
        self.assertEqual(result['model_prediction'], self.fallback)
        self.assertGreater(result['similarity'], .99)
        with self.store.connect() as db:
            self.assertIsNotNone(db.execute('SELECT feature FROM recordings').fetchone()[0])

    def test_edits_delete_restore_and_restart_take_effect_without_retraining(self):
        self.assertEqual(self.matcher.recognize(self.pcm,self.fallback)['label'], '进食前')
        self.store.annotate(self.mid, '呼唤', '门边')
        self.assertEqual(self.matcher.recognize(self.pcm,self.fallback)['label'], '呼唤')
        self.store.set_deleted(self.mid, True)
        reopened = AnnotationStore(self.path)
        matcher = ExampleRecognizer(reopened)
        self.assertEqual(reopened.list()['total'], 0)
        self.assertEqual(reopened.list(deleted=True)['total'], 1)
        self.assertIs(matcher.recognize(self.pcm,self.fallback),self.fallback)
        with self.assertRaises(KeyError): reopened.annotate(self.mid,'修改已删除记录','')
        reopened.set_deleted(self.mid,False)
        self.assertEqual(matcher.recognize(self.pcm,self.fallback)['label'], '呼唤')
        reopened.annotate(self.mid,'','')
        self.assertIs(matcher.recognize(self.pcm,self.fallback),self.fallback)

    def test_conflicting_labels_fall_back_instead_of_guessing(self):
        other = str(uuid.uuid4());self.store.add(other,1235,self.pcm)
        self.store.annotate(other,'玩耍','')
        self.assertEqual(self.matcher.recognize(self.pcm,self.fallback)['label'],self.fallback['label'])

    def test_unrelated_noise_human_silence_and_self_are_not_matched(self):
        noise = np.random.default_rng(1).normal(0,5000,24000).astype('<i2').tobytes()
        self.assertEqual(self.matcher.recognize(noise,self.fallback)['label'],self.fallback['label'])
        self.assertIs(self.matcher.recognize(bytes(32000),self.fallback),self.fallback)
        self.assertIs(self.matcher.recognize(self.pcm,self.fallback,self.mid),self.fallback)
        human = {'label':'人声','text':'主要人声'}
        self.assertIs(self.matcher.recognize(self.pcm,human),human)

    def test_replayed_clip_with_different_boundaries_uses_time_alignment(self):
        original = np.frombuffer(self.pcm,dtype='<i2')
        # Same call recorded from its middle, with new leading/trailing silence and lower gain.
        replay = np.concatenate([np.zeros(8000),original[6000:18000]*.6,np.zeros(16000)]).astype('<i2').tobytes()
        result = self.matcher.recognize(replay,self.fallback)
        self.assertEqual(result['label'],'进食前')
        self.assertEqual(result['match_method'],'aligned')
        self.assertLess(result['match_diagnostics']['best_global'],.94)
        self.assertGreater(result['similarity'],.90)

    def test_delete_restore_http_and_unknown_id(self):
        server=annotation_server(self.store,0)
        worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
        base=f'http://127.0.0.1:{server.server_port}'
        def post(action,mid):
            return urlopen(Request(f'{base}/api/{action}/{mid}',data=b'{}',
                                  headers={'Content-Type':'application/json','Origin':base}))
        try:
            with post('delete',self.mid) as response:self.assertTrue(json.load(response)['deleted'])
            with urlopen(base+'/api/recordings') as response:self.assertEqual(json.load(response)['total'],0)
            with urlopen(base+'/api/recordings?deleted=1') as response:self.assertEqual(json.load(response)['total'],1)
            with post('restore',self.mid) as response:self.assertFalse(json.load(response)['deleted'])
            self.assertEqual(self.store.list()['items'][0]['label'],'进食前')
            with self.assertRaises(HTTPError) as exc:post('delete',str(uuid.uuid4()))
            self.assertEqual(exc.exception.code,404)
            with self.assertRaises(HTTPError) as exc:
                urlopen(Request(base+'/api/delete/'+self.mid,data=b'{}',headers={'Content-Type':'application/json','Origin':'https://example.com'}))
            self.assertEqual(exc.exception.code,403)
        finally:server.shutdown();server.server_close();worker.join()

    def test_old_database_migrates_preserving_samples_and_labels(self):
        old=Path(self.temp.name)/'old.sqlite3'
        with sqlite3.connect(old) as db:
            db.execute("CREATE TABLE recordings(id TEXT PRIMARY KEY,timestamp REAL NOT NULL,pcm BLOB NOT NULL,prediction TEXT,label TEXT NOT NULL DEFAULT '',context TEXT NOT NULL DEFAULT '',annotated_at REAL)")
            db.execute('INSERT INTO recordings VALUES(?,?,?,?,?,?,?)',(self.mid,1234,self.pcm,None,'进食前','饭盆旁',1235))
        store=AnnotationStore(old)
        self.assertEqual(store.list()['items'][0]['label'],'进食前')
        self.assertEqual(ExampleRecognizer(store).recognize(self.pcm,self.fallback)['label'],'进食前')


class BridgeExamplesTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_device_upload_publishes_example_label_and_archives_result(self):
        class Model:
            def describe_cat(self,pcm): return {'label':'喵叫','text':'模型提示'}
        with tempfile.TemporaryDirectory() as temp:
            store=AnnotationStore(Path(temp)/'test.sqlite3')
            pcm=call();reference=str(uuid.uuid4())
            store.add(reference,1,pcm);store.annotate(reference,'呼唤','门边')
            bridge=Bridge('test',Model(),store)
            queue=asyncio.Queue(maxsize=16);bridge.clients[object()]={'queue':queue}
            bridge.voice_pcm.extend(pcm);bridge.finish_voice()
            await asyncio.gather(*bridge.translation_tasks,*bridge.archive_tasks)
            events=[json.loads(queue.get_nowait()) for _ in range(queue.qsize())]
            translation=events[-1]
            self.assertEqual(translation['type'],'translation')
            self.assertEqual('猫叫识别：呼唤',translation['text'])
            self.assertEqual(translation['result']['example_id'],reference)
            self.assertEqual(store.list()['items'][0]['prediction']['label'],'呼唤')

if __name__=='__main__':unittest.main()
