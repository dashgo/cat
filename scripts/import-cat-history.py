"""Import existing App device WAVs into the local annotation archive, once per UUID."""
import argparse
import json
from pathlib import Path
import sys
import wave
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
from annotations import AnnotationStore

parser = argparse.ArgumentParser()
parser.add_argument('history_dir', type=Path, help='App Documents/VoiceChat 目录')
parser.add_argument('--database', type=Path, default=Path(__file__).resolve().parents[1] / 'data/cat-annotations.sqlite3')
args = parser.parse_args()
store = AnnotationStore(args.database)
count = 0
for item in json.loads((args.history_dir / 'history.json').read_text()):
    if item['source'] != 'device': continue
    import uuid
    uuid.UUID(item['id'])
    path = args.history_dir / (item['id'] + '.wav')
    if not path.is_file(): continue
    with wave.open(str(path), 'rb') as audio:
        if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (16000, 1, 2):
            raise ValueError(f'录音格式不正确：{path.name}')
        store.add(item['id'], item['timestamp'], audio.readframes(audio.getnframes()))
    count += 1
print(f'已归档 {count} 条设备录音；相同 UUID 不重复插入，已有人工标注保持不变。')
