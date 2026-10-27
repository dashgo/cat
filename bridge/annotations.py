"""Local SQLite recording archive and loopback-only annotation UI."""
import io
import json
from pathlib import Path
import sqlite3
import time
import uuid
import wave
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class AnnotationStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('''CREATE TABLE IF NOT EXISTS recordings (
                id TEXT PRIMARY KEY, timestamp REAL NOT NULL, pcm BLOB NOT NULL,
                prediction TEXT, label TEXT NOT NULL DEFAULT '',
                context TEXT NOT NULL DEFAULT '', annotated_at REAL)''')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(recordings)')}
            for name, kind in [('deleted_at', 'REAL'), ('feature', 'BLOB'), ('feature_version', 'TEXT')]:
                if name not in columns:
                    db.execute(f'ALTER TABLE recordings ADD COLUMN {name} {kind}')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def add(self, message_id, timestamp, pcm):
        uuid.UUID(message_id)
        if not 3200 <= len(pcm) <= 1920000 or len(pcm) % 2:
            raise ValueError('录音格式不正确')
        with self.connect() as db:
            db.execute('INSERT OR IGNORE INTO recordings(id,timestamp,pcm) VALUES(?,?,?)',
                       (message_id, timestamp, pcm))

    def prediction(self, message_id, result):
        with self.connect() as db:
            db.execute('UPDATE recordings SET prediction=? WHERE id=?',
                       (json.dumps(result, ensure_ascii=False), message_id))

    def list(self, page=0, pending=False, deleted=False):
        where = 'WHERE deleted_at IS NOT NULL' if deleted else 'WHERE deleted_at IS NULL'
        if pending: where += " AND label=''"
        with self.connect() as db:
            total = db.execute(f'SELECT count(*) FROM recordings {where}').fetchone()[0]
            rows = db.execute(f'''SELECT id,timestamp,length(pcm)/32000.0 AS duration,
                prediction,label,context,annotated_at,deleted_at FROM recordings {where}
                ORDER BY timestamp DESC,id LIMIT 25 OFFSET ?''', (page * 25,)).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item['prediction'] = json.loads(item['prediction']) if item['prediction'] else None
            items.append(item)
        return {'items': items, 'total': total, 'page': page}

    def annotate(self, message_id, label, context):
        if not isinstance(label, str) or not isinstance(context, str):
            raise ValueError('标签和情境必须为文字')
        label, context = label.strip(), context.strip()
        if len(label) > 100 or len(context) > 2000:
            raise ValueError('标签最多100字，情境最多2000字')
        with self.connect() as db:
            changed = db.execute('UPDATE recordings SET label=?,context=?,annotated_at=? WHERE id=? AND deleted_at IS NULL',
                                 (label, context, time.time() if label else None, message_id)).rowcount
        if not changed:
            raise KeyError(message_id)
        return {'id': message_id, 'label': label, 'context': context}

    def set_deleted(self, message_id, deleted):
        with self.connect() as db:
            changed = db.execute('UPDATE recordings SET deleted_at=? WHERE id=?',
                                 (time.time() if deleted else None, message_id)).rowcount
        if not changed: raise KeyError(message_id)
        return {'id': message_id, 'deleted': deleted}

    def examples(self, exclude_id=None):
        with self.connect() as db:
            return [dict(row) for row in db.execute('''SELECT id,label,context,feature,feature_version
                FROM recordings WHERE deleted_at IS NULL AND label<>'' AND id<>?''', (exclude_id or '',))]

    def pcm(self, message_id):
        with self.connect() as db:
            row = db.execute('SELECT pcm FROM recordings WHERE id=?', (message_id,)).fetchone()
        if row is None: raise KeyError(message_id)
        return row['pcm']

    def cache_feature(self, message_id, feature, version):
        with self.connect() as db:
            db.execute('UPDATE recordings SET feature=?,feature_version=? WHERE id=?',
                       (feature, version, message_id))

    def audio(self, message_id):
        with self.connect() as db:
            row = db.execute('SELECT pcm FROM recordings WHERE id=?', (message_id,)).fetchone()
        if row is None:
            raise KeyError(message_id)
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
            output.writeframes(row['pcm'])
        return buffer.getvalue()


def annotation_server(store, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass

        def respond(self, status, body, content_type='application/json; charset=utf-8'):
            if isinstance(body, dict):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "frame-ancestors 'none'")
            self.end_headers(); self.wfile.write(body)

        def allowed_host(self):
            return self.headers.get('Host') in (f'127.0.0.1:{self.server.server_port}',
                                                f'localhost:{self.server.server_port}')

        def do_GET(self):
            if not self.allowed_host():
                return self.respond(403, {'error': '仅允许本机访问'})
            url = urlsplit(self.path)
            try:
                if url.path == '/':
                    return self.respond(200, Path(__file__).with_name('annotation.html').read_bytes(),
                                        'text/html; charset=utf-8')
                if url.path == '/api/recordings':
                    query = parse_qs(url.query)
                    page = int(query.get('page', ['0'])[0])
                    if page < 0 or page > 1000000: raise ValueError('页码不正确')
                    return self.respond(200, store.list(page, query.get('pending') == ['1'],
                                                       query.get('deleted') == ['1']))
                if url.path.startswith('/audio/') and url.path.endswith('.wav'):
                    mid = url.path.removeprefix('/audio/').removesuffix('.wav')
                    uuid.UUID(mid)
                    return self.respond(200, store.audio(mid), 'audio/wav')
                self.respond(404, {'error': '记录不存在'})
            except (ValueError, KeyError): self.respond(404, {'error': '记录不存在'})
            except sqlite3.Error: self.respond(500, {'error': '读取记录失败，请稍后重试'})

        def do_POST(self):
            origin = self.headers.get('Origin')
            allowed = (f'http://127.0.0.1:{self.server.server_port}',
                       f'http://localhost:{self.server.server_port}')
            if (not self.allowed_host() or (origin is not None and origin not in allowed)
                    or self.headers.get('Content-Type', '').split(';')[0] != 'application/json'):
                return self.respond(403, {'error': '请求来源不允许'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 16384: raise ValueError('请求长度不正确')
                url = urlsplit(self.path)
                prefix = next((p for p in ('/api/annotations/', '/api/delete/', '/api/restore/')
                               if url.path.startswith(p)), None)
                if prefix is None:
                    return self.respond(404, {'error': '记录不存在'})
                mid = url.path.removeprefix(prefix)
                uuid.UUID(mid)
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict): raise ValueError('请求格式不正确')
                if prefix == '/api/annotations/':
                    result = store.annotate(mid, body.get('label', ''), body.get('context', ''))
                else:
                    result = store.set_deleted(mid, prefix == '/api/delete/')
                self.respond(200, result)
            except (ValueError, UnicodeError) as exc: self.respond(400, {'error': str(exc)})
            except KeyError: self.respond(404, {'error': '记录不存在'})
            except sqlite3.Error: self.respond(500, {'error': '保存失败，请稍后重试'})

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)
