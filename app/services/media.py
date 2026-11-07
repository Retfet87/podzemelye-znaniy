"""Bounded image/video validation, independently of untrusted MIME metadata."""
from io import BytesIO
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import warnings

from PIL import Image, UnidentifiedImageError

LIMITS = {"image": 5 * 1024**2, "video": 20 * 1024**2}


def validate_media(kind, name, data):
    if kind not in LIMITS or not isinstance(data, bytes) or not 0 < len(data) <= LIMITS[kind]:
        raise ValueError("Фото: до 5 МБ; видео MP4: до 20 МБ. До трёх вложений.")
    if not isinstance(name, str) or len(name) > 100 or any(c in name for c in '/\\\r\n\x00"'):
        raise ValueError("Недопустимое имя медиафайла.")
    if kind == "image":
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(data)) as im:
                    if im.format not in ("PNG", "JPEG") or max(im.size) > 7680 or min(im.size) < 1:
                        raise ValueError
                    fmt = im.format
                    im.verify()
            expected = ('.png',) if fmt == 'PNG' else ('.jpg', '.jpeg')
            if Path(name).suffix.lower() not in expected:
                raise ValueError
        except (ValueError, OSError, UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning):
            raise ValueError("Нужна корректная фотография PNG/JPEG до 7680 × 7680.") from None
        return "image/png" if fmt == "PNG" else "image/jpeg"
    if Path(name).suffix.lower() != '.mp4' or len(data) < 12 or data[4:8] != b'ftyp':
        raise ValueError("Видео должно быть в формате MP4.")
    if not shutil.which('ffprobe'):
        raise ValueError("Проверка видео недоступна: требуется ffprobe. Используй Docker-сборку.")
    try:
        with tempfile.NamedTemporaryFile(suffix='.mp4') as tmp:
            tmp.write(data)
            tmp.flush()
            result = subprocess.run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file',
                '-show_entries', 'stream=codec_type:format=format_name', '-of', 'json', tmp.name],
                capture_output=True, timeout=10, check=True)
        info = json.loads(result.stdout)
        if 'mp4' not in info.get('format', {}).get('format_name', '').split(',') or not any(
                stream.get('codec_type') == 'video' for stream in info.get('streams', [])):
            raise ValueError
    except (ValueError, OSError, subprocess.SubprocessError):
        raise ValueError("Видео MP4 повреждено или не содержит видеодорожку.") from None
    return 'video/mp4'
