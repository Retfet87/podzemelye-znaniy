"""MAX image/video upload contracts; URLs restricted by the existing CDN guard."""
import json
from pathlib import Path
from uuid import uuid4

from app.max_api.files import transfer
from app.services.media import LIMITS, validate_media


def receive_media(client, attachment):
    if not isinstance(attachment, dict):
        raise ValueError("Прикрепи фото PNG/JPEG или видео MP4.")
    kind = attachment.get('type')
    payload = attachment.get('payload') or {}
    if not isinstance(payload, dict):
        raise ValueError("Медиа недоступно.")
    name = attachment.get('filename', '')
    if kind == 'file':
        kind = 'video' if str(name).lower().endswith('.mp4') else 'image'
    if kind not in LIMITS:
        raise ValueError("Прикрепи фото PNG/JPEG или видео MP4.")
    url = payload.get('url')
    if kind == 'video' and not url:
        token = payload.get('token')
        if not isinstance(token, str) or not token or len(token) > 4096 or any(c in token for c in '/?#\r\n'):
            raise ValueError("Видео недоступно.")
        urls = client.request('GET', '/videos/' + token).get('urls', {})
        if isinstance(urls, dict):
            url = next((urls[k] for k in ('mp4_720', 'mp4_480', 'mp4_360', 'mp4_1080') if urls.get(k)), None)
    data = transfer(client.context, url, 'GET', limit=LIMITS[kind])
    if not name:
        name = 'video.mp4' if kind == 'video' else ('photo.png' if data.startswith(b'\x89PNG') else 'photo.jpg')
    validate_media(kind, name, data)
    return kind, name, data


def upload_media(client, kind, name, data):
    mime = validate_media(kind, name, data)
    result = client.request('POST', '/uploads', {'type': kind})
    boundary = uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="data"; filename="{name}"\r\n'
            f'Content-Type: {mime}\r\n\r\n').encode() + data + f'\r\n--{boundary}--\r\n'.encode()
    raw = transfer(client.context, result.get('url'), 'POST', limit=65536, body=body,
                   headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    if kind == 'video':
        token = result.get('token')
        if not isinstance(token, str) or not token:
            raise ValueError("MAX не подтвердил загрузку видео.")
        return {'type': kind, 'payload': {'token': token}}
    try:
        reply = json.loads(raw)
        photos = reply.get('photos')
        if isinstance(photos, dict) and photos and all(isinstance(v, dict) and isinstance(v.get('token'), str) and v['token'] for v in photos.values()):
            return {'type': 'image', 'payload': {'photos': photos}}
        token = reply.get('token')
        if isinstance(token, str) and token:
            return {'type': 'image', 'payload': {'token': token}}
    except (ValueError, AttributeError, UnicodeError):
        pass
    raise ValueError("MAX не подтвердил загрузку фотографии.")


def welcome(client):
    path = Path(__file__).resolve().parents[2] / 'assets/welcome.png'
    return upload_media(client, 'image', path.name, path.read_bytes())
