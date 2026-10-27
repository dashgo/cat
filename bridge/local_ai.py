"""Offline sound classification, Mandarin ASR, and deliberately synthetic meows.

YAMNet identifies sound events, not feline semantics. Descriptions below are
product heuristics, never a translation model trained on paired cat/human speech.
"""
from pathlib import Path
import csv
import math
import threading
import time
import wave

import numpy as np

RATE = 16000
ROOT = Path(__file__).resolve().parents[1]
CAT_LABELS = {
    76: ('猫叫', '检测到猫的声音，具体意图暂不能判断。'),
    77: ('呼噜', '检测到呼噜声。可能在互动或自我安抚，需结合行为判断。'),
    78: ('喵叫', '检测到喵叫。可能在呼唤或寻求关注，需结合现场情境判断。'),
    79: ('嘶叫', '检测到嘶叫。可能希望保持距离，需结合行为判断。'),
    80: ('长嚎', '检测到较长的猫嚎声。可能在呼唤或表达紧张，具体原因不能仅凭声音判断。'),
}
PROFILES = {
    'greeting': ('打招呼', [(0.55, 510, 730, 480)]),
    'call': ('呼唤', [(0.65, 480, 760, 430), (0.55, 520, 790, 470)]),
    'food': ('吃饭邀请', [(0.40, 560, 830, 520), (0.45, 590, 880, 500)]),
    'comfort': ('安抚', [(0.85, 380, 570, 340)]),
    'play': ('玩耍邀请', [(0.30, 620, 950, 650), (0.35, 650, 1000, 580), (0.40, 580, 900, 500)]),
    'boundary': ('轻声提醒', [(0.65, 430, 620, 380)]),
    'generic': ('普通互动', [(0.65, 500, 780, 440)]),
}
QUICK_PHRASES = {
    'hello': ('你好', 'greeting'),
    'come': ('过来', 'call'),
    'food': ('吃饭啦', 'food'),
    'play': ('一起玩耍', 'play'),
    'comfort': ('别怕', 'comfort'),
    'love': ('我爱你', 'comfort'),
    'sleep': ('晚安', 'comfort'),
    'stop': ('不要咬', 'boundary'),
}


def pcm_float(pcm):
    if not pcm or len(pcm) % 2 or len(pcm) > RATE * 2 * 60:
        raise ValueError('PCM 长度不正确')
    return np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768


def text_intent(text):
    # Negation takes precedence: “不要吃” should not become an invitation to eat.
    groups = [
        ('boundary', ('不要', '不许', '别叫', '别咬', '停止', 'stop', "don't")),
        ('food', ('吃', '饭', '粮', '饿', 'food', 'eat')),
        ('comfort', ('别怕', '不怕', '乖', '安静', '睡', '休息', '没事', '爱你', 'relax')),
        ('play', ('玩', '球', '逗', 'play')),
        ('call', ('过来', '回来', '来这', '看我', 'come')),
        ('greeting', ('你好', '早上', '晚上', '哈喽', 'hello', 'hi')),
    ]
    for intent, words in groups:
        if any(word in text.lower() for word in words):
            return intent
    return 'generic'


def synthesize_meow(intent, sample_path=None):
    """Harmonic source, moving formants, vibrato, and a smooth amplitude envelope.

    This is a sound-design output, not a learned or proven cat communication code.
    Peak <= 0.55; no harsh hissing or aggressive screeches are synthesized.
    """
    _, calls = PROFILES.get(intent, PROFILES['generic'])
    template_path = Path(sample_path) if sample_path else ROOT / 'models/yamnet/miaow_16k.wav'
    if template_path.is_file():
        with wave.open(str(template_path), 'rb') as audio:
            if (audio.getframerate(), audio.getnchannels(), audio.getsampwidth()) != (RATE, 1, 2):
                raise ValueError('猫叫模板音频格式不正确')
            template = np.frombuffer(audio.readframes(audio.getnframes()), dtype='<i2').astype(np.float32) / 32768
        # First isolated call from Google's YAMNet tutorial recording, 0.70–2.05s.
        template = template[int(.70 * RATE):int(2.05 * RATE)]
        parts = [np.zeros(int(.10 * RATE))]
        for _, start, _, _ in calls:
            speed = np.clip(start / 500, .85, 1.15)
            positions = np.arange(0, len(template) - 1, speed)
            call = np.interp(positions, np.arange(len(template)), template)
            fade = min(int(.035 * RATE), len(call) // 2)
            call[:fade] *= np.linspace(0, 1, fade)
            call[-fade:] *= np.linspace(1, 0, fade)
            call *= .55 / max(1e-6, float(np.max(np.abs(call))))
            parts.extend([call, np.zeros(int(.22 * RATE))])
        return (np.concatenate(parts) * 32767).astype('<i2').tobytes()
    parts = [np.zeros(int(.10 * RATE), dtype=np.float32)]
    for duration, start, crest, end in calls:
        t = np.arange(round(duration * RATE), dtype=np.float64) / RATE
        u = t / duration
        pitch = np.interp(u, [0, .3, 1], [start, crest, end])
        pitch *= 1 + .018 * np.sin(2 * math.pi * 18 * t)
        phase = 2 * math.pi * np.cumsum(pitch) / RATE
        signal = np.zeros_like(t)
        f1 = np.interp(u, [0, .4, 1], [1050, 1550, 650])
        f2 = np.interp(u, [0, .5, 1], [2400, 2700, 1650])
        for harmonic in range(1, 13):
            frequency = harmonic * pitch
            weight = (.20 + np.exp(-.5 * ((frequency - f1) / 400)**2)
                      + .6 * np.exp(-.5 * ((frequency - f2) / 650)**2)) / harmonic
            signal += weight * np.sin(harmonic * phase)
        envelope = np.minimum(1, u / .10) * np.minimum(1, (1 - u) / .22)
        signal *= np.maximum(0, envelope) * np.sin(math.pi * u)**.35
        signal /= max(1e-6, float(np.max(np.abs(signal))))
        parts += [signal * .55, np.zeros(int(.18 * RATE))]
    result = np.concatenate(parts)
    return np.clip(result * 32767, -32768, 32767).astype('<i2').tobytes()


class LocalTranslator:
    def __init__(self, model_root=None):
        self.root = Path(model_root) if model_root else ROOT / 'models'
        self.lock = threading.Lock()
        self.yamnet = None
        self.whisper = None
        self.ready = False
        self.error = ''

    def phrase_to_cat(self, phrase_id):
        if phrase_id not in QUICK_PHRASES:
            raise ValueError('不支持的常用互动词语')
        text, intent = QUICK_PHRASES[phrase_id]
        output = synthesize_meow(intent, self.root / 'yamnet/miaow_16k.wav')
        return output, {'text': text, 'intent': intent, 'label': PROFILES[intent][0],
                        'note': '合成互动音，不代表猫能理解原句含义'}

    def load(self):
        with self.lock:
            if self.ready:
                return
            try:
                from ai_edge_litert.interpreter import Interpreter
                from faster_whisper import WhisperModel
                self.yamnet = Interpreter(model_path=str(self.root / 'yamnet/yamnet.tflite'), num_threads=2)
                self.yamnet.allocate_tensors()
                self.labels = [row['display_name'] for row in csv.DictReader(
                    (self.root / 'yamnet/class_map.csv').open())]
                if len(self.labels) != 521:
                    raise ValueError('YAMNet 类别表不匹配')
                self.whisper = WhisperModel(str(self.root / 'whisper-tiny'), device='cpu',
                                           compute_type='int8', cpu_threads=4, num_workers=1,
                                           local_files_only=True)
                self.ready = True; self.error = ''
            except Exception as exc:
                self.error = str(exc)
                raise RuntimeError('本地模型未就绪，请运行 scripts/setup-local-ai.sh') from exc

    def describe_cat(self, pcm):
        start = time.monotonic()
        audio = pcm_float(pcm)
        with self.lock:
            if not self.ready:
                raise RuntimeError('本地模型尚未就绪')
            rms = float(np.sqrt(np.mean(audio**2)))
            if rms < .003:
                return {'text': '声音太轻或没有明显叫声，请靠近麦克风重试。', 'label': '声音过轻',
                        'method': 'YAMNet · 声音类型识别，非语义翻译', 'score': None}
            if len(audio) < RATE * .4:
                return {'text': '录音过短，暂不能判断声音类型。', 'label': '录音过短',
                        'method': 'YAMNet · 声音类型识别，非语义翻译', 'score': None}
            # Moderate gain handles the quiet INMP441 signal without amplifying silence.
            audio = np.clip(audio * min(4, .12 / max(rms, 1e-5)), -.98, .98)
            input_index = self.yamnet.get_input_details()[0]['index']
            output_index = self.yamnet.get_output_details()[0]['index']
            scores = []
            for offset in range(0, max(1, len(audio) - 7800), 7800):
                window = audio[offset:offset + 15600]
                window = np.pad(window, (0, max(0, 15600 - len(window)))).astype(np.float32)
                self.yamnet.set_tensor(input_index, window)
                self.yamnet.invoke()
                scores.append(self.yamnet.get_tensor(output_index)[0].copy())
            values = np.stack(scores).max(axis=0)
            cat_id = max(CAT_LABELS, key=lambda index: values[index])
            subtype = max(range(77, 81), key=lambda index: values[index])
            if values[subtype] >= .18 and values[subtype] >= values[76] * .5:
                cat_id = subtype
            score = float(values[cat_id])
            speech = float(values[0])
            top = [{'label': self.labels[int(i)], 'score': round(float(values[i]), 4)}
                   for i in np.argsort(values)[-3:][::-1]]
            if score >= .18 and not (speech > .35 and speech > score * 1.25):
                label, text = CAT_LABELS[cat_id]
            elif speech >= .35:
                label, text = '人声', '主要检测到人声，暂未识别到明确猫叫。'
            else:
                label, text = '未确定', '未识别到明确猫叫，暂不推测含义。可在安静环境重新录音。'
            return {'text': text, 'label': label, 'score': round(score, 4), 'top_classes': top,
                    'method': 'YAMNet · 声音类型识别，非语义翻译',
                    'elapsed_ms': round((time.monotonic() - start) * 1000)}

    def human_to_cat(self, pcm):
        start = time.monotonic()
        audio = pcm_float(pcm)
        with self.lock:
            if not self.ready:
                raise RuntimeError('本地模型尚未就绪')
            rms = float(np.sqrt(np.mean(audio**2)))
            if rms < .003:
                raise ValueError('录音声音太轻，未生成猫叫，请靠近麦克风重试')
            segments, _ = self.whisper.transcribe(
                audio, language='zh', beam_size=3, vad_filter=True,
                condition_on_previous_text=False, temperature=0,
                initial_prompt='你好，过来吃饭，陪我玩，不要咬，乖，别怕。')
            accepted = [s.text.strip() for s in segments if s.no_speech_prob < .65 and s.avg_logprob > -1.0]
            text = ''.join(accepted).strip()
            if not text:
                raise ValueError('未识别到清晰人声，请重新录音')
            intent = text_intent(text)
            output = synthesize_meow(intent, self.root / 'yamnet/miaow_16k.wav')
            return output, {'text': text, 'label': PROFILES[intent][0], 'intent': intent,
                            'method': 'Whisper Tiny 本地转写 + 规则意图映射 + 猫叫合成',
                            'note': '合成互动音，不代表猫能理解原句含义',
                            'elapsed_ms': round((time.monotonic() - start) * 1000)}
