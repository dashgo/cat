#!/usr/bin/env python3
"""Download pinned public model files; verify SHA-256 before atomic installation."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import urllib.request

ROOT = Path(__file__).resolve().parents[1]


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--mirror', action='store_true', help='使用 hf-mirror.com 下载 Hugging Face 权重（校验和不变）')
    args = parser.parse_args()
    manifest = json.loads((ROOT / 'bridge/model-manifest.json').read_text())

    def fetch(item):
        target = ROOT / 'models' / item['path']
        if target.is_file() and digest(target) == item['sha256']:
            print('Verified', item['path'], flush=True)
            return
        url = item['url']
        if args.mirror:
            url = url.replace('https://huggingface.co/', 'https://hf-mirror.com/')
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_suffix(target.suffix + '.part')
        request = urllib.request.Request(url, headers={'User-Agent': 'CAT-Local-AI-Setup/1.0'})
        with urllib.request.urlopen(request, timeout=120) as response, part.open('wb') as output:
            while block := response.read(1024 * 1024):
                output.write(block)
        if part.stat().st_size != item['bytes'] or digest(part) != item['sha256']:
            raise RuntimeError('模型文件校验失败：' + item['path'])
        part.replace(target)
        print('Installed', item['path'], flush=True)

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(fetch, manifest))


if __name__ == '__main__':
    main()
