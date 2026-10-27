#!/usr/bin/env python3
"""Loopback-only simulator audio bridge. PCM: 16kHz, signed LE16, mono."""
import argparse
import base64
import uuid
import asyncio
import json
import math
import signal
import struct
import threading
import time
import zlib
from pathlib import Path
from collections import Counter

import serial
from serial.tools import list_ports
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

HEADER = struct.Struct('<4sBBHH')
MAGIC = b'CAT1'
PCM_BYTES = 640
MIC, PLAY, TALK, STATUS, BUTTON = 1, 2, 3, 4, 5
MAX_VOICE_BYTES = 16000 * 2 * 60


def packet(kind, payload, sequence=0):
    header = HEADER.pack(MAGIC, kind, 1, sequence & 0xffff, len(payload))
    body = header + payload
    return body + struct.pack('<I', zlib.crc32(body))


class Parser:
    def __init__(self):
        self.buffer = bytearray()
        self.bad_crc = 0

    def feed(self, data):
        self.buffer.extend(data)
        while len(self.buffer) >= HEADER.size:
            if self.buffer[:4] != MAGIC:
                del self.buffer[0]
                continue
            _, kind, version, seq, length = HEADER.unpack_from(self.buffer)
            if version != 1 or length > PCM_BYTES:
                del self.buffer[0]
                continue
            total = HEADER.size + length + 4
            if len(self.buffer) < total:
                break
            body = bytes(self.buffer[:total - 4])
            crc, = struct.unpack_from('<I', self.buffer, total - 4)
            if zlib.crc32(body) != crc:
                self.bad_crc += 1
                del self.buffer[0]
                continue
            payload = bytes(self.buffer[HEADER.size:total - 4])
            del self.buffer[:total]
            yield kind, seq, payload


class Bridge:
    def __init__(self, port, translator=None, annotations=None):
        self.port = port
        self.device = None
        self.serial_lock = threading.Lock()
        self.clients = {}
        self.owner = None
        self.sequence = 0
        self.board = {}
        self.metrics = Counter()
        self.last_packet = 0
        self.last_error = ''
        self.running = True
        self.parser = Parser()
        self.loop = None
        self.recording = False
        self.voice_pcm = bytearray()
        self.playback_task = None
        self.translator = translator
        self.translation_tasks = set()
        self.annotations = annotations
        self.archive_tasks = set()
        self.example_recognizer = None
        if annotations is not None and translator is not None:
            from sound_examples import ExampleRecognizer
            self.example_recognizer = ExampleRecognizer(annotations)

    def write(self, kind, payload):
        with self.serial_lock:
            if not self.device or not self.device.is_open:
                raise RuntimeError('开发板串口未连接')
            data = packet(kind, payload, self.sequence)
            self.sequence += 1
            if self.device.write(data) != len(data):
                raise RuntimeError('串口写入不完整')

    def reader(self):
        while self.running:
            try:
                selected = self.port
                if selected == 'auto':
                    matches = [p.device for p in list_ports.comports() if
                               'CAT Audio Bridge' in (p.product or '') or
                               (p.vid == 0x2fe3 and p.pid == 0x0010)]
                    if not matches:
                        raise serial.SerialException('等待 CAT Audio Bridge：请连接 nRF USB (J3)')
                    selected = matches[0]
                with serial.Serial(selected, 1000000, timeout=.03, write_timeout=.3) as device:
                    self.device = device
                    self.last_error = ''
                    self.parser = Parser()
                    while self.running:
                        data = device.read(device.in_waiting or 1)
                        for kind, seq, payload in self.parser.feed(data):
                            self.loop.call_soon_threadsafe(self.receive, kind, seq, payload)
                    self.device = None
            except (serial.SerialException, OSError) as exc:
                self.device = None
                self.last_error = str(exc)
                self.loop.call_soon_threadsafe(self.abort_recording)
                time.sleep(1)

    def abort_recording(self):
        if self.recording:
            self.recording = False
            self.voice_pcm.clear()
            self.broadcast_event({'type': 'device_recording', 'enabled': False})
            self.broadcast_event({'type': 'error', 'message': 'USB 连接中断，本条设备录音已取消'})

    def receive(self, kind, seq, payload):
        self.last_packet = time.monotonic()
        if kind == STATUS:
            try:
                self.board = json.loads(payload)
            except (ValueError, UnicodeError):
                self.metrics['invalid_status'] += 1
        elif kind == BUTTON and len(payload) == 1:
            if payload[0] and not self.recording and self.owner is None:
                self.recording = True
                self.voice_pcm.clear()
                self.broadcast_event({'type': 'device_recording', 'enabled': True})
            elif not payload[0] and self.recording:
                self.finish_voice()
        elif kind == MIC and len(payload) == PCM_BYTES and self.recording:
            self.metrics['mic_frames'] += 1
            self.voice_pcm.extend(payload)
            if len(self.voice_pcm) >= MAX_VOICE_BYTES:
                self.finish_voice()

    def broadcast_event(self, event):
        text = json.dumps(event)
        for state in self.clients.values():
            if not state['queue'].full():
                state['queue'].put_nowait(text)
            else:
                self.metrics['ws_dropped'] += 1

    def finish_voice(self):
        self.recording = False
        self.broadcast_event({'type': 'device_recording', 'enabled': False})
        if len(self.voice_pcm) >= 3200:
            message_id = str(uuid.uuid4())
            pcm = bytes(self.voice_pcm)
            timestamp = time.time()
            archive = None
            if self.annotations:
                archive = asyncio.create_task(self.archive_voice(message_id, timestamp, pcm))
                self.archive_tasks.add(archive)
                archive.add_done_callback(self.archive_tasks.discard)
            self.broadcast_event({
                'type': 'voice', 'id': message_id, 'source': 'device',
                'timestamp': timestamp, 'pcm': base64.b64encode(pcm).decode(),
                'translation_state': 'pending' if self.translator else None,
            })
            self.metrics['device_messages'] += 1
            if self.translator:
                if len(self.translation_tasks) >= 4:
                    self.broadcast_event({'type': 'translation', 'id': message_id, 'state': 'failed',
                                          'text': '本机识别繁忙，请稍后重试', 'error': '本机识别繁忙，请稍后重试'})
                else:
                    task = asyncio.create_task(self.translate_device(message_id, pcm, archive))
                    self.translation_tasks.add(task)
                    task.add_done_callback(self.translation_tasks.discard)
        self.voice_pcm.clear()

    async def archive_voice(self, message_id, timestamp, pcm):
        try:
            await asyncio.to_thread(self.annotations.add, message_id, timestamp, pcm)
            return True
        except Exception as exc:
            self.metrics['archive_errors'] += 1
            self.broadcast_event({'type': 'error', 'message': f'后台录音归档失败：{exc}'})
            return False

    async def translate_device(self, message_id, pcm, archive=None):
        try:
            result = await asyncio.to_thread(self.translator.describe_cat, pcm)
            if self.example_recognizer:
                try:
                    result = await asyncio.to_thread(self.example_recognizer.recognize,
                                                     pcm, result, message_id)
                except Exception as exc:
                    self.metrics['example_errors'] += 1
                    print(f'人工样本匹配失败 [{message_id}]: {exc}', flush=True)
                    result = {**result, 'note': '人工标注匹配暂不可用，当前显示声音类型提示',
                              'match_diagnostics': {'reason': 'error', 'message': str(exc)}}
            if archive is not None and await archive:
                try:
                    await asyncio.to_thread(self.annotations.prediction, message_id, result)
                except Exception:
                    self.metrics['archive_errors'] += 1
            self.broadcast_event({'type': 'translation', 'id': message_id, 'state': 'ready', 'result': result, 'text': result['text'],
                                  'note': result.get('note', '声学提示，非确定的猫语含义')})
        except Exception as exc:
            self.broadcast_event({'type': 'translation', 'id': message_id, 'state': 'failed', 'text': str(exc), 'error': str(exc)})

    async def translate_and_play(self, ws, pcm, message_id):
        delegated = False
        try:
            await ws.send(json.dumps({'type': 'delivery', 'id': message_id, 'state': 'translating'}))
            output, result = await asyncio.to_thread(self.translator.human_to_cat, pcm)
            await ws.send(json.dumps({'type': 'translation', 'id': message_id, 'state': 'ready',
                                      'result': result, 'text': f"人声：{result['text']}\n互动意图：{result['label']}",
                                      'note': result['note'], 'cat_pcm': base64.b64encode(output).decode()}))
            delegated = True
            await self.play_voice(ws, output, message_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                await ws.send(json.dumps({'type': 'translation', 'id': message_id, 'state': 'failed', 'text': str(exc), 'error': str(exc)}))
                await ws.send(json.dumps({'type': 'delivery', 'id': message_id, 'state': 'failed'}))
            except ConnectionClosed:
                pass
        finally:
            if not delegated:
                await self.stop_talk(ws)

    async def play_voice(self, ws, pcm, message_id):
        released = False
        try:
            await ws.send(json.dumps({'type': 'delivery', 'id': message_id, 'state': 'playing'}))
            await asyncio.to_thread(self.write, TALK, b'\x01')
            for offset in range(0, len(pcm), PCM_BYTES):
                frame = pcm[offset:offset + PCM_BYTES].ljust(PCM_BYTES, b'\x00')
                await asyncio.to_thread(self.write, PLAY, frame)
                self.metrics['frames_from_app'] += 1
                await asyncio.sleep(.02)
            await asyncio.sleep(.15)
            await self.stop_talk(ws)
            released = True
            await ws.send(json.dumps({'type': 'delivery', 'id': message_id, 'state': 'played'}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                await ws.send(json.dumps({'type': 'delivery', 'id': message_id, 'state': 'failed'}))
                await ws.send(json.dumps({'type': 'error', 'message': str(exc)}))
            except ConnectionClosed:
                pass
        finally:
            if not released:
                await self.stop_talk(ws)

    async def generate_phrase_and_play(self, ws, phrase_id, message_id):
        delegated = False
        try:
            await ws.send(json.dumps({'type': 'delivery', 'id': message_id, 'state': 'generating'}))
            output, result = await asyncio.to_thread(self.translator.phrase_to_cat, phrase_id)
            await ws.send(json.dumps({'type': 'translation', 'id': message_id, 'state': 'ready',
                                      'result': result, 'text': f"常用互动：{result['text']}\n互动意图：{result['label']}",
                                      'note': result['note'], 'cat_pcm': base64.b64encode(output).decode()}))
            delegated = True
            await self.play_voice(ws, output, message_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                await ws.send(json.dumps({'type': 'translation', 'id': message_id,
                                          'state': 'failed', 'text': str(exc)}))
                await ws.send(json.dumps({'type': 'delivery', 'id': message_id, 'state': 'failed'}))
            except ConnectionClosed:
                pass
        finally:
            if not delegated:
                await self.stop_talk(ws)

    def status(self):
        return {
            'type': 'status',
            'hardware_connected': time.monotonic() - self.last_packet < 3,
            'serial_port': self.port, 'sample_rate': 16000,
            'board': self.board, 'metrics': dict(self.metrics),
            'clients': len(self.clients), 'talking': self.owner is not None,
            'bridge_crc_errors': self.parser.bad_crc,
            'device_recording': self.recording,
            'ai': {'enabled': self.translator is not None,
                   'ready': bool(self.translator and self.translator.ready),
                   'error': self.translator.error if self.translator else '',
                   'jobs': len(self.translation_tasks)},
            'ai_enabled': self.translator is not None,
            'ai_ready': bool(self.translator and self.translator.ready),
            'error': self.last_error,
        }

    async def sender(self, ws, state):
        while True:
            data = await state['queue'].get()
            await ws.send(data)
            self.metrics['frames_to_app'] += 1

    async def stop_talk(self, ws):
        if self.owner is ws:
            try:
                await asyncio.to_thread(self.write, TALK, b'\x00')
            except Exception:
                pass
            finally:
                if self.owner is ws:
                    self.owner = None

    async def tone(self, ws):
        if self.owner is not None:
            raise RuntimeError('请先结束说话')
        self.owner = ws
        try:
            await asyncio.to_thread(self.write, TALK, b'\x01')
            for frame in range(50):
                samples = []
                for j in range(320):
                    t = (frame * 320 + j) / 16000
                    fade = min(1, t / .04, (1 - t) / .04)
                    samples.append(int(5000 * fade * math.sin(2 * math.pi * 440 * t)))
                await asyncio.to_thread(self.write, PLAY, struct.pack('<320h', *samples))
                self.metrics['test_frames'] += 1
                await asyncio.sleep(.02)
            await asyncio.sleep(.12)
        finally:
            await self.stop_talk(ws)

    async def handler(self, ws):
        state = {'listen': False, 'queue': asyncio.Queue(maxsize=16), 'pcm': bytearray()}
        self.clients[ws] = state
        sender = asyncio.create_task(self.sender(ws, state))
        try:
            await ws.send(json.dumps(self.status()))
            async for message in ws:
                command = {}
                try:
                    if isinstance(message, bytes):
                        if self.translator:
                            raise ValueError('识别模式请使用完整语音 voice_send')
                        if self.owner is not ws:
                            continue
                        if len(message) > 16384 or len(message) % 2:
                            raise ValueError('音频消息长度不正确')
                        state['pcm'].extend(message)
                        while len(state['pcm']) >= PCM_BYTES:
                            payload = bytes(state['pcm'][:PCM_BYTES])
                            del state['pcm'][:PCM_BYTES]
                            await asyncio.to_thread(self.write, PLAY, payload)
                            self.metrics['frames_from_app'] += 1
                    else:
                        command = json.loads(message)
                        if not isinstance(command, dict):
                            command = {}
                            raise ValueError('命令必须为 JSON 对象')
                        if command.get('type') == 'phrase_send':
                            if self.owner is not None or self.recording:
                                raise RuntimeError('设备忙，请等待当前语音结束')
                            if not self.translator or not self.translator.ready:
                                raise RuntimeError('本地猫叫生成尚未就绪')
                            phrase_id = command.get('phrase_id')
                            if not isinstance(phrase_id, str):
                                raise ValueError('请选择有效的常用互动词语')
                            self.owner = ws
                            self.playback_task = asyncio.create_task(self.generate_phrase_and_play(
                                ws, phrase_id, str(command.get('id', ''))))
                        elif command.get('type') in ('voice_send', 'cat_send'):
                            if self.owner is not None or self.recording:
                                raise RuntimeError('设备忙，请等待当前语音结束')
                            encoded = command.get('pcm', '')
                            if not isinstance(encoded, str):
                                raise ValueError('音频须为 Base64 字符串')
                            pcm = base64.b64decode(encoded, validate=True)
                            if not 3200 <= len(pcm) <= MAX_VOICE_BYTES or len(pcm) % 2:
                                raise ValueError('语音须为 0.1 至 60 秒的单声道 PCM')
                            replay = command['type'] == 'cat_send'
                            self.owner = ws
                            self.playback_task = asyncio.create_task(
                                self.translate_and_play(ws, pcm, str(command.get('id', '')))
                                if self.translator and not replay else self.play_voice(ws, pcm, str(command.get('id', ''))))
                        elif command.get('type') == 'listen':
                            state['listen'] = bool(command.get('enabled'))
                            while not state['queue'].empty():
                                state['queue'].get_nowait()
                        elif command.get('type') == 'talk':
                            if self.translator:
                                raise RuntimeError('识别模式请使用完整语音 voice_send')
                            if command.get('enabled'):
                                if self.owner is not None and self.owner is not ws:
                                    raise RuntimeError('另一客户端正在说话')
                                await asyncio.to_thread(self.write, TALK, b'\x01')
                                self.owner = ws
                                state['pcm'].clear()
                            else:
                                state['pcm'].clear()
                                await asyncio.sleep(.08)
                                await self.stop_talk(ws)
                        elif command.get('type') == 'test_tone':
                            await self.tone(ws)
                        elif command.get('type') == 'status':
                            await ws.send(json.dumps(self.status()))
                except (ValueError, RuntimeError, serial.SerialException, OSError) as exc:
                    if not self.playback_task or self.playback_task.done():
                        await self.stop_talk(ws)
                    if isinstance(message, str) and command.get('type') in ('voice_send', 'cat_send', 'phrase_send'):
                        await ws.send(json.dumps({'type': 'delivery', 'id': command.get('id'), 'state': 'failed'}))
                    await ws.send(json.dumps({'type': 'error', 'message': str(exc)}))
        except ConnectionClosed:
            pass
        finally:
            if self.owner is ws and self.playback_task:
                self.playback_task.cancel()
                await asyncio.gather(self.playback_task, return_exceptions=True)
            await self.stop_talk(ws)
            sender.cancel()
            self.clients.pop(ws, None)

    async def broadcast_status(self):
        while True:
            status = json.dumps(self.status())
            for ws in list(self.clients):
                try:
                    await ws.send(status)
                except ConnectionClosed:
                    pass
            await asyncio.sleep(1)

    async def run(self, host, port, admin_port=8766):
        self.loop = asyncio.get_running_loop()
        if self.translator:
            await asyncio.to_thread(self.translator.load)
            print('本地 AI 已就绪：YAMNet + Whisper Tiny (CPU int8)', flush=True)
        async with serve(self.handler, host, port, max_size=3000000, ping_interval=15):
            admin = None
            try:
                if self.annotations:
                    from annotations import annotation_server
                    admin = annotation_server(self.annotations, admin_port)
                    threading.Thread(target=admin.serve_forever, daemon=True).start()
                    print(f'猫叫标注后台 http://127.0.0.1:{admin.server_port}', flush=True)
                threading.Thread(target=self.reader, daemon=True).start()
                print(f'CAT bridge ws://{host}:{port} -> {self.port} @ 1000000', flush=True)
                await self.broadcast_status()
            finally:
                self.running = False
                if admin:
                    await asyncio.to_thread(admin.shutdown)
                    admin.server_close()
                await asyncio.gather(*self.archive_tasks, *self.translation_tasks, return_exceptions=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--serial', default='auto')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-ai', action='store_true', help='显式恢复原始语音透传模式')
    parser.add_argument('--admin-port', type=int, default=8766)
    parser.add_argument('--no-admin', action='store_true', help='关闭录音归档及本机标注后台')
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).resolve().parents[1] / 'data')
    args = parser.parse_args()
    translator = None
    if not args.no_ai:
        from local_ai import LocalTranslator
        translator = LocalTranslator()
    annotations = None
    if not args.no_admin:
        from annotations import AnnotationStore
        annotations = AnnotationStore(args.data_dir / 'cat-annotations.sqlite3')
    bridge = Bridge(args.serial, translator, annotations)
    try:
        asyncio.run(bridge.run('127.0.0.1', args.port, args.admin_port))
    except KeyboardInterrupt:
        bridge.running = False
