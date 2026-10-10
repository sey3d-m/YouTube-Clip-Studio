#!/usr/bin/env python3
"""YouTube Clip Studio — video -> Gemini editorial plan -> FFmpeg Shorts -> release pack.
Runs in GitHub Actions; no persistent server. Python 3.11+.
"""
from __future__ import annotations

import csv
import html
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pydantic import BaseModel, Field

LOG = logging.getLogger('clip-studio')
MAX_DOWNLOAD_BYTES = 3 * 1024**3
BROWSER_USER_AGENT = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                      'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
LANGUAGES = {'fa': 'فارسی', 'bn': 'বাংলা', 'ar': 'العربية', 'en': 'English'}


class Candidate(BaseModel):
    start: float = Field(description='Start timestamp in seconds, relative to source video')
    end: float = Field(description='End timestamp in seconds, relative to source video')
    title: str = ''
    alternative_titles: list[str] = Field(default_factory=list)
    description: str = ''
    hashtags: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    pinned_comment: str = ''
    cover_text: str = ''
    why_selected: str = ''


class Plan(BaseModel):
    video_summary: str = ''
    clips: list[Candidate] = Field(default_factory=list)


def run_cmd(command: list[str], *, quiet: bool = False) -> str:
    LOG.info('Executing media command: %s', command[0])
    res = subprocess.run(command, text=True, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, check=False)
    if res.returncode:
        raise RuntimeError(f"Command failed ({res.returncode}): {' '.join(command[:4])}\n{res.stderr[-2400:]}")
    if not quiet and res.stderr:
        LOG.debug('Command stderr: %s', res.stderr[-300:])
    return res.stdout


def seconds_to_hms(seconds: float) -> str:
    total = int(max(0, seconds))
    return f'{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}'


def youtube_timestamp_link(url: str, start: float) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    video_id = None
    if host in ('youtube.com', 'www.youtube.com', 'm.youtube.com'):
        video_id = parse_qs(parsed.query).get('v', [''])[0]
        if not video_id and parsed.path.startswith('/shorts/'):
            video_id = parsed.path.split('/')[2]
    elif host in ('youtu.be', 'www.youtu.be'):
        video_id = parsed.path.strip('/').split('/')[0]
    if video_id and re.fullmatch(r'[a-zA-Z0-9_-]{11}', video_id):
        return f'https://www.youtube.com/watch?v={video_id}&t={int(start)}s'
    return ''


def is_youtube(url: str) -> bool:
    return (urlparse(url).hostname or '').lower() in (
        'youtube.com', 'www.youtube.com', 'm.youtube.com', 'youtu.be', 'www.youtu.be'
    )


def is_aparat(url: str) -> bool:
    return (urlparse(url).hostname or '').lower() in ('aparat.com', 'www.aparat.com')


def is_aparat_cdn(url: str) -> bool:
    """Recognize direct Aparat media URLs (distinct from its player page)."""
    host = (urlparse(url).hostname or '').lower()
    return host == 'asset.aparat.com' or host.endswith('.asset.aparat.com')


def is_google_drive(url: str) -> bool:
    return (urlparse(url).hostname or '').lower() in ('drive.google.com', 'docs.google.com')


def aparat_video_hash(page_url: str) -> str:
    """Get the public Aparat video hash without following arbitrary page URLs."""
    parsed = urlparse(page_url)
    if not is_aparat(page_url):
        raise ValueError('Expected an Aparat player page URL.')
    match = re.fullmatch(r'/v/([A-Za-z0-9]+)/*', parsed.path)
    if not match:
        raise ValueError('Expected a single Aparat video page: https://www.aparat.com/v/VIDEO_ID')
    return match.group(1)


def aparat_media_candidates(payload: dict) -> list[str]:
    """Parse media links in Aparat's public video-info JSON; do not trust external hosts.

    The website API may change without notice. Skip unknown/malformed entries.
    """
    data = payload.get('data', {}) if isinstance(payload, dict) else {}
    if isinstance(data, list):
        data = data[0] if data else {}
    attrs = data.get('attributes', {}) if isinstance(data, dict) else {}
    if not isinstance(attrs, dict):
        return []
    links = attrs.get('file_link_all') or []
    if not isinstance(links, list):
        return []
    ranked = []
    for item in links:
        if not isinstance(item, dict):
            continue
        label = str(item.get('profile') or item.get('label') or item.get('quality') or '')
        found = re.search(r'(\d{3,4})[pP]?', label)
        height = int(found.group(1)) if found else 0
        # Avoid needlessly downloading 4K files on GitHub's limited disk/compute.
        if height > 1080:
            continue
        urls = item.get('urls') or []
        if isinstance(urls, str):
            urls = [urls]
        for candidate in urls if isinstance(urls, list) else []:
            if isinstance(candidate, dict):
                candidate = candidate.get('url') or candidate.get('src') or ''
            if not isinstance(candidate, str):
                continue
            parsed = urlparse(candidate)
            host = (parsed.hostname or '').lower()
            # Only the same provider's media hosts; no arbitrary external URLs.
            if parsed.scheme != 'https' or not (host == 'aparat.com' or host.endswith('.aparat.com')):
                continue
            if parsed.username or parsed.password or parsed.port not in (None, 443):
                continue
            rank = (height, 1 if parsed.path.lower().endswith('.mp4') else 0)
            ranked.append((rank, candidate))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return list(dict.fromkeys(link for _, link in ranked))


def download_aparat_api(page_url: str, work_dir: Path) -> Path:
    """Resolve the video from Aparat's public JSON endpoint; fail cleanly if blocked.

    This is not an authentication bypass. 403/geo/private limitations remain.
    """
    import requests
    video_id = aparat_video_hash(page_url)
    endpoint = f'https://www.aparat.com/api/fa/v1/video/video/show/videohash/{video_id}'
    LOG.info('Trying Aparat public video-info API for video %s', video_id)
    headers = {'User-Agent': BROWSER_USER_AGENT, 'Accept': 'application/json',
               'Referer': page_url}
    response = requests.get(endpoint, timeout=(15, 30), headers=headers)
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError('Aparat video API did not return JSON.') from exc
    links = aparat_media_candidates(data)
    if not links:
        raise RuntimeError('Aparat public API did not supply a usable media URL.')
    LOG.info('Aparat API supplied %s media URL(s); trying available formats.', len(links))
    target = work_dir / 'source.mp4'
    failures = []
    # Avoid dozens of CDN attempts in case of server-side IP restrictions.
    for candidate in links[:10]:
        try:
            is_hls = '.m3u8' in urlparse(candidate).path.lower()
            if is_hls:
                LOG.info('Trying Aparat HLS source via FFmpeg')
                media_headers = f'User-Agent: {BROWSER_USER_AGENT}\r\nReferer: {page_url}\r\n'
                run_cmd(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                         '-headers', media_headers, '-i', candidate, '-c', 'copy',
                         '-movflags', '+faststart', str(target)], quiet=True)
            else:
                LOG.info('Trying Aparat video file from provider CDN')
                media_headers = {'User-Agent': BROWSER_USER_AGENT,
                                 'Referer': page_url, 'Origin': 'https://www.aparat.com'}
                partial = target.with_suffix('.partial')
                try:
                    with requests.get(candidate, stream=True, timeout=(20, 120),
                                      allow_redirects=False, headers=media_headers) as media:
                        media.raise_for_status()
                        ctype = media.headers.get('Content-Type', '').lower()
                        if 'text/html' in ctype or 'application/json' in ctype:
                            raise ValueError('CDN responded with HTML/JSON rather than video.')
                        size = int(media.headers.get('Content-Length') or 0)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise ValueError('Aparat video exceeds 3GB limit.')
                        total = 0
                        with partial.open('wb') as fp:
                            for chunk in media.iter_content(chunk_size=1024 * 1024):
                                if chunk:
                                    total += len(chunk)
                                    if total > MAX_DOWNLOAD_BYTES:
                                        raise ValueError('Aparat video exceeds 3GB limit.')
                                    fp.write(chunk)
                    partial.replace(target)
                finally:
                    partial.unlink(missing_ok=True)
            if target.is_file() and 0 < target.stat().st_size <= MAX_DOWNLOAD_BYTES:
                LOG.info('Aparat API download completed successfully.')
                return target
            raise RuntimeError('Empty or oversized media file')
        except Exception as exc:
            target.unlink(missing_ok=True)
            failures.append(str(exc)[:140])
            LOG.warning('Aparat media candidate failed (%s/%s): %s',
                        len(failures), min(len(links), 10), failures[-1])
    raise RuntimeError('All Aparat API media links were unavailable from the runner: '
                       + ' | '.join(failures[:3]))


def fetch_video(url: str, work_dir: Path) -> Path:
    """Download a publicly accessible video for which the user has permission.

    Aparat *player pages* use yt-dlp. Direct Aparat CDN URLs use HTTP with a
    page Referer, but can still expire or block datacenter IPs; in that case a
    player page URL or an accessible copy in Google Drive is required.
    """
    if not url.startswith(('https://', 'http://')):
        raise ValueError('VIDEO_URL must start with https:// or http://')
    target = work_dir / 'source.mp4'
    if is_google_drive(url):
        import gdown
        LOG.info('Downloading Google Drive share file (must be accessible to the runner)')
        result = gdown.download(url=url, output=str(target), quiet=False, fuzzy=True)
        if not result or not target.exists():
            raise RuntimeError('Google Drive download failed. Check link access (Anyone with the link).')
    elif is_youtube(url) or is_aparat(url):
        provider = 'Aparat' if is_aparat(url) else 'YouTube'
        errors = []
        # yt-dlp Aparat extractor has a known 'Unable to extract og:title' issue
        # (yt-dlp #17425). Try the provider JSON endpoint *before* yt-dlp.
        if is_aparat(url):
            try:
                target = download_aparat_api(url, work_dir)
            except Exception as exc:
                errors.append(f'API: {str(exc)[:250]}')
                LOG.warning('Aparat API route unavailable; trying yt-dlp: %s', str(exc)[:250])
                target = None
        else:
            target = None
        if target is None:
            import yt_dlp
            opts = {
                'format': 'bv*[height<=1080]+ba/b[height<=1080]/b',
                'outtmpl': str(work_dir / 'source.%(ext)s'),
                'merge_output_format': 'mp4',
                'noplaylist': True,
                'quiet': False,
                'retries': 3,
                'fragment_retries': 3,
                'http_headers': {'User-Agent': BROWSER_USER_AGENT,
                                 'Referer': 'https://www.aparat.com/' if is_aparat(url) else 'https://www.youtube.com/'},
            }
            LOG.info('Trying %s player page using yt-dlp', provider)
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    ydl.download([url])
            except Exception as exc:
                errors.append(f'yt-dlp: {str(exc)[:250]}')
                if is_aparat(url):
                    raise RuntimeError(
                        'Aparat download failed both via the public API and yt-dlp. '
                        'yt-dlp has a known Aparat og:title extractor bug (issue #17425), '
                        'and Aparat may block video downloads from GitHub IPs. '
                        'Upload a video that you have the rights to use to Google Drive, '
                        'share it as Viewer using Anyone with the link, then pass its URL '
                        'as video_url or backup_video_url. Details: ' + ' / '.join(errors)
                    ) from exc
                raise RuntimeError(f'{provider} download failed: {str(exc)[-400:]}') from exc
            candidates = sorted(
                (p for p in work_dir.glob('source.*') if p.is_file() and p.stat().st_size > 0
                 and p.suffix not in ('.part', '.ytdl', '.partial')),
                key=lambda p: p.stat().st_size, reverse=True
            )
            if not candidates:
                raise RuntimeError(f'{provider} returned no playable video. Try a Google Drive MP4.')
            target = candidates[0]

    else:
        import requests
        direct_aparat = is_aparat_cdn(url)
        if direct_aparat:
            LOG.warning('Aparat CDN MP4 supplied; prefer https://www.aparat.com/v/VIDEO_ID. '
                        'Direct media URLs can expire or return HTTP 403 on GitHub.')
        else:
            LOG.info('Downloading direct video URL')
        headers = {'User-Agent': BROWSER_USER_AGENT}
        if direct_aparat:
            headers.update({'Referer': 'https://www.aparat.com/',
                            'Origin': 'https://www.aparat.com'})
        try:
            with requests.get(url, stream=True, timeout=(25, 180),
                              allow_redirects=True, headers=headers) as resp:
                resp.raise_for_status()
                mime = resp.headers.get('Content-Type', '').lower()
                if 'text/html' in mime or 'application/json' in mime:
                    raise ValueError('This URL returned a webpage/error response instead of video.')
                reported = int(resp.headers.get('content-length') or 0)
                if reported > MAX_DOWNLOAD_BYTES:
                    raise ValueError('Input exceeds 3GB safety limit.')
                total = 0
                with target.open('wb') as f:
                    for chunk in resp.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_DOWNLOAD_BYTES:
                            raise ValueError('Input exceeds 3GB safety limit.')
                        f.write(chunk)
        except requests.exceptions.HTTPError as exc:
            status = getattr(exc.response, 'status_code', None)
            if status == 403 and direct_aparat:
                raise RuntimeError(
                    'Aparat CDN returned HTTP 403 Forbidden. This is an access restriction, '
                    'not an invalid clip duration. Do NOT use caspian*.asset.aparat.com/*.mp4. '
                    'Enter the original player page URL, e.g. https://www.aparat.com/v/VIDEO_ID. '
                    'If that page also fails from GitHub, upload your permitted MP4 to '
                    'Google Drive and paste its publicly viewable share URL.'
                ) from exc
            if status == 403:
                raise RuntimeError('Direct video server returned HTTP 403. Provide an accessible '
                                   'video file URL or Google Drive share link instead.') from exc
            raise
    if not target.exists() or target.stat().st_size == 0 or target.stat().st_size > MAX_DOWNLOAD_BYTES:
        raise ValueError('Empty or oversized video (limit: 3GB).')
    LOG.info('Downloaded: %.1f MB', target.stat().st_size / 1024**2)
    return target


def video_duration(path: Path) -> float:
    raw = run_cmd(['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
                   '-of', 'default=noprint_wrappers=1:nokey=1', str(path)], quiet=True)
    duration = float(raw.strip())
    if duration <= 0:
        raise ValueError('Video has no valid duration')
    return duration


def make_analysis_proxy(source: Path, proxy: Path) -> None:
    """Low-resolution analysis copy preserves the full duration and spoken audio."""
    run_cmd(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
             '-i', str(source), '-map', '0:v:0', '-map', '0:a:0?',
             '-vf', 'scale=640:-2:force_original_aspect_ratio=decrease,fps=1',
             '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '33',
             '-c:a', 'aac', '-b:a', '64k', '-ac', '1',
             '-movflags', '+faststart', str(proxy)], quiet=True)
    LOG.info('Analysis proxy: %.1f MB', proxy.stat().st_size / 1024**2)


def editorial_prompt(duration: float, *, mode: str, language: str,
                     count: int, min_seconds: int, max_seconds: int, subject: str) -> str:
    language_name = LANGUAGES.get(language, LANGUAGES['fa'])
    base = f'''You are an expert editor for an educational YouTube channel. Analyze this ACTUAL video including its audio.
Video duration: {duration:.2f} seconds. Timecodes MUST be seconds relative to the ORIGINAL video, not approximate chapter labels.
Output language for all public-facing copy: {language_name}. Extra context / priority: {subject or 'educational, accurate, professional'}.
Create metadata that faithfully reflects the spoken content. Do NOT fabricate quotations, hadith references, verses, events, names, sources or claims not stated in the video. Avoid unsupported sensationalism and policy-violating wording.
Write a useful natural-language description (about 2-5 lines), 3 alternative non-identical clickable yet truthful titles, 4-7 hashtags each with '#', and 7-12 search tags without '#'. Provide a relevant pinned question/comment and very short 2-6 word cover_text. Keep writing clean and publishable.
We need complete thoughts and intelligible opening/closing; no mid-sentence cuts. Clip recommendations are editorial suggestions requiring human verification of boundaries.
'''
    if mode == 'precut':
        return base + '''This is ALREADY ONE EDITED CLIP. Return exactly one item in clips, start=0, end=video duration. Do NOT extract extra sections; create only the publication package.''' 
    return base + f'''This is a longer source video. Identify at most {count} non-overlapping, high-value extracts. Each extract MUST be between {min_seconds} and {max_seconds} seconds, and MUST be genuinely present in the source. Prefer a striking question, clear argument, useful educational fact, or concise response to a misconception. Avoid intros, gaps and repeated ideas. Sort clips in source chronology. If no valid segments exist, return clips=[]. Give a short why_selected reason for each segment.''' 


def gemini_plan(video_path: Path, prompt: str, primary_model: str) -> tuple[Plan, str]:
    """Gemini Files API with transient retries; delete remote file in finally."""
    from google import genai
    from google.genai import types
    key = os.environ.get('GEMINI_API_KEY', '')
    if not key:
        raise RuntimeError('Missing repository secret: GEMINI_API_KEY')
    client = genai.Client(api_key=key)
    remote = None
    try:
        remote = client.files.upload(file=str(video_path), config={'mime_type': 'video/mp4'})
        for _ in range(90):
            state = getattr(getattr(remote, 'state', None), 'name', None)
            if state == 'ACTIVE':
                break
            if state == 'FAILED':
                raise RuntimeError('Gemini file processing failed')
            LOG.info('Gemini preparing uploaded video; state=%s', state)
            time.sleep(5)
            remote = client.files.get(name=remote.name)
        else:
            raise TimeoutError('Gemini File API did not activate the video in 7.5 minutes.')

        models = list(dict.fromkeys([primary_model, 'gemini-3.5-flash-lite']))
        last_error = None
        for model in models:
            for attempt in range(1, 5):
                try:
                    LOG.info('Generating plan with %s (attempt %s/4)', model, attempt)
                    response = client.models.generate_content(
                        model=model,
                        contents=[remote, prompt],
                        config=types.GenerateContentConfig(
                            response_mime_type='application/json',
                            response_schema=Plan,
                            temperature=0.35,
                        ),
                    )
                    parsed = getattr(response, 'parsed', None)
                    plan = parsed if isinstance(parsed, Plan) else Plan.model_validate_json(response.text)
                    if not plan.clips:
                        raise ValueError('Model returned no valid clip suggestions.')
                    return plan, model
                except Exception as exc:
                    last_error = exc
                    message = str(exc).lower()
                    transient = any(t in message for t in (
                        '429', '503', '500', '502', '504', 'unavailable', 'deadline',
                        'resource_exhausted', 'high demand', 'temporarily', 'timeout'
                    ))
                    LOG.warning('Gemini attempt failed: %s', str(exc)[:300])
                    if not transient:
                        raise
                    if attempt < 4:
                        delay = min(70, 6 * 2 ** (attempt - 1))
                        time.sleep(delay)
            LOG.warning('Model %s exhausted transient retries; switching if possible.', model)
        raise RuntimeError(f'All Gemini models failed. Last error: {last_error}')
    finally:
        if remote is not None:
            try:
                client.files.delete(name=remote.name)
                LOG.info('Deleted temporary file from Gemini Files API')
            except Exception as exc:
                LOG.warning('Unable to delete Gemini temporary file: %s', exc)


def normalize_clips(items: list[Candidate], duration: float, min_seconds: int,
                    max_seconds: int, count: int, mode: str) -> tuple[list[Candidate], list[str]]:
    """Do not silently fix AI timestamps or choose arbitrary source segments."""
    if mode == 'precut' and items:
        clip = items[0].model_copy(deep=True)
        clip.start, clip.end = 0.0, round(duration, 2)
        return [clip], []
    valid: list[Candidate] = []
    rejected: list[str] = []
    for clip in items:
        start, end = float(clip.start), float(clip.end)
        length = end - start
        reason = ''
        if start < 0 or end > duration + 0.5 or end <= start:
            reason = 'out of range / reversed'
        elif length < min_seconds or length > max_seconds:
            reason = f'duration={length:.1f}s outside [{min_seconds},{max_seconds}]'
        elif any(start < old.end - 0.3 and end > old.start + 0.3 for old in valid):
            reason = 'overlaps a previously selected clip'
        elif not clip.title.strip():
            reason = 'missing title'
        if reason:
            rejected.append(f'{seconds_to_hms(start)}-{seconds_to_hms(end)} {reason}')
        else:
            valid.append(clip)
        if len(valid) >= count:
            break
    return valid, rejected


def render_video(source: Path, dest: Path, start: float, end: float) -> None:
    """720x1280 Shorts; blurred background preserves original full horizontal frame."""
    filt = ('split=2[bg][fg];'
            '[bg]scale=720:1280:force_original_aspect_ratio=increase,'
            'crop=720:1280,boxblur=20:2[back];'
            '[fg]scale=720:1280:force_original_aspect_ratio=decrease[front];'
            '[back][front]overlay=(W-w)/2:(H-h)/2,format=yuv420p')
    run_cmd(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
             '-ss', str(start), '-t', str(end - start), '-i', str(source),
             '-vf', filt, '-map', '0:v:0', '-map', '0:a:0?',
             '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '27',
             '-maxrate', '1700k', '-bufsize', '3400k',
             '-c:a', 'aac', '-b:a', '96k', '-ac', '2',
             '-movflags', '+faststart', str(dest)], quiet=True)


def _font(size: int):
    from PIL import ImageFont
    fonts = [
        '/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf',
        '/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf',
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        'C:/Windows/Fonts/tahomabd.ttf',
    ]
    for path in fonts:
        if Path(path).is_file():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


def _rtl_display(text: str) -> str:
    if not re.search(r'[\u0600-\u06ff]', text):
        return text
    import arabic_reshaper
    from bidi.algorithm import get_display
    return get_display(arabic_reshaper.reshape(text))


def create_cover(video: Path, destination: Path, title: str) -> None:
    """A draft landscape cover; Shorts thumbnail selection may need YouTube mobile app."""
    from PIL import Image, ImageDraw, ImageEnhance, ImageFilter
    frame = destination.with_suffix('.frame.jpg')
    run_cmd(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
             '-ss', '0.7', '-i', str(video), '-frames:v', '1', str(frame)], quiet=True)
    with Image.open(frame) as im:
        image = im.convert('RGB')
    frame.unlink(missing_ok=True)
    # Use a darkened, blurred full-frame background in 16:9, overlay a clear centered face/frame.
    w, h = image.size
    scale = max(1280 / w, 720 / h)
    bg = image.resize((round(w * scale), round(h * scale)), Image.Resampling.LANCZOS)
    left, top = (bg.width - 1280) // 2, (bg.height - 720) // 2
    bg = bg.crop((left, top, left + 1280, top + 720)).filter(ImageFilter.GaussianBlur(9))
    bg = ImageEnhance.Brightness(bg).enhance(0.50)
    draw = ImageDraw.Draw(bg, 'RGBA')
    draw.rounded_rectangle((58, 63, 390, 128), radius=24, fill=(244, 183, 40, 230))
    draw.text((81, 74), 'YOUTUBE CLIP', font=_font(33), fill=(20, 25, 40, 255))
    clean = (title or 'کلیپ منتخب').strip().replace('\n', ' ')
    clean = clean[:90]
    text_size = 74 if len(clean) < 40 else 57
    font = _font(text_size)
    # Simple length-based line wrapping, with shaping for Persian/Arabic.
    max_chars = 24 if text_size >= 70 else 30
    words = clean.split()
    lines, cur = [], ''
    for word in words:
        new = (cur + ' ' + word).strip()
        if len(new) > max_chars and cur:
            lines.append(cur)
            cur = word
        else:
            cur = new
    if cur:
        lines.append(cur)
    lines = lines[:3]
    y = 335 - (len(lines) - 1) * 44
    for line in lines:
        shaped = _rtl_display(line)
        bbox = draw.textbbox((0, 0), shaped, font=font, stroke_width=2)
        tw = bbox[2] - bbox[0]
        x = max(40, (1280 - tw) // 2)
        draw.text((x, y), shaped, font=font, fill='white',
                  stroke_width=3, stroke_fill=(12, 21, 39, 255))
        y += text_size + 20
    bg.save(destination, format='JPEG', quality=88, optimize=True)


def write_metadata(out_dir: Path, clip: Candidate, number: int, source_url: str) -> dict:
    filename = f'clip-{number:02d}'
    base = out_dir / 'metadata'
    base.mkdir(parents=True, exist_ok=True)
    row = clip.model_dump()
    row['source_start_hms'] = seconds_to_hms(clip.start)
    row['source_end_hms'] = seconds_to_hms(clip.end)
    row['source_video_timestamp_link'] = youtube_timestamp_link(source_url, clip.start)
    row['video_file'] = f'clips/{filename}.mp4'
    row['cover_file'] = f'covers/{filename}.jpg'
    (base / f'{filename}.json').write_text(json.dumps(row, ensure_ascii=False, indent=2), encoding='utf-8')
    alts = '\n'.join(f'  - {s}' for s in clip.alternative_titles)
    lines = [
        f'کلیپ {number} — بسته انتشار یوتیوب',
        f'برش: {row["source_start_hms"]} تا {row["source_end_hms"]}',
        f'لینک تایم‌دار منبع: {row["source_video_timestamp_link"]}', '',
        'تیتر اصلی:', clip.title, '',
        'تیترهای جایگزین:', alts, '',
        'توضیحات:', clip.description, '',
        'هشتگ‌ها:', ' '.join(clip.hashtags), '',
        'تگ‌های YouTube Studio:', ', '.join(clip.tags), '',
        'کامنت پین‌شده:', clip.pinned_comment, '',
        'متن روی کاور:', clip.cover_text, '',
        'دلیل انتخاب کلیپ:', clip.why_selected, '',
        'پیش از انتشار، محتوا، مرز برش‌ها و صحت عبارات را انسانی بررسی کنید.'
    ]
    (base / f'{filename}.txt').write_text('\n'.join(lines), encoding='utf-8')
    return row


def dashboard(out_dir: Path, items: list[dict], summary: str, rejects: list[str]) -> None:
    esc = lambda s: html.escape(str(s or ''))
    cards = []
    for idx, c in enumerate(items, start=1):
        video = f'clips/clip-{idx:02d}.mp4'
        cover = f'covers/clip-{idx:02d}.jpg'
        title_options = '\n'.join([c.get('title', '')] + c.get('alternative_titles', []))
        copytext = '\n\n'.join([c.get('title', ''), c.get('description', ''),
                                    ' '.join(c.get('hashtags', [])), c.get('pinned_comment', '')])
        cards.append(f'''
        <article class="card">
          <div class="preview"><video controls preload="metadata" src="{video}" poster="{cover}"></video></div>
          <div class="info"><div class="minor">کلیپ {idx:02d} | {esc(c['source_start_hms'])} تا {esc(c['source_end_hms'])}</div>
          <h2>{esc(c.get('title'))}</h2>
          <div class="pair"><strong>۳ تیتر پیشنهادی</strong><p>{esc(title_options)}</p></div>
          <div class="pair"><strong>توضیحات یوتیوب</strong><p>{esc(c.get('description'))}</p></div>
          <div class="pair"><strong>هشتگ‌ها</strong><p>{esc(' '.join(c.get('hashtags', [])))}</p></div>
          <div class="pair"><strong>تگ‌ها</strong><p>{esc(', '.join(c.get('tags', [])))}</p></div>
          <div class="pair"><strong>کامنت پین‌شده</strong><p>{esc(c.get('pinned_comment'))}</p></div>
          <div class="pair"><strong>دلیل انتخاب</strong><p>{esc(c.get('why_selected'))}</p></div>
          <a href="{video}" download>دریافت MP4</a> · <a href="{cover}" download>کاور JPG</a> · <a href="metadata/clip-{idx:02d}.txt" download>متن بسته انتشار</a>
          <button type="button" onclick="copyPack(this)">کپی متن انتشار</button>
          <textarea hidden class="copy">{esc(copytext)}</textarea></div>
        </article>''')
    message = '<br>'.join(esc(x) for x in rejects)
    page = f'''<!DOCTYPE html><html lang="fa" dir="rtl"><head><meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>YouTube Clip Studio | خروجی</title><style>
    :root{{--bg:#0b1323;--card:#142037;--line:#2c3b56;--text:#eef3ff;--muted:#adbed5;--accent:#f6bd45}}
    *{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--text);font:16px tahoma,Arial,sans-serif;line-height:1.85}}
    .container{{max-width:1260px;margin:0 auto;padding:34px 20px}}header{{border:1px solid var(--line);background:linear-gradient(120deg,#1d3458,#111d31);padding:38px;border-radius:20px;margin-bottom:25px}}
    h1{{font-size:32px;margin:0 0 7px}}h2{{font-size:24px;line-height:1.65;margin:4px 0 24px}}p{{margin:6px 0;white-space:pre-wrap}}
    .eyebrow,.minor{{color:var(--accent);font-weight:bold;font-size:13px}}.muted{{color:var(--muted)}}.card{{display:grid;grid-template-columns:340px 1fr;gap:26px;background:var(--card);border:1px solid var(--line);border-radius:20px;margin-bottom:20px;padding:22px}}
    .preview video{{width:100%;aspect-ratio:9 / 16;max-height:600px;object-fit:contain;background:black;border-radius:14px}}
    .pair{{border-top:1px solid var(--line);padding:10px 0}}strong{{color:#dae5f8}}a{{color:#9dd7ff;display:inline-block;margin:14px 0 0 12px;text-decoration:none}}
    button{{display:inline-block;background:var(--accent);padding:10px 18px;border:0;border-radius:9px;color:#13213a;font-weight:bold;cursor:pointer;margin-top:12px}}
    footer{{color:var(--muted);margin:25px 0;font-size:13px}}@media(max-width:800px){{.card{{grid-template-columns:1fr}}.preview video{{max-height:470px}}h1{{font-size:25px}}header{{padding:25px}}}}
    </style></head><body><main class="container"><header><div class="eyebrow">YOUTUBE CLIP STUDIO · GITHUB ACTIONS</div>
    <h1>بسته‌های آماده تدوین و انتشار</h1><p class="muted">{esc(summary)}</p><p>{len(items)} کلیپ ساخته شده است. هر ویدئو و متن را پیش از انتشار بررسی کنید.</p></header>
    {''.join(cards)}{'<p class="muted">برش‌های ردشده: ' + message + '</p>' if rejects else ''}
    <footer>این گزارش مستقل و آفلاین است. انتشار در یوتیوب خودکار انجام نمی‌شود. برای Shorts، انتخاب تصویر بندانگشتی ممکن است از داخل اپلیکیشن یوتیوب انجام شود.</footer></main>
    <script>function copyPack(btn){{let box=btn.parentElement.querySelector('.copy');let t=box.value;let done=()=>{{btn.textContent='کپی شد';setTimeout(()=>btn.textContent='کپی متن انتشار',1500)}};if(navigator.clipboard){{navigator.clipboard.writeText(t).then(done).catch(()=>fallback())}}else{{fallback()}}function fallback(){{box.hidden=false;box.select();document.execCommand('copy');box.hidden=true;done()}}}}</script></body></html>'''
    (out_dir / 'dashboard.html').write_text(page, encoding='utf-8')



def parse_number_setting(name: str, default: int) -> int:
    """Parse GitHub Actions input: seconds, mm:ss or hh:mm:ss, including Persian digits."""
    raw = os.environ.get(name, '').strip()
    if not raw:
        return default
    try:
        if name in ('STUDIO_MIN_SECONDS', 'STUDIO_MAX_SECONDS') and ':' in raw:
            parts = raw.split(':')
            if len(parts) not in (2, 3) or any(not x.isdecimal() for x in parts):
                raise ValueError()
            values = [int(x) for x in parts]
            if any(v >= 60 for v in values[1:]):
                raise ValueError()
            seconds = 0
            for value in values:
                seconds = seconds * 60 + value
            return seconds
        return int(raw)
    except ValueError as exc:
        raise ValueError(
            f'{name}={raw!r} is invalid. Enter a whole number in seconds such as 25 or 90, '
            'or a duration such as 1:30.'
        ) from exc


def load_clip_settings(mode: str) -> tuple[int, int, int]:
    """Validate automatic settings; return (count, min_seconds, max_seconds)."""
    if mode == 'precut':
        LOG.info('precut mode: processing whole video; count/min/max are ignored')
        return 1, 25, 65
    count = parse_number_setting('STUDIO_COUNT', 4)
    min_seconds = parse_number_setting('STUDIO_MIN_SECONDS', 25)
    max_seconds = parse_number_setting('STUDIO_MAX_SECONDS', 65)
    LOG.info('Clip settings: count=%s, min_seconds=%s, max_seconds=%s',
             count, min_seconds, max_seconds)
    if not 1 <= count <= 12:
        raise ValueError(f'STUDIO_COUNT={count} is outside the supported range 1–12.')
    if min_seconds > max_seconds:
        LOG.warning('Minimum length %ss exceeds maximum %ss; swapping automatically.',
                    min_seconds, max_seconds)
        min_seconds, max_seconds = max_seconds, min_seconds
    if not 5 <= min_seconds <= max_seconds <= 900:
        raise ValueError(
            f'Invalid duration interval: min_seconds={min_seconds}, max_seconds={max_seconds}. '
            'Use 5–900 seconds (or mm:ss), e.g. min_seconds=25 max_seconds=65. '
            'Clips over 180 seconds may not qualify as Shorts.'
        )
    return count, min_seconds, max_seconds


def main() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
    url = os.environ.get('VIDEO_URL', '').strip()
    mode = os.environ.get('STUDIO_MODE', 'automatic')
    language = os.environ.get('STUDIO_LANGUAGE', 'fa')
    model = os.environ.get('STUDIO_MODEL', 'gemini-3.8-flash')
    subject = os.environ.get('STUDIO_SUBJECT', '')
    count, min_seconds, max_seconds = load_clip_settings(mode)
    out_dir = Path(os.environ.get('OUTPUT_DIR', 'output')).resolve()
    work_dir = Path('work').resolve()
    if mode not in ('automatic', 'precut') or language not in LANGUAGES:
        raise ValueError('Unknown mode or language')
    if not url:
        raise ValueError('VIDEO_URL input is empty')
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'clips').mkdir(exist_ok=True)
    (out_dir / 'covers').mkdir(exist_ok=True)
    (out_dir / 'metadata').mkdir(exist_ok=True)
    (out_dir / 'README-OUTPUT.txt').write_text(
        '1) dashboard.html را باز کنید.\n2) پوشه clips و metadata را بررسی کنید.\n'
        '3) درستی گفته‌ها، زمان آغاز و پایان، مسائل حق نشر و قوانین یوتیوب را پیش از انتشار کنترل کنید.\n'
        '4) این سامانه ویدئو را در یوتیوب منتشر نمی‌کند.\n', encoding='utf-8')
    try:
        backup_url = os.environ.get('BACKUP_VIDEO_URL', '').strip()
        try:
            source = fetch_video(url, work_dir)
        except Exception as primary_error:
            if not backup_url:
                raise
            LOG.warning('Primary video source failed: %s', str(primary_error)[:400])
            LOG.info('Trying backup_video_url (e.g. Google Drive authorized MP4)')
            try:
                source = fetch_video(backup_url, work_dir)
            except Exception as backup_error:
                raise RuntimeError(
                    f'Both primary video_url and backup_video_url failed. '
                    f'Primary: {str(primary_error)[:220]}; Backup: {str(backup_error)[:220]}'
                ) from backup_error
    except Exception as exc:
        (out_dir / 'download-error.txt').write_text(
            'YouTube Clip Studio - download failed\n'
            f'{type(exc).__name__}: {exc}\n\n'
            'For Aparat: use the VIDEO PAGE link https://www.aparat.com/v/VIDEO_ID, '
            'not a caspian*.asset.aparat.com MP4 URL.\n'
            'If Aparat blocks GitHub, upload the authorized original MP4 '
            'to Google Drive and use its share URL.\n',
            encoding='utf-8')
        raise
    duration = video_duration(source)
    LOG.info('Source duration: %.2f seconds', duration)
    if mode == 'automatic' and duration < min_seconds:
        raise ValueError('Input is shorter than min_seconds. Select precut mode instead.')
    proxy = work_dir / 'analysis.mp4'
    make_analysis_proxy(source, proxy)
    prompt = editorial_prompt(duration, mode=mode, language=language, count=count,
                              min_seconds=min_seconds, max_seconds=max_seconds, subject=subject)
    plan, actual_model = gemini_plan(proxy, prompt, model)
    clips, rejects = normalize_clips(plan.clips, duration, min_seconds, max_seconds, count, mode)
    if not clips:
        (out_dir / 'rejected.json').write_text(json.dumps(rejects, ensure_ascii=False, indent=2), encoding='utf-8')
        raise RuntimeError('No valid segments after timestamp/duration validation. See rejected.json.')
    rows = []
    for number, clip in enumerate(clips, start=1):
        vid_path = out_dir / 'clips' / f'clip-{number:02d}.mp4'
        cover_path = out_dir / 'covers' / f'clip-{number:02d}.jpg'
        LOG.info('Rendering clip %d: %.2f-%.2f', number, clip.start, clip.end)
        render_video(source, vid_path, clip.start, clip.end)
        try:
            create_cover(vid_path, cover_path, clip.cover_text or clip.title)
        except Exception as exc:
            LOG.warning('Stylized cover failed; trying plain video frame: %s', exc)
            try:
                run_cmd(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y',
                         '-ss', '0.5', '-i', str(vid_path), '-frames:v', '1',
                         str(cover_path)], quiet=True)
            except Exception as cover_exc:
                LOG.warning('No cover available: %s', cover_exc)
        rows.append(write_metadata(out_dir, clip, number, url))
    with (out_dir / 'cut-list.csv').open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(['number', 'start_seconds', 'end_seconds', 'start_hms', 'end_hms', 'title', 'reason'])
        for n, clip in enumerate(clips, start=1):
            writer.writerow([n, clip.start, clip.end, seconds_to_hms(clip.start),
                             seconds_to_hms(clip.end), clip.title, clip.why_selected])
    dashboard(out_dir, rows, plan.video_summary, rejects)
    manifest = {'source_url': url, 'duration_seconds': duration, 'model': actual_model,
                'language': language, 'mode': mode, 'summary': plan.video_summary,
                'clips': rows, 'rejected': rejects}
    (out_dir / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(os.environ['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as f:
            f.write(f'### YouTube Clip Studio\n\nCreated **{len(rows)}** clip(s). '
                    f'Output includes `dashboard.html`, MP4 clips and metadata. '
                    f'Download the **youtube-studio** artifact below.\n')
    LOG.info('Done: %d clips. Read output/dashboard.html', len(rows))
    # Keep source and proxy off artifacts; no large persistent storage on GitHub.
    shutil.rmtree(work_dir, ignore_errors=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        logging.exception('Studio failed: %s', error)
        message = str(error).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')
        print(f'::error title=YouTube Clip Studio::{message}', flush=True)
        sys.exit(1)
