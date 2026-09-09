"""Sonamos Mas Roku GIF Bridge - cPanel/Passenger edition.
Converts remote GIF/WebP/animated PNG media to bounded PNG sprite atlases for Roku.
"""
from __future__ import annotations
import hashlib
import io
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urljoin, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from PIL import Image

BASE = Path(__file__).resolve().parent
CACHE = Path(os.environ.get('SLAM_CACHE', str(BASE / 'cache'))).resolve()
MAX_BYTES = 12 * 1024 * 1024
MAX_PIXELS = 4_000_000
MAX_FRAMES = 500
CACHE_LIMIT = 256 * 1024 * 1024
TTL = 86400
ALLOWED = {
    'static.klipy.com', 'static1.klipy.com', 'static2.klipy.com',
    'media.tenor.com', 'c.tenor.com',
    'media.giphy.com', 'i.giphy.com',
    'media0.giphy.com', 'media1.giphy.com', 'media2.giphy.com',
    'media3.giphy.com', 'media4.giphy.com',
    'api.javicdev.com',
    'sonamosmas.com', 'www.sonamosmas.com',
    'impacrecords.com', 'www.impacrecords.com',
}
GATE = threading.BoundedSemaphore(2)
LOCK = threading.Lock()
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
CACHE.mkdir(parents=True, exist_ok=True)

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def checked_url(url: str) -> str:
    p = urlsplit(url)
    if (p.scheme != 'https' or p.hostname not in ALLOWED or p.username
            or p.password or p.port not in (None, 443)):
        raise ValueError('Origen multimedia no autorizado')
    addresses = socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)
    if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
        raise ValueError('Direccion de red no permitida')
    return url

def download(url: str) -> bytes:
    opener = build_opener(NoRedirect())
    for _ in range(4):
        checked_url(url)
        try:
            with opener.open(Request(url, headers={'User-Agent': 'SonamosRoku/0.7.0'}), timeout=10) as r:
                length = int(r.headers.get('Content-Length', '0') or 0)
                if length > MAX_BYTES:
                    raise ValueError('Archivo demasiado grande')
                data = r.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise ValueError('Archivo demasiado grande')
                return data
        except HTTPError as e:
            if e.code not in (301, 302, 303, 307, 308):
                raise
            url = urljoin(url, e.headers.get('Location', ''))
    raise ValueError('Demasiadas redirecciones')

def make_atlas(data: bytes) -> tuple[bytes, dict]:
    with Image.open(io.BytesIO(data)) as im:
        if im.format not in ('GIF', 'WEBP', 'PNG'):
            raise ValueError('Formato de animacion no admitido')
        if im.width * im.height > MAX_PIXELS:
            raise ValueError('Resolucion demasiado grande')
        count = getattr(im, 'n_frames', 1)
        if count > MAX_FRAMES:
            raise ValueError('Animacion demasiado larga')
        durations = []
        for i in range(count):
            im.seek(i)
            durations.append(max(20, min(60000, int(im.info.get('duration', 100) or 100))))
        total = max(1, sum(durations))
        period = max(125, math.ceil(total / 64))
        marks = list(range(0, total, period)) or [0]
        out_durations = [min(period, max(1, total - t)) for t in marks]
        scale = min(128 / im.width, 96 / im.height, 1)
        width, height = max(1, round(im.width * scale)), max(1, round(im.height * scale))
        frames = []
        end = 0
        mark_index = 0
        for i, delay in enumerate(durations):
            im.seek(i)
            end += delay
            if mark_index < len(marks) and marks[mark_index] < end:
                frame = im.convert('RGBA').resize((width, height), Image.Resampling.LANCZOS)
                while mark_index < len(marks) and marks[mark_index] < end:
                    frames.append(frame.copy())
                    mark_index += 1
        if not frames:
            im.seek(0)
            frames = [im.convert('RGBA').resize((width, height), Image.Resampling.LANCZOS)]
            out_durations = [100]
            total = 100
        columns = min(8, len(frames))
        rows = math.ceil(len(frames) / columns)
        sheet = Image.new('RGBA', (columns * width, rows * height))
        for i, frame in enumerate(frames):
            sheet.paste(frame, ((i % columns) * width, (i // columns) * height))
        buf = io.BytesIO()
        sheet.save(buf, format='PNG', compress_level=4)
        return buf.getvalue(), {
            'width': width, 'height': height, 'columns': columns,
            'durations': out_durations, 'loopMs': total,
        }

def atomic_write(path: Path, data: bytes):
    CACHE.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=CACHE, delete=False) as f:
        f.write(data)
        temp = Path(f.name)
    temp.replace(path)

def trim_cache():
    files = sorted(CACHE.glob('*.png'), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    for p in files:
        size = p.stat().st_size
        if time.time() - p.stat().st_mtime <= TTL and total <= CACHE_LIMIT:
            continue
        p.unlink(missing_ok=True)
        p.with_suffix('.json').unlink(missing_ok=True)
        total -= size

def convert(url: str) -> dict:
    p = urlsplit(url)
    if p.scheme != 'https' or p.hostname not in ALLOWED or p.username or p.password or p.port not in (None, 443):
        raise ValueError('Origen multimedia no autorizado')
    key = hashlib.sha256(('atlas-v1:' + url).encode()).hexdigest()
    meta_path, png_path = CACHE / (key + '.json'), CACHE / (key + '.png')
    with LOCK:
        if meta_path.exists() and png_path.exists() and time.time() - meta_path.stat().st_mtime < TTL:
            return json.loads(meta_path.read_text())
    data = download(url)
    png, meta = make_atlas(data)
    meta['image'] = '/atlas/' + key + '.png'
    with LOCK:
        trim_cache()
        atomic_write(png_path, png)
        atomic_write(meta_path, json.dumps(meta).encode())
    return meta

def _response(start_response, status: str, body: bytes, kind='application/json', cache='no-store'):
    headers = [
        ('Content-Type', kind),
        ('Content-Length', str(len(body))),
        ('Cache-Control', cache),
        ('X-Content-Type-Options', 'nosniff'),
        ('Access-Control-Allow-Origin', '*'),
    ]
    start_response(status, headers)
    return [body]

def application(environ, start_response):
    try:
        if environ.get('REQUEST_METHOD', 'GET').upper() != 'GET':
            return _response(start_response, '405 Method Not Allowed', b'{"error":"GET only"}')
        path = environ.get('PATH_INFO', '/') or '/'
        query_string = environ.get('QUERY_STRING', '') or ''
        if len(path) + len(query_string) > 8192:
            return _response(start_response, '414 URI Too Long', b'{"error":"URL demasiado larga"}')
        if path == '/healthz':
            return _response(start_response, '200 OK', b'{"status":"ok","version":"cpanel-1.1"}')
        if re.fullmatch(r'/atlas/[a-f0-9]{64}\.png', path):
            file_path = CACHE / path.rsplit('/', 1)[1]
            try:
                return _response(start_response, '200 OK', file_path.read_bytes(), 'image/png', 'public, max-age=3600')
            except FileNotFoundError:
                return _response(start_response, '404 Not Found', b'{"error":"No disponible"}')
        if path != '/convert':
            return _response(start_response, '404 Not Found', b'{}')
        if not GATE.acquire(blocking=False):
            return _response(start_response, '503 Service Unavailable', b'{"error":"Ocupado, reintentar"}')
        try:
            values = parse_qs(query_string).get('url', [])
            if len(values) != 1:
                raise ValueError('Falta URL')
            result = convert(values[0])
            return _response(start_response, '200 OK', json.dumps(result).encode())
        except (ValueError, OSError, Image.DecompressionBombError):
            return _response(start_response, '422 Unprocessable Entity', b'{"error":"No se pudo convertir este archivo"}')
        finally:
            GATE.release()
    except Exception:
        return _response(start_response, '500 Internal Server Error', b'{"error":"Error interno"}')
