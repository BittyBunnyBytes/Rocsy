"""One-time public source-page reads and bounded local preview caching for custom LoRAs."""
from __future__ import annotations

import asyncio
import io
import json
import re
import shutil
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urljoin

from PIL import Image

from . import agentic_web, custom_loras


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
    def handle_starttag(self, tag, attrs):
        if tag in {'p', 'br', 'li', 'div', 'h2', 'h3'}:
            self.parts.append('\n')
    def handle_data(self, data):
        self.parts.append(data)


def _plain(value: str) -> str:
    parser = _Text()
    parser.feed(value or '')
    return ' '.join(''.join(parser.parts).split())


async def source(url: str) -> tuple[str, list[dict]]:
    parts = urlsplit(url)
    match = re.search(r'/models/(\d+)', parts.path) if parts.hostname in {
        'civitai.com', 'www.civitai.com', 'civitai.red', 'civitai.green'} else None
    if match:
        model_data, _, _ = await agentic_web.safe_get(f'https://civitai.com/api/v1/models/{match.group(1)}', 3_000_000)
        model = json.loads(model_data)
        version_id = (parse_qs(parts.query).get('modelVersionId') or [None])[0]
        if version_id and re.fullmatch(r'\d+', version_id):
            version_data, _, _ = await agentic_web.safe_get(f'https://civitai.com/api/v1/model-versions/{version_id}', 3_000_000)
            version = json.loads(version_data)
        else:
            version = (model.get('modelVersions') or [{}])[0]
        text = '\n'.join((
            f"Name: {model.get('name', '')}", f"Creator: {(model.get('creator') or {}).get('username', '')}",
            f"Version: {version.get('name', '')}", f"Base model: {version.get('baseModel', '')}",
            f"Trained words: {', '.join(version.get('trainedWords') or [])}",
            f"Model description: {_plain(model.get('description') or '')}",
            f"Version notes: {_plain(version.get('description') or '')}",
        ))[:25000]
        media = []
        for item in (version.get('images') or [])[:12]:
            media_url = item.get('url', '')
            if media_url.startswith('https://'):
                is_video = item.get('type') == 'video' or media_url.lower().endswith(('.mp4','.webm'))
                transform = 'transcode=true,width=720,optimized=true' if is_video else 'width=640'
                media_url = re.sub(r'/(original=true|width=\d+[^/]*)/', f'/{transform}/', media_url, count=1)
                media.append({'url': media_url, 'video': is_video,
                              'nsfw': int(item.get('nsfwLevel') or 1)})
        return text, media
    page = await agentic_web.page(url)
    body, _, separator = page.partition('\nMedia URLs: ')
    media = []
    if separator:
        for value in separator.split(' | ')[:12]:
            media_url = urljoin(url, value.strip())
            suffix = urlsplit(media_url).path.lower()
            if suffix.endswith(('.mp4','.webm','.mov','.jpg','.jpeg','.png','.webp')):
                media.append({'url': media_url, 'video': suffix.endswith(('.mp4','.webm','.mov')), 'nsfw': 1})
    return body[:25000], media


async def cache_previews(item_id: str, candidates: list[dict]) -> list[dict]:
    folder = custom_loras.MEDIA_ROOT / item_id
    folder.mkdir(parents=True, exist_ok=True)
    results = []
    for candidate in candidates[:8]:
        if len(results) >= 4:
            break
        number = len(results)
        try:
            content, mime, _ = await agentic_web.safe_get(candidate['url'], 80 * 1024 * 1024)
            is_video = candidate['video'] or mime.startswith('video/')
            poster = folder / f'{number}.jpg'
            video = None
            if is_video:
                source_path = folder / f'{number}.source'
                source_path.write_bytes(content)
                output = folder / f'{number}.mp4'
                process = await asyncio.create_subprocess_exec('ffmpeg','-y','-loglevel','error','-i',str(source_path),
                    '-vf','scale=640:-2','-t','12','-an','-c:v','libx264','-preset','veryfast','-crf','27','-movflags','+faststart',str(output),
                    stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE)
                try:
                    _, error = await asyncio.wait_for(process.communicate(), timeout=180)
                except (TimeoutError, asyncio.CancelledError):
                    process.kill()
                    await process.wait()
                    raise
                if process.returncode or not output.is_file():
                    raise ValueError('Could not make preview video: ' + error.decode(errors='replace')[-120:])
                process = await asyncio.create_subprocess_exec('ffmpeg','-y','-loglevel','error','-i',str(output),'-frames:v','1',str(poster),
                    stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE)
                try:
                    await asyncio.wait_for(process.communicate(), timeout=30)
                except (TimeoutError, asyncio.CancelledError):
                    process.kill()
                    await process.wait()
                    raise
                source_path.unlink(missing_ok=True)
                if not poster.is_file():
                    raise ValueError('Could not make preview poster')
                video = f'api/lora-gallery/media/{item_id}/{number}.mp4'
            elif mime.startswith('image/') or urlsplit(candidate['url']).path.lower().endswith(('.png','.jpg','.jpeg','.webp')):
                with Image.open(io.BytesIO(content)) as image:
                    image.thumbnail((640,640))
                    image.convert('RGB').save(poster, 'JPEG', quality=85)
            else:
                continue
            results.append({'poster': f'api/lora-gallery/media/{item_id}/{number}.jpg', 'video': video,
                            'nsfw': candidate.get('nsfw', 1)})
        except Exception:
            for stale in (folder / f'{number}.source', folder / f'{number}.mp4', folder / f'{number}.jpg'):
                stale.unlink(missing_ok=True)
            continue
    return results
