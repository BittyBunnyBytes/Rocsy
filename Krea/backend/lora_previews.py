"""Cache Civitai gallery media locally so previews work behind the gateway."""
from __future__ import annotations

import asyncio
import io
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image
from imageio_ffmpeg import get_ffmpeg_exe

from . import custom_loras, lora_links


async def _ffmpeg(*args: str) -> None:
    process = await asyncio.create_subprocess_exec(get_ffmpeg_exe(), '-y', '-loglevel', 'error', *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        _, error = await asyncio.wait_for(process.communicate(), timeout=180)
    except (TimeoutError, asyncio.CancelledError):
        process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise ValueError(error.decode(errors='replace')[-200:])


async def cache(item_id: str, previews: list[dict], token: str = '') -> list[dict]:
    folder = custom_loras.MEDIA_ROOT / item_id
    folder.mkdir(parents=True, exist_ok=True)
    result = []
    for entry in previews[:8]:
        if len(result) >= 4:
            break
        url = str(entry.get('url') or '')
        if urlsplit(url).scheme != 'https':
            continue
        number = len(result)
        poster = folder / f'{number}.jpg'
        video_path = folder / f'{number}.mp4'
        source = folder / f'{number}.source'
        try:
            content, mime, _, _ = await lora_links._stream(url, 'civitai', token, 80 * 1024 * 1024)
            is_video = entry.get('type') == 'video' or mime.startswith('video/')
            if is_video:
                source.write_bytes(content or b'')
                await _ffmpeg('-i', str(source), '-vf', 'scale=640:-2', '-t', '12', '-an', '-c:v', 'libx264',
                              '-preset', 'veryfast', '-crf', '27', '-movflags', '+faststart', str(video_path))
                await _ffmpeg('-i', str(video_path), '-frames:v', '1', str(poster))
            elif mime.startswith('image/'):
                with Image.open(io.BytesIO(content or b'')) as image:
                    image.thumbnail((640, 640))
                    image.convert('RGB').save(poster, 'JPEG', quality=85)
            else:
                continue
            result.append({'poster': f'api/lora-gallery/media/{item_id}/{number}.jpg',
                           'video': f'api/lora-gallery/media/{item_id}/{number}.mp4' if is_video else None,
                           'nsfw': int(entry.get('nsfwLevel') or 1)})
        except Exception:
            poster.unlink(missing_ok=True)
            video_path.unlink(missing_ok=True)
        finally:
            source.unlink(missing_ok=True)
    return result
