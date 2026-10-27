"""Run real local models against WAV/AIFF fixtures, without playing hardware audio.

Usage: .venv/bin/python scripts/smoke-local-ai.py CAT.wav HUMAN.aiff
"""
import json
from pathlib import Path
import sys
import time
import wave
import av
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
from local_ai import LocalTranslator


def read_pcm(path):
    resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)
    chunks = []
    with av.open(path) as container:
        for frame in container.decode(audio=0):
            chunks.extend(f.to_ndarray().astype('<i2').tobytes() for f in resampler.resample(frame))
        chunks.extend(f.to_ndarray().astype('<i2').tobytes() for f in resampler.resample(None))
    return b''.join(chunks)


translator = LocalTranslator()
start = time.monotonic(); translator.load()
result = {'load_ms': round((time.monotonic() - start)*1000)}
result['cat'] = translator.describe_cat(read_pcm(sys.argv[1]))
assert result['cat']['label'] in ('猫叫', '喵叫', '呼噜', '嘶叫', '长嚎'), result
output, result['human'] = translator.human_to_cat(read_pcm(sys.argv[2]))
assert result['human']['text'], result
assert result['human']['intent'] == 'food', result
path = Path(__file__).resolve().parents[1] / 'artifacts/ai-generated-cat.wav'
with wave.open(str(path), 'wb') as wav:
    wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000); wav.writeframes(output)
result['generated_seconds'] = len(output) / 32000
print(json.dumps(result, ensure_ascii=False, indent=2))
