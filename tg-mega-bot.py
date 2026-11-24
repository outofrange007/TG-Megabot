import asyncio
import base64
import hashlib
import http.client
import json
import os
import random
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from urllib.parse import (
    parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit,
)
from contextlib import aclosing, closing, suppress
from pathlib import Path

from PIL import Image, ImageOps
from telethon import TelegramClient, events, Button
from telethon.errors import (
    FloodWaitError,
    ImageProcessFailedError,
    MediaInvalidError,
    PhotoInvalidDimensionsError,
    PhotoSaveFileInvalidError,
    RpcCallFailError,
    ServerError,
    TimedOutError,
    TopicDeletedError,
)

# Forum topic creation moved from the channels namespace (Telethon
# <= 1.41, keyword "channel") to the messages namespace (Telethon
# >= 1.42, keyword "peer"). Support both so an older pinned Telethon
# does not crash the bot at import time.
try:
    from telethon.tl.functions.messages import CreateForumTopicRequest
    CREATE_TOPIC_PEER_KWARG = "peer"
except ImportError:
    from telethon.tl.functions.channels import CreateForumTopicRequest
    CREATE_TOPIC_PEER_KWARG = "channel"

# Deleting a forum topic is done with messages.deleteTopicHistory, where
# top_msg_id is the topic ID (the message ID of the topic's root service
# message). Some Telethon generations expose the same request in the
# channels namespace instead; degrade gracefully if it is missing entirely.
try:
    from telethon.tl.functions.messages import DeleteTopicHistoryRequest
    DELETE_TOPIC_VARIANT = "messages"
except ImportError:
    try:
        from telethon.tl.functions.channels import DeleteTopicHistoryRequest
        DELETE_TOPIC_VARIANT = "channels"
    except ImportError:
        DeleteTopicHistoryRequest = None
        DELETE_TOPIC_VARIANT = None

try:
    # Comes with telethon; used for MEGA's AES node/attribute decryption.
    import pyaes
except ImportError:
    raise SystemExit("pyaes is required. Install it with: pip install pyaes")

try:
    # OpenSSL-backed AES; ~150x faster than pyaes for file decryption.
    from cryptography.hazmat.primitives.ciphers import (
        Cipher,
        algorithms,
        modes,
    )
    HAVE_FAST_CRYPTO = True
except ImportError:
    HAVE_FAST_CRYPTO = False

try:
    # Bunkr support: plain HTTP scraping. Optional at import time so a
    # missing package only disables /bunkr instead of the whole bot.
    import requests
    from bs4 import BeautifulSoup, FeatureNotFound
    HAVE_BUNKR_DEPS = True
except ImportError:
    requests = None
    BeautifulSoup = None
    FeatureNotFound = Exception
    HAVE_BUNKR_DEPS = False

# ---- Configuration ----
API_ID = int(os.environ["TG_API_ID"])
API_HASH = os.environ["TG_API_HASH"]
BOT_TOKEN = os.environ["TG_BOT_TOKEN"]
CHAT_ID = int(os.environ["TG_CHAT_ID"])  # For example: -1001234567890

ALLOWED_USERS = {
    int(value)
    for value in os.environ.get("TG_ALLOWED_USERS", "").split(",")
    if value.strip()
}

WORK_DIR = os.environ.get("WORK_DIR") or None

# Delay before temporary acknowledgement and success messages are deleted.
# Set to 0 to delete them immediately.
STATUS_DELETE_DELAY = int(os.environ.get("STATUS_DELETE_DELAY", "60"))

# Seconds to wait for a large-file handling choice; 0 disables the prompt.
LARGE_FILE_ASK_TIMEOUT = int(os.environ.get("LARGE_FILE_ASK_TIMEOUT", "120"))

# Use /data in Docker when it exists; otherwise use the current directory.
DATA_DIR = Path(
    os.environ.get("DATA_DIR", "/data" if os.path.isdir("/data") else ".")
)
DB_PATH = str(DATA_DIR / "queue.sqlite3")
# Telethon appends ".session"; the default keeps existing deployments
# (DATA_DIR/bot_session.session) logged in. Override with SESSION_NAME.
SESSION_NAME = os.environ.get("SESSION_NAME", "bot_session").strip() or "bot_session"
SESSION_PATH = str(DATA_DIR / SESSION_NAME)
KEPT_DIR = DATA_DIR / "kept"

# Telegram upload limit safety threshold: files above this are split
# (or, with the large-file prompt, can be kept on the server instead).
PART_SIZE = 1900 * 1024 * 1024
COPY_BUF = 8 * 1024 * 1024


def env_mib(name: str, default: float) -> int:
    """Read an env var given in MiB and return it as bytes."""
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = default
    return int(value * 1024 * 1024)


def env_float(name: str, default: float) -> float:
    """Read a float env var, falling back to the default on bad input."""
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# Largest YouTube / Netzkino download accepted per video.
YT_MAX_SIZE = env_mib("YT_MAX_SIZE", 1900)
NZ_MAX_SIZE = env_mib("NZ_MAX_SIZE", 1900)

# Optional cookies.txt (Netscape format) for authenticated YouTube
# downloads such as age-restricted or members-only videos.
YT_COOKIES = os.environ.get("YT_COOKIES", "")

# Seconds between megadl progress lines in the log.
PROGRESS_INTERVAL = 10

# Seconds between "still downloading" heartbeats for long downloads.
HEARTBEAT_INTERVAL = 30

# Seconds per API request and number of attempts for MEGA API calls.
MEGA_API_TIMEOUT = 60
MEGA_API_RETRIES = 3

# Download attempts per file and pause when MEGA's bandwidth limit hits.
MEGA_DOWNLOAD_RETRIES = 3
BANDWIDTH_RETRY_DELAY = 60

# Anti-flood tuning for large folder shares: pause between two file
# downloads, how many consecutive failures trigger a cooldown pause,
# and how long that pause lasts. All three can be set via environment.
MEGA_FILE_DELAY = env_float("MEGA_FILE_DELAY", 0.25)
MEGA_FAIL_STREAK = int(env_float("MEGA_FAIL_STREAK", 10))
MEGA_COOLDOWN = env_float("MEGA_COOLDOWN", 300)

# When a folder job aborts on API error -16, it is re-queued this
# many times automatically, each run doubling the per-file pacing.
MEGA_AUTO_RETRIES = int(env_float("MEGA_AUTO_RETRIES", 3))

# When a folder job pauses on HTTP 509 (MEGA bandwidth limit), it is
# re-queued to start again after this many seconds; every further 509
# pause doubles the wait. The pause exists so the quota can reset
# instead of the bot burning failed retries file by file.
BANDWIDTH_REQUEUE_DELAY = env_float("BANDWIDTH_REQUEUE_DELAY", 900)

# Bytes fetched from the storage server per read (also the decrypt unit).
DOWNLOAD_CHUNK = 1024 * 1024

# Resumable MEGA folder downloads. An interrupted file (cancel, network
# error, HTTP 509, shutdown) keeps its partial data in MEGA_RESUME_DIR,
# keyed by the file's node handle, and the next attempt for the same
# file (in-job retry, automatic re-queue, /retry, container restart)
# continues from there with a byte-range request. MEGA_RESUME=0
# restores the old behaviour (partial data is always deleted). Partial
# files untouched for MEGA_RESUME_MAX_AGE_HOURS are pruned.
MEGA_RESUME = env_float("MEGA_RESUME", 1) != 0
MEGA_RESUME_DIR = Path(
    os.environ.get("MEGA_RESUME_DIR")
    or (Path(WORK_DIR) if WORK_DIR else DATA_DIR) / "mega-resume"
)
MEGA_RESUME_MAX_AGE = env_float("MEGA_RESUME_MAX_AGE_HOURS", 72) * 3600
# Bytes between two persisted resume checkpoints (data + MAC state).
MEGA_RESUME_CHECKPOINT = 64 * 1024 * 1024
# Pause before an in-job retry after a transient network error.
MEGA_NETWORK_RETRY_DELAY = 10

# Free disk space required before each MEGA download: the file needs
# room twice (the download itself plus the copy written by the
# image/video preparation), plus this reserve so the bot never fills
# the volume completely. Bytes already present in a resumable partial
# download are subtracted. Without enough room the whole job stops
# (instead of every remaining file failing one by one); /retry later.
MIN_FREE_SPACE = env_mib("MIN_FREE_SPACE_MIB", 512)
DISK_SPACE_FACTOR = 2

# Telegram uploads. Telethon sleeps through short flood waits itself
# (up to its flood_sleep_threshold of 60s) and raises FloodWaitError
# for longer ones; send_file() then waits the requested time (plus a
# small margin) and tries again. Waits longer than
# UPLOAD_FLOOD_WAIT_MAX seconds fail the upload instead (0 = no cap).
UPLOAD_FLOOD_WAIT_MAX = env_float("UPLOAD_FLOOD_WAIT_MAX", 3600)
UPLOAD_FLOOD_WAIT_MARGIN = 2
# Flood waits honoured per single send before giving up.
UPLOAD_FLOOD_RETRIES = 5
# Attempts per upload on transient network / Telegram server errors and
# the pause between them. Other errors (file too big, bad request) are
# never retried.
UPLOAD_RETRIES = max(1, int(env_float("UPLOAD_RETRIES", 3)))
UPLOAD_RETRY_DELAY = env_float("UPLOAD_RETRY_DELAY", 10)

# Match new and legacy MEGA file/folder links. New-style links may end
# with a subfolder or file path inside the share, e.g.
# https://mega.nz/folder/<handle>#<key>/folder/<subhandle> — such links
# make the bot download only that subfolder (or single file).
MEGA_REGEX = re.compile(
    r"https?://mega\.(?:nz|io)/"
    r"(?:(?:file|folder)/[\w-]+#[\w-]+(?:/(?:folder|file)/[\w-]+)*"
    r"|#![\w-]+![\w-]+)"
)

# A new-style link may select a subfolder or a single file inside the
# share, e.g. .../folder/<handle>#<key>/folder/<sub handle>.
MEGA_SUBPATH_REGEX = re.compile(r"/(?:folder|file)/([\w-]+)/?$")

# Match YouTube video/shorts/live URLs (youtu.be included).
YT_REGEX = re.compile(
    r"https?://(?:(?:www|m|music)\.)?(?:youtube\.com|youtu\.be)/\S+",
    re.IGNORECASE,
)

# Match Netzkino movie pages.
NETZKINO_REGEX = re.compile(
    r"https?://(?:www\.)?netzkino\.de/\S+",
    re.IGNORECASE,
)

# Match an optional "-t <topic id>" argument.
TOPIC_REGEX = re.compile(r"(?:^|\s)-t\s+(\d+)", re.IGNORECASE)

# Match an optional "-newtopic <name>" argument. The name may contain
# spaces and ends at the next "-t" flag, a URL, or the end of the text.
NEWTOPIC_REGEX = re.compile(
    r"(?:^|\s)-newtopic\s+(.+?)(?=\s+-t\s|\s*https?://|\s*$)",
    re.IGNORECASE,
)

# Match an optional "-dupes" flag: re-transfer files the duplicate
# registry already knows, instead of skipping them.
DUPES_REGEX = re.compile(r"(?:^|\s)-dupes(?:\s|$)", re.IGNORECASE)

# Match an optional "-recode" flag: prepare videos for Telegram (720p
# H.264/AAC, remux when they already fit). Without it, videos are
# uploaded exactly as downloaded.
RECODE_REGEX = re.compile(r"(?:^|\s)-recode(?:\s|$)", re.IGNORECASE)


def pop_flag(regex: re.Pattern, text: str) -> tuple[bool, str]:
    """Return (flag_present, text_without_flag)."""
    if regex.search(text):
        return True, regex.sub(" ", text)
    return False, text

# ---- Bunkr configuration ----
# Known Bunkr site domains. Album pages live at /a/<id>, file pages at
# /f/<slug>, /v/<slug> (video) and /i/<slug> (image).
BUNKR_DOMAINS = {
    "bunkr.si", "bunkr.su", "bunkr.ru", "bunkr.black",
    "bunkr.media", "bunkr.is", "bunkr.to", "bunkr.la",
    "bunkr.fi", "bunkr.pk", "bunkr.cr", "bunkr.ph", "bunkr.ws",
    "bunkr.ac", "bunkr.ax", "bunkr.red", "bunkr.site", "bunkr.sk",
    "bunkr.ps", "bunkrr.su", "bunkrr.ru",
}
# Bunkr moves to new TLDs often; any bunkr.<tld> / bunkrr.<tld> host is
# accepted as well, so a new mirror works without a code change.
BUNKR_SITE_REGEX = re.compile(r"(?:^|\.)(bunkrr?\.[a-z]{2,12})$")
BUNKR_FILE_PATH_PREFIXES = ("/f/", "/v/", "/i/")
BUNKR_ALBUM_PATH_PREFIX = "/a/"

# Candidate URLs in a message; the host is checked against BUNKR_DOMAINS
# afterwards, so this pattern can stay generic.
BUNKR_URL_REGEX = re.compile(
    r"https?://[\w.-]+/(?:a|f|v|i)/[^\s<>\"']+", re.IGNORECASE
)

# First host label of Bunkr CDN servers, e.g. cdn12, i-cdn, media-files3,
# burger, pizza, taquito, milkshake, mozzarella, nachos.
BUNKR_CDN_LABEL_REGEX = re.compile(
    r"^(?:i-)?(?:cdn|media-files|burger|pizza|taquito|milkshake|"
    r"mozzarella|nachos)\d*$",
    re.IGNORECASE,
)

BUNKR_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
# Back-off before retry 1, 2 and 3 after HTTP 429, 5xx or a connection error.
BUNKR_RETRY_DELAYS = (10, 30, 60)
# (connect, read) timeouts in seconds for Bunkr page and CDN requests.
BUNKR_TIMEOUT = (15, 60)
# Streaming download chunk size.
BUNKR_CHUNK = 8192
# Pause between two files of an album, to stay below Bunkr's rate limit.
BUNKR_FILE_DELAY = env_float("BUNKR_FILE_DELAY", 1.0)

# Queued jobs are prefixed with their source so the worker can pick the
# right downloader: "yt:<url>" for YouTube, "nk:<url>" for Netzkino,
# "bk:<url>" for Bunkr albums/files, "cm:<url>" for Coomer profiles/posts
# and unprefixed MEGA links (the original format).

# ---- Coomer configuration ----
# Known Coomer site domains. Profile pages live at /<service>/user/<name>,
# single posts at /<service>/user/<name>/<post_id> (e.g. /onlyfans/user/
# katjakrasavice). Coomer moves to new TLDs often, so any coomer.<tld>
# host is accepted and a new mirror works without a code change.
COOMER_DOMAINS = {'coomer.st', 'coomer.su', 'coomer.pet'}
COOMER_DOMAIN_REGEX = re.compile(r'(?:^|\.)coomer\.[a-z]{2,12}$')
# Candidate URLs in a message; the host is checked afterwards. The
# optional query block is captured so a profile link like
# .../user/<name>?o=50 keeps its offset in the queued "cm:<url>" job.
COOMER_URL_REGEX = re.compile(
    r'https?://[\w.-]+/[a-z]+/user/[\w.-]+(?:/\d+)?(?:\?[^\s<>"\')]*)?',
    re.IGNORECASE,
)
COOMER_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'
)
# Back-off before retry 1, 2 and 3 after HTTP 429, 5xx or a connection error.
COOMER_RETRY_DELAYS = (10, 30, 60)
# (connect, read) timeouts in seconds for Coomer API and CDN requests.
COOMER_TIMEOUT = (15, 60)
# Streaming download chunk size.
COOMER_CHUNK = 8192
# Pause between two files of a profile, to stay below rate limits.
COOMER_FILE_DELAY = env_float('COOMER_FILE_DELAY', 1.0)
# Safety cap for how many posts of a profile are scanned (0 = all).
COOMER_MAX_POSTS = int(env_float('COOMER_MAX_POSTS', 0))
# Page size of the posts endpoint; the server caps it at 50 per page.
COOMER_PAGE_SIZE = 50
# Connect timeout in seconds for CDN file requests: Coomer's CDN node
# net (91.149.227.0/24) sometimes has no route at all, and then a short
# timeout keeps a dead download from stalling for the read timeout.
COOMER_CONNECT_TIMEOUT = env_float('COOMER_CONNECT_TIMEOUT', 10.0)
# Consecutive CDN connection failures that abort a profile job early
# instead of grinding through every file of the profile.
COOMER_CDN_FAIL_LIMIT = int(env_float('COOMER_CDN_FAIL_LIMIT', 3))
# When the CDN node net has no route from this host, the same bytes are
# served through the always-reachable origin as img.<site>/thumbnail/
# data/<path> (downscaled image, poster for videos). 0 disables it.
COOMER_THUMBNAIL_FALLBACK = (
    os.environ.get('COOMER_THUMBNAIL_FALLBACK', '1').lower()
    not in ('0', 'false', 'no', '')
)
# File suffixes treated as video, so a thumbnail poster is renamed to
# .jpg and never mistaken for a video by the upload pipeline.
COOMER_VIDEO_EXTS = {
    '.mp4', '.m4v', '.mkv', '.webm', '.mov', '.avi', '.wmv', '.flv',
    '.mpg', '.mpeg', '.3gp', '.ts',
}


class CoomerError(RuntimeError):
    """A Coomer profile page or file could not be fetched or parsed."""


def _coomer_host(url: str) -> str:
    """Return the lowercased host of a Coomer URL."""
    return urlsplit(url).hostname.lower() if urlsplit(url).hostname else ''


def is_coomer_url(url: str) -> bool:
    """Return True when the URL's host is a Coomer site domain."""
    host = _coomer_host(url)
    return host in COOMER_DOMAINS or bool(COOMER_DOMAIN_REGEX.search(host))


def coomer_links_in(text: str) -> list[str]:
    """Return the Coomer profile/post URLs contained in a message text."""
    urls = []
    for url in clean_urls(COOMER_URL_REGEX.findall(text)):
        if is_coomer_url(url) and parse_coomer_link(url) is not None:
            urls.append(url)
    return urls


def parse_coomer_link(url: str):
    """Split a Coomer URL into (kind, service, user, post_id).

    kind is 'profile' for /<service>/user/<name> links and 'post' for
    /<service>/user/<name>/<post_id> links. Returns None for anything
    that is not a profile or post page (index, search, static files).
    """
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https'):
        return None
    segments = [s for s in parts.path.split('/') if s]
    if len(segments) >= 3 and segments[1].lower() == 'user':
        service = segments[0].lower()
        user = segments[2]
        if not re.match(r'^[a-z][\w-]*$', service):
            return None
        if len(segments) == 3:
            return ('profile', service, user, None)
        if len(segments) == 4 and segments[3].isdigit():
            return ('post', service, user, segments[3])
    return None


def coomer_session():
    """Build a requests session that passes Coomer's bot checks.

    Coomer (like Kemono) rejects JSON requests from plain clients with
    HTTP 403 unless they send an Accept header a browser would not use
    for JSON, so every API request goes out with 'Accept: text/css'.
    """
    if not HAVE_BUNKR_DEPS:
        raise CoomerError(
            'Coomer support needs the requests package: pip install requests'
        )
    session = requests.Session()
    session.headers.update({
        'User-Agent': COOMER_USER_AGENT,
        'Accept': 'text/css',
    })
    return session


def _coomer_sleep(seconds: float) -> None:
    """Sleep in 0.5 s slices so a cancel takes effect quickly."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if RUNNING['cancel']:
            raise JobCancelled()
        time.sleep(0.5)


def coomer_request(session, url: str, params=None, what: str = 'page'):
    """GET a Coomer URL, retrying on HTTP 429, 5xx and connection errors.

    Returns the response even on HTTP 400: the posts endpoint signals
    'no more pages' with 400, which the caller treats as the end.
    Raises CoomerError for other HTTP errors or after all retries.
    """
    attempts = len(COOMER_RETRY_DELAYS) + 1
    for attempt in range(attempts):
        try:
            response = session.get(
                url,
                params=params,
                timeout=COOMER_TIMEOUT,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            reason = str(exc) or type(exc).__name__
            if attempt == attempts - 1:
                raise CoomerError(
                    f'Coomer {what} {url} failed after {attempts} attempts: '
                    f'{reason}'
                ) from exc
            delay = COOMER_RETRY_DELAYS[attempt]
            log(f'[coomer] {what} {url}: {reason} - retry in {delay}s')
            _coomer_sleep(delay)
            continue

        status = response.status_code
        if status in (200, 400):
            return response
        reason = response.reason or ''
        if status in (403, 404):
            raise CoomerError(
                f'HTTP {status} for Coomer {what} {url}: {reason}'
            )
        if attempt == attempts - 1:
            raise CoomerError(f'HTTP {status} for Coomer {what} {url}: {reason}')
        delay = COOMER_RETRY_DELAYS[attempt]
        log(f'[coomer] {what} {url}: HTTP {status} - retry in {delay}s')
        _coomer_sleep(delay)
    raise CoomerError(f'Giving up on Coomer {what} {url}')


def _coomer_api_base(url: str) -> str:
    """Return the https base (scheme + host) of a Coomer site URL."""
    parts = urlsplit(url)
    return f'{parts.scheme or "https"}://{parts.netloc}'


def _coomer_start_offset(link: str) -> int:
    """Read the `?o=<n>` start offset of a Coomer profile link.

    Returns the integer value of the first `o` query parameter so a
    profile job can start at that posts-endpoint offset. Missing, not a
    number or negative values fall back to 0, keeping the default of
    scanning from the first page.
    """
    for key, value in parse_qsl(urlsplit(link).query):
        if key == 'o':
            try:
                offset = int(value)
            except (TypeError, ValueError):
                return 0
            return offset if offset > 0 else 0
    return 0


def parse_coomer_media(items: list, seen: set[str]) -> list[tuple[str, str]]:
    """Extract (filename, path) media pairs from post items.

    Coomer posts carry a main `file` (may be empty) and a list of
    `attachments`; both use {name, path} shapes where path is the CDN
    location under /data. Paths already in `seen` are skipped, so the
    same file referenced by several posts is only fetched once.
    """
    media = []
    for item in items:
        entries = []
        main = item.get('file')
        if isinstance(main, dict) and main.get('path'):
            entries.append(main)
        for att in item.get('attachments') or []:
            if isinstance(att, dict) and att.get('path'):
                entries.append(att)
        for entry in entries:
            path = str(entry['path'])
            if path.startswith('/'):
                path = path.lstrip('/')
            key = path
            if key in seen:
                continue
            seen.add(key)
            name = str(entry.get('name') or '').strip()
            media.append((name, path))
    return media


def coomer_filename(name: str, path: str, fallback: str = 'coomer_file') -> str:
    """Return a safe file name, preferring the Coomer display name."""
    candidate = name or path.rsplit('/', 1)[-1]
    candidate = re.sub(r'[\\/\x00-\x1f]', '', candidate).strip()
    candidate = re.sub(r'[^\w.\-]+', '_', candidate).strip('._-')
    if not candidate:
        candidate = fallback
    if '.' not in candidate and '.' in path.rsplit('/', 1)[-1]:
        candidate += '.' + path.rsplit('.', 1)[-1].lower()
    return candidate[:240]


def _is_coomer_connection_error(exc: Exception) -> bool:
    """Return True for CoomerError messages that signal a dead CDN node.

    "Network is unreachable" and friends mean the host has no route to
    the CDN node net at all; the caller counts them and aborts a job
    after COOMER_CDN_FAIL_LIMIT in a row instead of retrying file by
    file for the whole profile.
    """
    text = str(exc).lower()
    return any(marker in text for marker in (
        'unreachable', 'failed to establish', 'max retries exceeded',
        'connection refused', 'connection aborted', 'connection reset',
        'timed out',
    ))


def fetch_coomer_media(
    api_base: str,
    service: str,
    user: str,
    session,
    post_id: str | None = None,
    start_offset: int = 0,
) -> list[tuple[str, str]]:
    """Return the (filename, path) media of a profile or single post.

    Profile links are paginated through the posts endpoint (50 posts
    per page, signalled as HTTP 400 once past the last page) up to
    COOMER_MAX_POSTS posts. `start_offset` (from a `?o=<n>` profile-link
    query) sets the first offset scanned; paging then continues from
    there. Post links fetch that one post only.
    """
    if post_id is not None:
        post_url = f'{api_base}/api/v1/{service}/user/{user}/post/{post_id}'
        response = coomer_request(session, post_url, what='post')
        if response.status_code != 200:
            raise CoomerError(f'Coomer post {post_id} not found at {post_url}')
        payload = response.json()
        posts = [payload['post']] if isinstance(payload, dict) and 'post' in payload else []
        return parse_coomer_media(posts, set())

    posts_url = f'{api_base}/api/v1/{service}/user/{user}/posts'
    seen: set[str] = set()
    media: list[tuple[str, str]] = []
    offset = start_offset if start_offset > 0 else 0
    scanned = 0
    while True:
        response = coomer_request(
            session,
            posts_url,
            params={'o': offset},
            what='posts page',
        )
        if response.status_code != 200:
            break
        posts = response.json()
        if not isinstance(posts, list) or not posts:
            break
        media.extend(parse_coomer_media(posts, seen))
        scanned += len(posts)
        offset += len(posts)
        if COOMER_MAX_POSTS and scanned >= COOMER_MAX_POSTS:
            log(f'[coomer] COOMER_MAX_POSTS={COOMER_MAX_POSTS} reached')
            break
        if len(posts) < COOMER_PAGE_SIZE:
            break
    return media


def _coomer_thumb_url(cdn_url: str) -> str | None:
    """Build the origin thumbnail URL (img.<site>/thumbnail/data/<path>).

    Returns None when the URL is not a /data/<path> download on a
    coomer.<tld> host, so the caller can simply skip the fallback.
    """
    host = (urlsplit(cdn_url).hostname or '').lower()
    base = re.search(r'(coomer\.[a-z0-9.-]+)$', host)
    tail = re.search(r'/data/([^?#]+)$', cdn_url)
    if not base or not tail:
        return None
    return f'https://img.{base.group(1)}/thumbnail/data/{tail.group(1)}'


def _final_coomer_dest(dest: Path, content_type: str, used_thumb: bool) -> Path:
    """Return the destination path, fixing a poster's extension.

    When the origin thumbnail answered with an image but the original
    file was a video, the bytes are a poster frame and are stored as a
    .jpg so the preparation pipeline never runs ffprobe on an image.
    """
    p = Path(dest)
    if used_thumb and content_type.lower().startswith('image/') and p.suffix.lower() in COOMER_VIDEO_EXTS:
        return p.with_suffix('.jpg')
    return p


def download_coomer_file(
    cdn_url: str,
    dest_path: str,
    progress_cb=None,
    referer: str = '',
    session=None,
    state=None,
):
    """Stream a Coomer /data file to dest_path and return the final Path.

    coomer.st answers with a 302 to a CDN node (n3.coomer.st). That node
    net is not routable from every server; when the connection fails the
    identical bytes are fetched through the always-reachable origin as
    img.<site>/thumbnail/data/<path> (a downscaled image, and a poster
    frame for videos). Via the shared `state` dict the job then keeps
    using the thumbnail route so no further file wastes the connect
    timeout, and a video poster is stored as a .jpg. HTTP 404/403 and
    incomplete transfers raise CoomerError so the caller moves on.
    """
    own_session = session is None
    session = session or coomer_session()
    thumb_url = _coomer_thumb_url(cdn_url)
    thumb_only = bool(state and state.get('thumb_only'))
    headers = {}
    if referer:
        headers['Referer'] = referer

    def _open(url: str):
        try:
            return session.get(
                url,
                headers=headers,
                timeout=(COOMER_CONNECT_TIMEOUT, COOMER_TIMEOUT[1]),
                stream=True,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise CoomerError(
                f'Coomer file {url}: {str(exc) or type(exc).__name__}'
            ) from exc

    written = 0
    total = 0
    try:
        if thumb_only and thumb_url:
            url = thumb_url
            used_thumb = True
            response = _open(url)
        else:
            try:
                url = cdn_url
                used_thumb = False
                response = _open(url)
            except CoomerError as exc:
                if not (thumb_url and COOMER_THUMBNAIL_FALLBACK
                        and _is_coomer_connection_error(exc)):
                    raise
                log(
                    '[coomer] CDN unreachable, using the origin thumbnail '
                    f'route for this and later files ({cdn_url})'
                )
                url = thumb_url
                used_thumb = True
                response = _open(url)

        if state is not None and used_thumb:
            state['thumb_only'] = True

        try:
            if response.status_code != 200:
                raise CoomerError(
                    f'HTTP {response.status_code} for Coomer file {url}'
                )
            total = int(response.headers.get('Content-Length') or 0)
            content_type = response.headers.get('Content-Type') or ''
            dest = _final_coomer_dest(Path(dest_path), content_type, used_thumb)
            part = dest.with_name(dest.name + '.part')
            try:
                with open(part, 'wb') as handle:
                    for chunk in response.iter_content(COOMER_CHUNK):
                        if RUNNING['cancel']:
                            raise JobCancelled()
                        if not chunk:
                            continue
                        written += len(chunk)
                        handle.write(chunk)
                        if progress_cb is not None:
                            progress_cb(written, total)
            except BaseException:
                part.unlink(missing_ok=True)
                raise
            if written == 0:
                part.unlink(missing_ok=True)
                raise CoomerError(f'Coomer sent an empty file for {url}')
            if total and written != total:
                part.unlink(missing_ok=True)
                raise CoomerError(
                    f'Incomplete Coomer download: {written} of {total} bytes'
                )
            part.replace(dest)
        finally:
            response.close()
        return dest
    except BaseException:
        Path(dest_path).with_name(Path(dest_path).name + '.part').unlink(
            missing_ok=True
        )
        raise
    finally:
        if own_session:
            session.close()


async def iter_coomer(
    link: str,
    temp_dir: Path,
    job_id: int,
    file_folders: dict[str, str | None] | None = None,
    status_cb=None,
):
    """Download a Coomer profile or post file by file, yielding each file.

    Mirrors iter_bunkr: one fresh subfolder per file keeps disk usage
    bounded by the largest file, a broken file does not stop the
    profile, and JOB_PROGRESS feeds /queue. file_folders maps each
    path to the profile name so the worker can route the files into a
    per-profile topic. status_cb(text) is awaited with
    "Coomer: <n>/<total> - <filename>" before each download.
    """
    if RUNNING['cancel']:
        raise JobCancelled()

    parsed = parse_coomer_link(link)
    if parsed is None:
        raise CoomerError(
            f'Unsupported Coomer URL {link}: expected /<service>/user/<name> '
            'with an optional /<post_id>'
        )
    kind, service, user, post_id = parsed
    api_base = _coomer_api_base(link)
    referer = link.rsplit('?', 1)[0]
    start_offset = _coomer_start_offset(link)

    session = coomer_session()
    try:
        media = await asyncio.to_thread(
            fetch_coomer_media, api_base, service, user, session, post_id,
            start_offset,
        )
        if not media:
            raise CoomerError(
                f'Coomer profile {user} ({service}) has no downloadable files'
            )
        title = f'{service}/{user}'

        total = len(media)
        failed = 0
        last_error = ''
        cdn_failures = 0
        state = {'thumb_only': False}
        progress = JOB_PROGRESS.setdefault(job_id, {})
        progress.update(total=total, current=0, current_name='')

        for index, (name, path) in enumerate(media, start=1):
            if RUNNING['cancel']:
                raise JobCancelled()
            if index > 1 and COOMER_FILE_DELAY > 0:
                await asyncio.sleep(COOMER_FILE_DELAY)

            progress['current'] = index
            filename = coomer_filename(name, path)
            progress['current_name'] = filename
            log(f'[coomer] ({index}/{total}) Downloading {filename}')
            if status_cb is not None:
                await status_cb(f'Coomer: {index}/{total} \u2014 {filename}')

            sub_dir = temp_dir / f'coomer{index:04d}'
            sub_dir.mkdir()
            try:
                dest = await asyncio.to_thread(
                    download_coomer_file,
                    f'{api_base}/data/{path}',
                    str(sub_dir / filename),
                    make_download_logger(filename, 'coomer'),
                    referer,
                    session,
                    state,
                )
            except JobCancelled:
                raise
            except Exception as exc:
                # One broken file should not kill the whole profile job.
                failed += 1
                last_error = str(exc) or type(exc).__name__
                progress['failed'] = progress.get('failed', 0) + 1
                if _is_coomer_connection_error(exc):
                    cdn_failures += 1
                    if cdn_failures >= COOMER_CDN_FAIL_LIMIT:
                        raise CoomerError(
                            f'Coomer CDN unreachable {cdn_failures} times in '
                            'a row - the CDN node net is down or has no '
                            'route from this host; try /retry later. Last '
                            f'error: {last_error}'
                        )
                else:
                    cdn_failures = 0
                log(f'[coomer] ({index}/{total}) Failed {path}: {last_error}')
                continue

            cdn_failures = 0
            filename = dest.name
            progress['current_name'] = filename
            log(
                f'[coomer] ({index}/{total}) Downloaded {filename} '
                f'({format_size(dest.stat().st_size)})'
            )
            if file_folders is not None:
                file_folders[str(dest)] = title
            yield dest

        if failed:
            log(f'[coomer] {failed} of {total} file(s) failed')
            if failed == total:
                raise CoomerError(
                    f'All {total} Coomer file(s) failed; last error: '
                    f'{last_error}'
                )
    finally:
        session.close()


# ---- Coomerfans configuration ----
# coomerfans.com mirrors Coomer but serves server-rendered HTML instead
# of a JS shell and exposes no /api JSON endpoints (they all 404), so
# posts and profiles are scraped from the page. Unlike coomer.st it
# serves the full videos itself and is not behind the 91.149.227.0/24
# CDN node net, which makes it the reliable mirror on hosts that cannot
# route to that net.
COOMERFANS_DOMAINS = {'coomerfans.com'}
COOMERFANS_DOMAIN_REGEX = re.compile(r'(?:^|\.)coomerfans\.[a-z]{2,12}$')
# Candidate URLs in a message; the host is checked afterwards. Posts live
# at /p/<post_id>/<user_id>/<service>, profiles at /u/<service>/<user_id>/<name>.
COOMERFANS_URL_REGEX = re.compile(
    r'https?://[\w.-]*coomerfans\.[a-z]{2,12}/(?:p|u)/[^\s<>"\')]+',
    re.IGNORECASE,
)
# Back-off before retry 1, 2 and 3 after HTTP 429, 5xx or a connection error.
COOMERFANS_RETRY_DELAYS = (10, 30, 60)
# (connect, read) timeouts in seconds for Coomerfans page and file requests.
COOMERFANS_TIMEOUT = (15, 90)
# Streaming download chunk size.
COOMERFANS_CHUNK = 8192
# Pause between two files of a profile, to stay below rate limits.
COOMERFANS_FILE_DELAY = env_float('COOMERFANS_FILE_DELAY', 1.0)
# Safety cap for how many posts of a profile are scanned (0 = all).
COOMERFANS_MAX_POSTS = int(env_float('COOMERFANS_MAX_POSTS', 0))
# Turning this off (COOMERFANS_ENABLED=0) hides /coomerfans and makes
# coomerfans.com links inert, leaving the coomer.st path untouched.
COOMERFANS_ENABLED = (
    os.environ.get('COOMERFANS_ENABLED', '1').lower()
    not in ('0', 'false', 'no', '')
)
# File suffixes served as post media: images and the full videos.
COOMERFANS_MEDIA_EXT = (
    r'(?:jpe?g|png|gif|webp|mp4|m4v|mkv|webm|mov|avi|wmv|flv|mpg|mpeg'
    r'|3gp|ts)'
)
# Suffixes treated as video, only so parse_coomerfans_media can order
# images before videos (Coomerfans serves real video, not posters).
COOMERFANS_VIDEO_EXTS = {
    '.mp4', '.m4v', '.mkv', '.webm', '.mov', '.avi', '.wmv', '.flv',
    '.mpg', '.mpeg', '.3gp', '.ts',
}
# One <img>/<source> src on an img<digit>.coomerfans.com/storage path is
# post media; the sidebar avatars live under /istorage/ and never match,
# so they are ignored. Signed media URLs carry a query (?e=..&hash=..).
COOMERFANS_SRC_REGEX = re.compile(
    r'src="?(https?://img\d+\.coomerfans\.com/storage/'
    r'[^"\'\s<>]+?\.' + COOMERFANS_MEDIA_EXT + r'(?:\?[^"\'\s<>]*)?)"?',
    re.IGNORECASE,
)
# Post links collected from a profile page: /p/<post_id>/<user_id>/<service>.
COOMERFANS_POSTLINK_REGEX = re.compile(r'/p/\d+/\d+/[a-z][\w-]*')
# Hard ceiling for profile paging so a huge profile does not page forever.
COOMERFANS_MAX_PAGES = 50


class CoomerfansError(RuntimeError):
    """A Coomerfans page or file could not be fetched or parsed."""


def _coomerfans_host(url: str) -> str:
    """Return the lowercased host of a Coomerfans URL."""
    return urlsplit(url).hostname.lower() if urlsplit(url).hostname else ''


def is_coomerfans_url(url: str) -> bool:
    """Return True when the URL's host is a Coomerfans mirror domain."""
    if not COOMERFANS_ENABLED:
        return False
    host = _coomerfans_host(url)
    return host in COOMERFANS_DOMAINS or bool(COOMERFANS_DOMAIN_REGEX.match(host))


def parse_coomerfans_link(url: str):
    """Split a Coomerfans URL into (kind, service, id, name).

    Posts are /p/<post_id>/<user_id>/<service> and return
    ('post', service, post_id, user_id); profiles are
    /u/<service>/<user_id>/<username> and return
    ('profile', service, user_id, username). Returns None otherwise
    (index, search, static files, an unknown first segment).
    """
    parts = urlsplit(url)
    if parts.scheme not in ('http', 'https'):
        return None
    segments = [s for s in parts.path.split('/') if s]
    if len(segments) != 4:
        return None
    if segments[0] == 'p' and segments[1].isdigit() and segments[2].isdigit():
        service = segments[3].lower()
        if re.match(r'^[a-z][\w-]*$', service):
            return ('post', service, segments[1], segments[2])
    elif segments[0] == 'u' and segments[2].isdigit():
        service = segments[1].lower()
        if re.match(r'^[a-z][\w-]*$', service):
            return ('profile', service, segments[2], segments[3])
    return None


def coomerfans_links_in(text: str) -> list[str]:
    """Return the Coomerfans profile/post URLs contained in a message text."""
    urls = []
    for url in clean_urls(COOMERFANS_URL_REGEX.findall(text)):
        if is_coomerfans_url(url) and parse_coomerfans_link(url) is not None:
            urls.append(url)
    return urls


def _coomerfans_unescape(text: str) -> str:
    """Turn an HTML-escaped '&amp;' back into a real '&' for downloads."""
    return text.replace('&amp;', '&')


def coomerfans_filename(url: str, fallback: str = 'coomerfans_file') -> str:
    """Return a safe file name from the storage object of a media URL."""
    candidate = urlsplit(url).path.rsplit('/', 1)[-1]
    candidate = re.sub(r'[\\/\x00-\x1f]', '', candidate).strip()
    candidate = re.sub(r'[^\w.\-]+', '_', candidate).strip('._-')
    if not candidate:
        candidate = fallback
    return candidate[:240]


def coomerfans_session():
    """Build a requests session that fetches Coomerfans HTML as a browser.

    coomerfans.com answers with server-rendered HTML, not JSON, so the
    session asks for text/html like a browser; unlike the coomer.st JSON
    API it does not need the 'Accept: text/css' trick.
    """
    if not HAVE_BUNKR_DEPS:
        raise CoomerfansError(
            'Coomerfans support needs the requests package: '
            'pip install requests'
        )
    session = requests.Session()
    session.headers.update({
        'User-Agent': COOMER_USER_AGENT,
        'Accept': (
            'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
        ),
    })
    return session


def coomerfans_request(session, url: str, params=None, what: str = 'page'):
    """GET a Coomerfans URL, retrying on HTTP 429, 5xx and connection errors.

    Returns the response on HTTP 200. HTTP 403/404 raise CoomerfansError
    right away: a bot block or a missing page is not worth a retry. The
    caller treats a raised 404 as the end of profile paging.
    """
    attempts = len(COOMERFANS_RETRY_DELAYS) + 1
    for attempt in range(attempts):
        try:
            response = session.get(
                url,
                params=params,
                timeout=COOMERFANS_TIMEOUT,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            reason = str(exc) or type(exc).__name__
            if attempt == attempts - 1:
                raise CoomerfansError(
                    f'Coomerfans {what} {url} failed after {attempts} '
                    f'attempts: {reason}'
                ) from exc
            delay = COOMERFANS_RETRY_DELAYS[attempt]
            log(f'[coomerfans] {what} {url}: {reason} - retry in {delay}s')
            _coomer_sleep(delay)
            continue

        status = response.status_code
        if status == 200:
            return response
        reason = response.reason or ''
        if status in (403, 404):
            raise CoomerfansError(
                f'HTTP {status} for Coomerfans {what} {url}: {reason}'
            )
        if attempt == attempts - 1:
            raise CoomerfansError(
                f'HTTP {status} for Coomerfans {what} {url}: {reason}'
            )
        delay = COOMERFANS_RETRY_DELAYS[attempt]
        log(f'[coomerfans] {what} {url}: HTTP {status} - retry in {delay}s')
        _coomer_sleep(delay)
    raise CoomerfansError(f'Giving up on Coomerfans {what} {url}')


def parse_coomerfans_media(html: str) -> list[str]:
    """Return the absolute post-media URLs found in a Coomerfans page.

    Only <img>/<source> src values pointing at an
    img<digit>.coomerfans.com/storage path count as post media; the
    sidebar avatars served under /istorage/ are never matched and are
    skipped. A signed query is HTML-escaped as '&amp;' in the page and
    is turned back into a real '&' here, so the download is not rejected
    with 403. Images are returned before videos, each de-duplicated in
    page order.
    """
    images: list[str] = []
    videos: list[str] = []
    seen: set[str] = set()
    for raw in COOMERFANS_SRC_REGEX.findall(html):
        url = _coomerfans_unescape(raw)
        if url in seen:
            continue
        seen.add(url)
        suffix = Path(urlsplit(url).path).suffix.lower()
        if suffix in COOMERFANS_VIDEO_EXTS:
            videos.append(url)
        else:
            images.append(url)
    return images + videos


def _coomerfans_start_page(link: str) -> int:
    """Read the `?page=<n>` start page of a Coomerfans profile link.

    Returns the integer value of the first `page` query parameter so a
    profile job starts at that page and then pages on as usual. Missing,
    non-numeric or values below 1 fall back to 1 (start from the top).
    """
    for key, value in parse_qsl(urlsplit(link).query):
        if key == 'page':
            try:
                page = int(value)
            except (TypeError, ValueError):
                return 1
            return page if page > 1 else 1
    return 1


def fetch_coomerfans_media(link: str) -> list[str]:
    """Return the media URLs of a Coomerfans post or profile.

    A post is a single page: scrape it and parse its media. A profile
    pages through /u/<service>/<user_id>/<name>?page=<n>, collecting the
    /p/... post links that belong to the same user on each page and
    scraping each of those post pages, combining their media. A `?page=N`
    in the profile link is a start/skip page: paging begins there and
    then continues to the limit or the end. A global
    seen set de-duplicates URLs across posts. COOMERFANS_MAX_POSTS caps
    the number of posts scanned (0 = all, up to COOMERFANS_MAX_PAGES).
    """
    parsed = parse_coomerfans_link(link)
    if parsed is None:
        raise CoomerfansError(f'Not a Coomerfans post or profile URL: {link}')
    kind = parsed[0]
    parts = urlsplit(link)
    api_base = f'{parts.scheme or "https"}://{parts.netloc}'

    session = coomerfans_session()
    try:
        if kind == 'post':
            response = coomerfans_request(session, link, what='post')
            media = parse_coomerfans_media(response.text)
            if not media:
                raise CoomerfansError(
                    f'Coomerfans post has no downloadable files: {link}'
                )
            return media

        _, service, user_id, username = parsed
        start_page = _coomerfans_start_page(link)
        seen: set[str] = set()
        media: list[str] = []
        scanned = 0
        for page in range(start_page, COOMERFANS_MAX_PAGES + 1):
            page_url = f'{api_base}/u/{service}/{user_id}/{username}'
            try:
                response = coomerfans_request(
                    session, page_url,
                    params={'page': page}, what='profile page',
                )
            except CoomerfansError as exc:
                if 'HTTP 404' in str(exc) and page > start_page:
                    break
                raise
            page_posts = list(dict.fromkeys(
                COOMERFANS_POSTLINK_REGEX.findall(response.text)
            ))
            # Only this user's own posts, so a shared sidebar of other
            # profiles' trending posts is not scraped as our media.
            page_posts = [p for p in page_posts if _coomerfans_same_user(p, user_id)]
            if not page_posts:
                break
            for post in page_posts:
                if COOMERFANS_MAX_POSTS and scanned >= COOMERFANS_MAX_POSTS:
                    break
                post_url = f'{api_base}{post}'
                try:
                    post_response = coomerfans_request(
                        session, post_url, what='post'
                    )
                except CoomerfansError as exc:
                    log(f'[coomerfans] post {post_url} skipped: {exc}')
                    continue
                for url in parse_coomerfans_media(post_response.text):
                    if url not in seen:
                        seen.add(url)
                        media.append(url)
                scanned += 1
            if COOMERFANS_MAX_POSTS and scanned >= COOMERFANS_MAX_POSTS:
                log(f'[coomerfans] COOMERFANS_MAX_POSTS={COOMERFANS_MAX_POSTS} reached')
                break
        if not media:
            raise CoomerfansError(
                f'Coomerfans profile {username} ({service}) has no '
                'downloadable files'
            )
        return media
    finally:
        session.close()


def _coomerfans_same_user(post: str, user_id: str) -> bool:
    """Return True when a /p/<post_id>/<user_id>/<service> link is ours."""
    segments = [s for s in post.split('/') if s]
    return len(segments) == 4 and segments[0] == 'p' and segments[2] == user_id


def download_coomerfans_file(
    url: str,
    dest_path: str,
    progress_cb=None,
    referer: str = '',
    session=None,
    state=None,
):
    """Stream a Coomerfans storage file to dest_path and return the final Path.

    Unlike coomer.st the bytes come straight from img<digit>.coomerfans.com,
    so there is no CDN redirect and no thumbnail fallback. The signed
    query (kept verbatim; its '&' must not be re-escaped) authorises the
    URL and a Referer of the post page is sent. HTTP 403/404, an empty
    body or a short Content-Length raise CoomerfansError so the caller
    skips that one file. `state` is unused but kept for signature parity.
    """
    own_session = session is None
    session = session or coomerfans_session()
    headers = {}
    if referer:
        headers['Referer'] = referer

    written = 0
    total = 0
    try:
        try:
            response = session.get(
                url,
                headers=headers,
                timeout=COOMERFANS_TIMEOUT,
                stream=True,
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            raise CoomerfansError(
                f'Coomerfans file {url}: {str(exc) or type(exc).__name__}'
            ) from exc

        try:
            if response.status_code != 200:
                raise CoomerfansError(
                    f'HTTP {response.status_code} for Coomerfans file {url}'
                )
            total = int(response.headers.get('Content-Length') or 0)
            dest = Path(dest_path)
            part = dest.with_name(dest.name + '.part')
            try:
                with open(part, 'wb') as handle:
                    for chunk in response.iter_content(COOMERFANS_CHUNK):
                        if RUNNING['cancel']:
                            raise JobCancelled()
                        if not chunk:
                            continue
                        written += len(chunk)
                        handle.write(chunk)
                        if progress_cb is not None:
                            progress_cb(written, total)
            except BaseException:
                part.unlink(missing_ok=True)
                raise
            if written == 0:
                part.unlink(missing_ok=True)
                raise CoomerfansError(
                    f'Coomerfans sent an empty file for {url}'
                )
            if total and written != total:
                part.unlink(missing_ok=True)
                raise CoomerfansError(
                    f'Incomplete Coomerfans download: {written} of {total} bytes'
                )
            part.replace(dest)
        finally:
            response.close()
        return dest
    except BaseException:
        Path(dest_path).with_name(Path(dest_path).name + '.part').unlink(
            missing_ok=True
        )
        raise
    finally:
        if own_session:
            session.close()


async def iter_coomerfans(
    link: str,
    temp_dir: Path,
    job_id: int,
    file_folders: dict[str, str | None] | None = None,
    status_cb=None,
):
    """Download a Coomerfans profile or post file by file, yielding each file.

    Mirrors iter_coomer: one fresh subfolder per file keeps disk usage
    bounded by the largest file, a broken file does not stop the job,
    and JOB_PROGRESS feeds /queue. file_folders maps each path to the
    post/profile title so the worker can route the files into a topic.
    status_cb(text) is awaited with
    "Coomerfans: <n>/<total> \u2014 <filename>" before each download.
    """
    if RUNNING['cancel']:
        raise JobCancelled()

    parsed = parse_coomerfans_link(link)
    if parsed is None:
        raise CoomerfansError(
            f'Unsupported Coomerfans URL {link}: expected '
            '/p/<post_id>/<user_id>/<service> or '
            '/u/<service>/<user_id>/<name>'
        )
    kind = parsed[0]
    referer = link.rsplit('?', 1)[0]

    session = coomerfans_session()
    try:
        media = await asyncio.to_thread(fetch_coomerfans_media, link)
        if not media:
            raise CoomerfansError(
                f'Coomerfans link {link} has no downloadable files'
            )
        title = f'coomerfans/{parsed[3]}' if kind == 'profile' else referer

        total = len(media)
        failed = 0
        last_error = ''
        progress = JOB_PROGRESS.setdefault(job_id, {})
        progress.update(total=total, current=0, current_name='')

        for index, url in enumerate(media, start=1):
            if RUNNING['cancel']:
                raise JobCancelled()
            if index > 1 and COOMERFANS_FILE_DELAY > 0:
                await asyncio.sleep(COOMERFANS_FILE_DELAY)

            progress['current'] = index
            filename = coomerfans_filename(url)
            progress['current_name'] = filename
            log(f'[coomerfans] ({index}/{total}) Downloading {filename}')
            if status_cb is not None:
                await status_cb(f'Coomerfans: {index}/{total} \u2014 {filename}')

            sub_dir = temp_dir / f'coomerfans{index:04d}'
            sub_dir.mkdir()
            try:
                dest = await asyncio.to_thread(
                    download_coomerfans_file,
                    url,
                    str(sub_dir / filename),
                    make_download_logger(filename, 'coomerfans'),
                    referer,
                    session,
                    None,
                )
            except JobCancelled:
                raise
            except Exception as exc:
                # One broken file should not kill the whole profile job.
                failed += 1
                last_error = str(exc) or type(exc).__name__
                progress['failed'] = progress.get('failed', 0) + 1
                log(f'[coomerfans] ({index}/{total}) Failed {url}: {last_error}')
                continue

            filename = dest.name
            progress['current_name'] = filename
            log(
                f'[coomerfans] ({index}/{total}) Downloaded {filename} '
                f'({format_size(dest.stat().st_size)})'
            )
            if file_folders is not None:
                file_folders[str(dest)] = title
            yield dest

        if failed:
            log(f'[coomerfans] {failed} of {total} file(s) failed')
            if failed == total:
                raise CoomerfansError(
                    f'All {total} Coomerfans file(s) failed; last error: '
                    f'{last_error}'
                )
    finally:
        session.close()


# ---- Pasted-text hardening ----
# Invisible Unicode characters (zero-width spaces, direction marks, word
# joiners) that sneak into messages when text is copied from other chats.
# Without tolerance for them, pasted commands are silently ignored.
INVISIBLE = r"\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff"
INVISIBLE_RE = re.compile(rf"[{INVISIBLE}]")
GAP = rf"[{INVISIBLE}\s]*"

# Colors accepted by Telegram for the fallback topic icon (RGB values).
TOPIC_ICON_COLORS = [
    0x6FB9F0, 0xFFD67E, 0xCB86DB, 0x8EEE98, 0xFF93B2, 0xFB6F5F,
]

# Topic lookup uses the persistent local registry, not Telegram pagination.
TOPIC_CREATE_LOCK = asyncio.Lock()

# The session file lives in DATA_DIR and Telethon opens it in the
# constructor, so the directory must exist before the client is built.
DATA_DIR.mkdir(parents=True, exist_ok=True)

client = TelegramClient(SESSION_PATH, API_ID, API_HASH)

# Strong references to fire-and-forget tasks; the event loop only keeps
# weak references, so unreferenced tasks may be garbage collected early.
BACKGROUND_TASKS: set[asyncio.Task] = set()


def spawn(awaitable) -> asyncio.Future:
    """Start a background task and keep it referenced until it finishes.

    Accepts coroutines and other awaitables. Telethon's
    client.disconnect(), for example, returns an asyncio.shield() Future
    (not a coroutine) while the loop is running; create_task() would
    reject it with "TypeError: a coroutine was expected".
    """
    if asyncio.iscoroutine(awaitable):
        task = asyncio.create_task(awaitable)
    else:
        task = asyncio.ensure_future(awaitable)
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)
    return task


# ---- Logging helpers ----
def log(message: str) -> None:
    """Print a timestamped log line to stdout."""
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"{timestamp} {message}", flush=True)


def format_size(size: float) -> str:
    """Return a byte count as a human-readable string."""
    size = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024:
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PiB"


def clean_urls(raw_urls: list[str]) -> list[str]:
    """Drop trailing punctuation and duplicates from matched URLs."""
    cleaned = (url.rstrip("),.!?:;>") for url in raw_urls)
    return list(dict.fromkeys(url for url in cleaned if url))


# ---- Cancellation state ----
# The worker processes one job at a time. /cancel talks to it through
# this shared state; all code runs in the same event loop, so no
# locking is needed.
# "shutdown" is set once on SIGTERM/SIGINT; the worker then keeps
# "cancel" raised so blocking download threads stop at the next chunk.
RUNNING = {"job_id": None, "process": None, "cancel": False, "shutdown": False}

# Live progress of the running folder job, keyed by job ID and read
# by /queue. iter_download writes the position inside the folder
# listing, the worker writes the upload/skip counters. The entry is
# removed when the job ends, so /queue can tell running from idle.
JOB_PROGRESS: dict[int, dict] = {}


class JobCancelled(Exception):
    """Raised when the user cancels the currently running job."""


def link_kind(link: str) -> str:
    """Return 'file' or 'folder' for a MEGA link."""
    # A /folder/ share stays folder-based even when the link selects a
    # file inside it (.../folder/<handle>#<key>/file/<file handle>):
    # such files are only addressable with the folder as API context.
    if "/folder/" in link:
        return "folder"
    if "/file/" in link or "#!" in link:
        return "file"
    return "folder"


# ---- Database: persistent job queue ----
def db_connect() -> sqlite3.Connection:
    """Open a connection to the SQLite database."""
    return sqlite3.connect(DB_PATH)


def init_db() -> None:
    """Create the tables and recover jobs interrupted by a restart."""
    with closing(db_connect()) as db, db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                link TEXT NOT NULL,
                topic_id INTEGER,
                requester INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                auto_folder_topics INTEGER NOT NULL DEFAULT 0,
                force_dupes INTEGER NOT NULL DEFAULT 0,
                file_delay REAL NOT NULL DEFAULT 0,
                retry_attempt INTEGER NOT NULL DEFAULT 0,
                not_before REAL,
                recode INTEGER NOT NULL DEFAULT 0
            )
        """)

        # Migrate existing databases without disturbing queued jobs.
        columns = {
            row[1] for row in db.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if "auto_folder_topics" not in columns:
            db.execute(
                "ALTER TABLE jobs ADD COLUMN auto_folder_topics "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "force_dupes" not in columns:
            db.execute(
                "ALTER TABLE jobs ADD COLUMN force_dupes "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "file_delay" not in columns:
            db.execute(
                "ALTER TABLE jobs ADD COLUMN file_delay "
                "REAL NOT NULL DEFAULT 0"
            )
        if "retry_attempt" not in columns:
            db.execute(
                "ALTER TABLE jobs ADD COLUMN retry_attempt "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "not_before" not in columns:
            db.execute("ALTER TABLE jobs ADD COLUMN not_before REAL")
        if "recode" not in columns:
            db.execute(
                "ALTER TABLE jobs ADD COLUMN recode INTEGER NOT NULL DEFAULT 0"
            )

        # Registry of forum topics the bot created or saw being created.
        db.execute("""
            CREATE TABLE IF NOT EXISTS topics (
                id INTEGER PRIMARY KEY,
                title TEXT NOT NULL
            )
        """)

        # Content hashes of uploaded files, used to skip duplicates.
        # node_handle is the file's own MEGA node ID, known before any
        # download and stable across renames.
        db.execute("""
            CREATE TABLE IF NOT EXISTS seen_files (
                hash TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                size INTEGER NOT NULL,
                job_id INTEGER,
                node_handle TEXT
            )
        """)

        # Older databases lack the node_handle column.
        seen_columns = {
            row[1] for row in db.execute(
                "PRAGMA table_info(seen_files)"
            ).fetchall()
        }
        if "node_handle" not in seen_columns:
            db.execute(
                "ALTER TABLE seen_files ADD COLUMN node_handle TEXT"
            )

        # History of duplicate-skip events: "name/size" for skips
        # before the download, "hash" for skips after it.
        db.execute("""
            CREATE TABLE IF NOT EXISTS skipped_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                size INTEGER NOT NULL,
                reason TEXT NOT NULL,
                job_id INTEGER,
                skipped_at TEXT NOT NULL
            )
        """)

        # Before the 'requeued' status existed, a job that the worker had
        # automatically re-queued (API error -16 / HTTP 509) stayed
        # 'failed', so a later /retry ran it a second time next to its
        # follow-up job. Reclassify those: a failed job is superseded
        # when a later job for the same link carries the next attempt.
        db.execute("""
            UPDATE jobs SET status = 'requeued'
            WHERE status = 'failed'
              AND EXISTS (
                  SELECT 1 FROM jobs AS later
                  WHERE later.link = jobs.link
                    AND later.id > jobs.id
                    AND later.retry_attempt = jobs.retry_attempt + 1
              )
        """)

        # Jobs marked as running were interrupted and should be processed again.
        db.execute(
            "UPDATE jobs SET status = 'pending' WHERE status = 'running'"
        )


def add_jobs(
    links: list[str],
    topic_id: int | None,
    requester: int,
    auto_folder_topics: bool = False,
    force_dupes: bool = False,
    file_delay: float = 0.0,
    retry_attempt: int = 0,
    not_before: float | None = None,
    recode: bool = False,
) -> list[int]:
    """Add new jobs to the persistent queue; return their job IDs.

    not_before is an epoch timestamp: the job is not claimed before
    that time (used to pause jobs after a MEGA bandwidth limit).
    recode enables 720p video preparation for this job (-recode flag).
    """
    with closing(db_connect()) as db, db:
        ids: list[int] = []

        for link in links:
            cursor = db.execute(
                """
                INSERT INTO jobs (
                    link, topic_id, requester, status, auto_folder_topics,
                    force_dupes, file_delay, retry_attempt, not_before,
                    recode
                )
                VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
                """,
                (
                    link,
                    topic_id,
                    requester,
                    int(auto_folder_topics),
                    int(force_dupes),
                    float(file_delay),
                    int(retry_attempt),
                    not_before,
                    int(recode),
                ),
            )
            ids.append(int(cursor.lastrowid))

        return ids


def claim_job() -> (
    tuple[int, str, int | None, int, bool, bool, float, int, bool] | None
):
    """Get the oldest claimable pending job and mark it as running.

    Jobs with a not_before timestamp in the future (paused after a
    bandwidth limit) are skipped until their wait has passed.
    """
    with closing(db_connect()) as db, db:
        row = db.execute(
            """
            SELECT id, link, topic_id, requester, auto_folder_topics,
                   force_dupes, file_delay, retry_attempt, recode
            FROM jobs
            WHERE status = 'pending'
              AND (not_before IS NULL OR not_before <= ?)
            ORDER BY id
            LIMIT 1
            """,
            (time.time(),),
        ).fetchone()

        if row is None:
            return None

        db.execute(
            "UPDATE jobs SET status = 'running' WHERE id = ?",
            (row[0],),
        )
        return (
            row[0],
            row[1],
            row[2],
            row[3],
            bool(row[4]),
            bool(row[5]),
            float(row[6] or 0.0),
            int(row[7] or 0),
            bool(row[8]),
        )


def finish_job(
    job_id: int, error: str | None = None, status: str | None = None
) -> None:
    """Mark a job as done or failed (or an explicit status).

    status="requeued" is used when the worker has already queued a
    follow-up job itself; such jobs are not 'failed', so /retry and
    /failed leave them alone.
    """
    if status is None:
        status = "failed" if error else "done"

    with closing(db_connect()) as db, db:
        db.execute(
            "UPDATE jobs SET status = ?, error = ? WHERE id = ?",
            (status, error, job_id),
        )


def queue_status() -> tuple[int | None, int]:
    """Return the running job ID and the number of pending jobs."""
    with closing(db_connect()) as db:
        running = db.execute(
            """
            SELECT id FROM jobs
            WHERE status = 'running'
            ORDER BY id
            LIMIT 1
            """
        ).fetchone()

        pending = db.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'pending'"
        ).fetchone()[0]

    return (running[0] if running else None, pending)


def pending_jobs(limit: int = 10) -> list[tuple[int, str, float | None]]:
    """Return the next pending jobs as (id, link, not_before) tuples."""
    with closing(db_connect()) as db:
        return db.execute(
            """
            SELECT id, link, not_before
            FROM jobs
            WHERE status = 'pending'
            ORDER BY id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def retry_jobs(job_id: int | None = None) -> int:
    """Reset failed jobs to pending and return the number of reset jobs."""
    with closing(db_connect()) as db, db:
        if job_id is None:
            cursor = db.execute(
                """
                UPDATE jobs
                SET status = 'pending', error = NULL
                WHERE status = 'failed'
                """
            )
        else:
            cursor = db.execute(
                """
                UPDATE jobs
                SET status = 'pending', error = NULL
                WHERE status = 'failed' AND id = ?
                """,
                (job_id,),
            )

        return cursor.rowcount


def remove_pending_job(job_id: int) -> bool:
    """Delete a pending job from the queue; return True if it existed."""
    with closing(db_connect()) as db, db:
        cursor = db.execute(
            "DELETE FROM jobs WHERE id = ? AND status = 'pending'",
            (job_id,),
        )
        return cursor.rowcount > 0


def list_failed(
    limit: int = 20,
) -> tuple[list[tuple[int, str, str]], int]:
    """Return recent failed jobs and the total number of failed jobs."""
    with closing(db_connect()) as db:
        rows = db.execute(
            """
            SELECT id, link, COALESCE(error, '')
            FROM jobs
            WHERE status = 'failed'
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        total = db.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'failed'"
        ).fetchone()[0]

    return rows, total


def save_topic(topic_id: int, title: str) -> None:
    """Remember a forum topic, updating its title after a rename."""
    if topic_id <= 0 or not title.strip():
        raise ValueError("Topic ID must be positive and title must not be empty")
    with closing(db_connect()) as db, db:
        db.execute(
            """
            INSERT INTO topics (id, title) VALUES (?, ?)
            ON CONFLICT(id) DO UPDATE SET title = excluded.title
            """,
            (topic_id, title),
        )


def saved_topics() -> list[tuple[int, str]]:
    """Return all known forum topics as (id, title) pairs."""
    with closing(db_connect()) as db:
        return db.execute(
            "SELECT id, title FROM topics ORDER BY id"
        ).fetchall()


def forget_topic(topic_id: int) -> bool:
    """Remove one topic from the registry; True when it existed."""
    with closing(db_connect()) as db, db:
        cursor = db.execute("DELETE FROM topics WHERE id = ?", (topic_id,))
        return cursor.rowcount > 0


def forget_all_topics() -> int:
    """Remove every topic from the registry; return the number removed."""
    with closing(db_connect()) as db, db:
        cursor = db.execute("DELETE FROM topics")
        return cursor.rowcount


def seen_file(hash_value: str) -> bool:
    """Return True when a file with this hash was uploaded before."""
    with closing(db_connect()) as db:
        return db.execute(
            "SELECT 1 FROM seen_files WHERE hash = ?",
            (hash_value,),
        ).fetchone() is not None


def seen_by_node(node_handle: str | None) -> bool:
    """Return True when this exact MEGA file node was uploaded before.

    The node handle comes from the folder listing, so it is known
    before any download starts, and it survives renames.
    """
    if not node_handle:
        return False

    with closing(db_connect()) as db:
        return db.execute(
            "SELECT 1 FROM seen_files WHERE node_handle = ? LIMIT 1",
            (node_handle,),
        ).fetchone() is not None


def seen_by_name_size(name: str, size: int) -> bool:
    """Return True when a file with this name and size was uploaded.

    Used to skip files before downloading them from MEGA at all.
    """
    with closing(db_connect()) as db:
        return db.execute(
            "SELECT 1 FROM seen_files WHERE name = ? AND size = ? LIMIT 1",
            (name, int(size)),
        ).fetchone() is not None


def mark_seen(
    hash_value: str,
    name: str,
    size: int,
    job_id: int,
    node_handle: str | None = None,
) -> None:
    """Record an uploaded file's content hash and MEGA node handle."""
    with closing(db_connect()) as db, db:
        db.execute(
            """
            INSERT OR IGNORE INTO seen_files
                (hash, name, size, job_id, node_handle)
            VALUES (?, ?, ?, ?, ?)
            """,
            (hash_value, name, size, job_id, node_handle),
        )


def seen_stats() -> tuple[int, int]:
    """Return the number of remembered files and their total size."""
    with closing(db_connect()) as db:
        row = db.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM seen_files"
        ).fetchone()

    return row[0], row[1]


def forget_seen_files() -> int:
    """Delete all remembered file hashes; return the old count."""
    with closing(db_connect()) as db, db:
        (count,) = db.execute("SELECT COUNT(*) FROM seen_files").fetchone()
        db.execute("DELETE FROM seen_files")

    return count


def mark_skipped(name: str, size: int, reason: str, job_id: int) -> None:
    """Record one duplicate-skip event in the persistent history."""
    with closing(db_connect()) as db, db:
        db.execute(
            """
            INSERT INTO skipped_files (name, size, reason, job_id, skipped_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                name,
                int(size),
                reason,
                job_id,
                time.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )


def skipped_stats() -> tuple[int, int, int, int]:
    """Return the total, node, name/size and hash skip counts."""
    with closing(db_connect()) as db:
        row = db.execute(
            """
            SELECT COUNT(*),
                   COALESCE(SUM(reason = 'node'), 0),
                   COALESCE(SUM(reason = 'name/size'), 0),
                   COALESCE(SUM(reason = 'hash'), 0)
            FROM skipped_files
            """
        ).fetchone()

    return row[0], row[1], row[2], row[3]


def recent_skipped(limit: int = 10) -> list[tuple[str, int, str, str]]:
    """Return the most recent skip events as (name, size, reason, when)."""
    with closing(db_connect()) as db:
        return db.execute(
            """
            SELECT name, size, reason, skipped_at
            FROM skipped_files
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()


def forget_skipped_files() -> int:
    """Delete the skip history; return the old count."""
    with closing(db_connect()) as db, db:
        (count,) = db.execute(
            "SELECT COUNT(*) FROM skipped_files"
        ).fetchone()
        db.execute("DELETE FROM skipped_files")

    return count


# ---- Download and upload ----
def dir_size(path: Path) -> int:
    """Return the total size of all files below path."""
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def file_hash(path: Path) -> str:
    """Return the SHA-256 hash of a file's content."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(DOWNLOAD_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


async def _pump_stream(stream, label: str) -> list[str]:
    """Log downloader output as it arrives.

    Lines containing a percentage (the moving progress bar) are rate
    limited so they do not flood the log. All other lines are logged
    instantly. Returns the collected lines for error reporting.
    """
    last_logged = 0.0
    buffer = b""
    lines: list[str] = []

    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break

        # Progress updates may be separated by \r instead of \n, and a
        # chunk boundary can fall inside a line, so buffer between reads.
        parts = re.split(rb"[\r\n]+", buffer + chunk)
        buffer = parts.pop()

        for raw in parts:
            line = raw.decode("utf-8", errors="ignore").strip()
            if not line:
                continue

            now = asyncio.get_running_loop().time()
            if "%" in line and now - last_logged < PROGRESS_INTERVAL:
                continue

            last_logged = now
            lines.append(line)
            log(f"[{label}] {line}")

    remainder = buffer.decode("utf-8", errors="ignore").strip()
    if remainder:
        lines.append(remainder)
        log(f"[{label}] {remainder}")

    return lines


async def run_megadl(link: str, target: Path) -> list[Path]:
    """Run megadl once for one link and return the downloaded files."""
    log(f"[dl] Starting download of {link}")

    process = await asyncio.create_subprocess_exec(
        "megadl",
        "--path",
        str(target),
        link,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    RUNNING["process"] = process
    log(f"[dl] megadl running as PID {process.pid}")

    # Stream both output channels so the log shows live progress.
    pump = asyncio.gather(
        _pump_stream(process.stdout, "dl:out"),
        _pump_stream(process.stderr, "dl:err"),
    )

    # Heartbeat: report how much data has landed on disk so far.
    started = asyncio.get_running_loop().time()

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL)

            # Backup termination in case /cancel ran between heartbeats.
            if RUNNING["cancel"] and process.returncode is None:
                log("[dl] Cancellation requested, stopping megadl")
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                continue

            size = await asyncio.to_thread(dir_size, target)
            elapsed = asyncio.get_running_loop().time() - started
            log(
                f"[dl] Still downloading ({int(elapsed)}s elapsed, "
                f"{format_size(size)} on disk)"
            )

    heartbeat_task = asyncio.create_task(heartbeat())

    try:
        output = await pump
        await process.wait()
    finally:
        heartbeat_task.cancel()
        RUNNING["process"] = None

    if RUNNING["cancel"]:
        raise JobCancelled()

    if process.returncode != 0:
        # Keep the last logged line, which carries megadl's actual error
        # (e.g. a bandwidth/quota message from MEGA).
        all_lines = output[0] + output[1]
        last_line = all_lines[-1] if all_lines else "unknown error"
        raise RuntimeError(f"megadl failed: {last_line}")

    # Include files inside subfolders.
    files = sorted(path for path in target.rglob("*") if path.is_file())
    total_size = sum(path.stat().st_size for path in files)

    log(
        f"[dl] Download finished: {len(files)} file(s), "
        f"{format_size(total_size)}"
    )
    for path in files:
        log(f"[dl]   - {path.name} ({format_size(path.stat().st_size)})")

    return files


# ---- MEGA public folder access ----
# Public folders are read through MEGA's public API without any account:
# the handle from the link selects the share, and the key from the link
# decrypts every node key (AES-128-ECB) and the file names inside the
# node attributes (AES-128-CBC with a zero IV). File contents are
# AES-128-CTR encrypted (the counter block is the 8-byte key nonce plus
# 8 zero bytes) and carry a chunked MAC in the second key half; the
# approach follows the megajs client library. Note that files inside a
# folder share are NOT addressable through standalone /file/ links:
# the folder handle must be sent as the API request context.

MEGA_API_URL = "https://g.api.mega.co.nz/cs"

MEGA_API_ERRORS = {
    -1: "internal error",
    -2: "invalid arguments",
    -3: "rate limited, try again later",
    -4: "too many concurrent requests",
    -5: "too many parallel downloads",
    -6: "insufficient permissions",
    -7: "file or folder not found",
    -8: "circular linkage",
    -9: "object not found",
    -10: "invalid or missing key",
    -11: "access denied",
    -12: "not enough credits",
    -13: "quota exceeded",
    -14: "invalid session",
    -15: "content removed or blocked",
    -16: "handle expired or rate limited (EEXPIRED)",
}

# Error codes worth retrying after a pause; all others fail instantly.
MEGA_RETRYABLE_ERRORS = {-3, -4, -16}


class MegaApiError(RuntimeError):
    """A MEGA API request answered with an error code."""

    def __init__(self, code: int) -> None:
        super().__init__(
            f"MEGA API error {code} ({MEGA_API_ERRORS.get(code, 'unknown')})"
        )


class MegaThrottled(MegaApiError):
    """MEGA answered -16: the share handle expired or, far more
    commonly, MEGA is rate limiting API access to the share."""


class MegaJobAborted(RuntimeError):
    """A folder job gave up because MEGA kept answering -16."""


class MegaBandwidthLimit(RuntimeError):
    """The storage server refused the download (HTTP 509)."""


class MegaTransientError(RuntimeError):
    """A download broke off for a reason a later attempt may not hit
    (connection reset, timeout, expired URL, 5xx, short read). Partial
    data is kept, so the retry resumes instead of starting over."""


class MegaResumeInvalid(RuntimeError):
    """Resumed partial data could not be used (range ignored by the
    server or MAC mismatch). It was discarded; retry from scratch."""


class InsufficientDiskSpace(RuntimeError):
    """Not enough free disk space for the next MEGA download.

    Stops the whole job: every remaining file would fail the same way.
    Partial resume data is kept and already uploaded files are skipped
    through the duplicate registry, so /retry continues cleanly once
    space has been freed.
    """


def mega_d64(text: str) -> bytes:
    """Decode MEGA's unpadded URL-safe base64."""
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def aes_ecb_decrypt(key: bytes, data: bytes) -> bytes:
    """Decrypt data with AES-128-ECB (pyaes works one block at a time)."""
    aes = pyaes.AESModeOfOperationECB(key)
    return b"".join(
        aes.decrypt(data[index:index + 16])
        for index in range(0, len(data), 16)
    )


def aes_cbc_decrypt_zero_iv(key: bytes, data: bytes) -> bytes:
    """Decrypt data with AES-128-CBC using a zero IV (MEGA attributes)."""
    aes = pyaes.AESModeOfOperationECB(key)
    plain = bytearray()
    previous = bytes(16)
    for index in range(0, len(data), 16):
        block = data[index:index + 16]
        decrypted = aes.decrypt(block)
        plain += bytes(a ^ b for a, b in zip(decrypted, previous))
        previous = block
    return bytes(plain)


def unmerge_key_mac(material: bytes) -> bytes:
    """Return the 16-byte AES key from stored file key material.

    Folder-contained file keys are 32 bytes: the AES key merged with
    the nonce/MAC half (see megajs unmergeKeyMac).
    """
    if len(material) == 32:
        return bytes(a ^ b for a, b in zip(material[:16], material[16:]))
    return material[:16]


def decrypt_mega_attrs(key: bytes, raw: str) -> dict | None:
    """Decrypt a node's attribute blob ("a" / "at") with its AES key.

    Attributes are "MEGA" plus a JSON object, zero padded and encrypted
    with AES-CBC under a zero IV. Returns None when the key is wrong.
    """
    try:
        plain = aes_cbc_decrypt_zero_iv(key, mega_d64(raw))
        plain = plain.split(b"\x00", 1)[0]
        if not plain.startswith(b"MEGA{"):
            return None
        attrs = json.loads(plain[4:].decode("utf-8"))
    except Exception:
        return None
    return attrs if isinstance(attrs, dict) else None


def decrypt_node(node: dict, folder_key: bytes):
    """Decrypt one node's key material and file name.

    Returns (key material, name), where the name may be None, or None
    when no key alternative could be decrypted.
    """
    # Several key versions can be separated by slashes; each may carry
    # an owning handle prefix before the encrypted key.
    for alternative in node.get("k", "").split("/"):
        try:
            encrypted = mega_d64(alternative.split(":")[-1])
            material = aes_ecb_decrypt(folder_key, encrypted)
            if len(material) not in (16, 32):
                continue

            raw = node.get("a")
            if not raw:
                return material, None

            attrs = decrypt_mega_attrs(unmerge_key_mac(material), raw)
            if attrs is None:
                continue
            return material, attrs.get("n")
        except Exception:
            continue

    return None


def mega_api(body: list, handle: str | None = None) -> dict:
    """Send one request to the MEGA public API and return the response."""
    payload = json.dumps(body).encode()
    url = f"{MEGA_API_URL}?id=0" + (f"&n={handle}" if handle else "")
    last_error: Exception | None = None

    for attempt in range(1, MEGA_API_RETRIES + 1):
        try:
            request = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(
                request, timeout=MEGA_API_TIMEOUT
            ) as response:
                result = json.loads(response.read().decode())

            # MEGA answers with a list of per-command results, but a
            # request-level error (e.g. -3 under load) arrives as a bare
            # integer instead of a list.
            first = result if isinstance(result, int) else result[0]
            if isinstance(first, int):
                # Transient errors (rate limits, expired public handle)
                # get a short backoff before the caller sees them.
                if (
                    first in MEGA_RETRYABLE_ERRORS
                    and attempt < MEGA_API_RETRIES
                ):
                    delay = 2 * attempt
                    log(
                        f"[mega] API error {first} "
                        f"({MEGA_API_ERRORS.get(first, 'unknown')}); "
                        f"retrying in {delay}s "
                        f"({attempt + 1}/{MEGA_API_RETRIES})"
                    )
                    time.sleep(delay)
                    continue

                if first == -16:
                    # Report -16 distinctly so the download loop can
                    # back off and stop instead of hammering the API.
                    raise MegaThrottled(first)

                raise MegaApiError(first)

            return first
        except MegaApiError:
            raise
        except Exception as exc:
            last_error = exc
            log(
                f"[mega] API request {attempt}/{MEGA_API_RETRIES} "
                f"failed: {exc}"
            )
            if attempt < MEGA_API_RETRIES:
                time.sleep(2 * attempt)

    raise RuntimeError(f"MEGA API unreachable: {last_error}")


def request_folder_nodes(handle: str) -> list[dict]:
    """Fetch the node list of a public folder (no account needed)."""
    return mega_api([{"a": "f", "c": 1, "ca": 1, "r": 1}], handle).get(
        "f", []
    )


def list_public_folder(
    link: str,
) -> list[tuple[str, str, int, bytes, str | None]]:
    """Turn a public folder link into downloadable file entries.

    Returns (path name, node handle, size, key material, topic folder)
    tuples. A link ending in a subfolder or file path limits the
    result to that subtree of the share.
    """
    match = re.search(r"/folder/([\w-]+)#([\w-]+)", link)
    if not match:
        raise RuntimeError("Not a supported (new-style) folder link")
    handle, key_text = match.groups()
    folder_key = mega_d64(key_text)
    sub_match = MEGA_SUBPATH_REGEX.search(link)
    sub_handle = sub_match.group(1) if sub_match else None
    nodes = request_folder_nodes(handle)
    by_handle = {node["h"]: node for node in nodes}
    # Validate the selected subtree before anything indexes by_handle
    # with it; an unknown handle used to raise a bare KeyError.
    if sub_handle and sub_handle not in by_handle:
        raise RuntimeError(
            "Selected subfolder or file is not part of the shared folder"
        )
    # The share's top-level folder node(s); their names stay out of the
    # reconstructed paths so topic routing sees the folders BELOW the
    # share root instead of the share's own name.
    root_handles = {
        node["h"] for node in nodes if node.get("p") not in by_handle
    }
    # Decrypt every node once; the names double as folder path parts.
    decrypted: dict[str, tuple[bytes, str | None]] = {}
    for node in nodes:
        result = decrypt_node(node, folder_key)
        if result is not None:
            decrypted[node["h"]] = result

    def folder_path(node: dict) -> str:
        """Reconstruct a node's path below the share root."""
        parts: list[str] = []
        current = node
        while current.get("p") in by_handle:
            current = by_handle[current["p"]]
            if current["h"] in root_handles:
                break
            name = decrypted.get(current["h"], (None, None))[1]
            if name is None:
                break
            parts.append(name)
        return "/".join(reversed(parts))

    # Full path of the subfolder selected by the link, INCLUDING its
    # own name. folder_path() only builds containing paths (it stops
    # at the node's parent), so the selected folder's own name has to
    # be appended here. Without it, topic routing started at the
    # selection itself and every topic was named after the selected
    # folder instead of its children. If the share root itself is
    # selected, the path stays empty and the first folder level below
    # the root routes the topics.
    selected_folder_path = ""
    if sub_handle and by_handle[sub_handle].get("t") == 1:
        selected_node = by_handle[sub_handle]
        if selected_node["h"] not in root_handles:
            own_name = decrypted.get(sub_handle, (None, None))[1]
            parent_path = folder_path(selected_node)
            if own_name:
                selected_folder_path = (
                    f"{parent_path}/{own_name}" if parent_path else own_name
                )
            else:
                selected_folder_path = parent_path

    # Restrict the download to the selected subtree: the target node
    # itself and everything below it.
    allowed: set[str] | None = None
    if sub_handle:
        children: dict[str, list[str]] = {}
        for node in nodes:
            children.setdefault(node.get("p"), []).append(node["h"])
        allowed = set()
        stack = [sub_handle]
        while stack:
            current = stack.pop()
            allowed.add(current)
            stack.extend(children.get(current, ()))
        target = decrypted.get(sub_handle, (None, None))[1] or sub_handle
        log(f"[dl] Link selects '{target}'; downloading only that subtree")

    entries: list[tuple[str, str, int, bytes, str | None]] = []
    for node in nodes:
        # Only real files with content are queued (t=0, size > 0).
        if node.get("t") != 0 or not node.get("s"):
            continue
        if allowed is not None and node["h"] not in allowed:
            continue
        result = decrypted.get(node["h"])
        if result is None:
            log(f"[dl] Skipping file with undecryptable key: {node['h']}")
            continue
        material, name = result
        path = folder_path(node)
        label = f"{path}/{name}" if path else (name or node["h"])
        # With a subfolder selected, the topic name comes from the
        # folder directly below that selection; deeper levels stay in
        # the same topic and files directly in the selection get none.
        # Without a selection, the first folder below the share root
        # routes the file.
        topic_folder = None
        if sub_handle and by_handle[sub_handle].get("t") == 1:
            if node.get("h") != sub_handle and name:
                full_parts = [part for part in label.split("/") if part]
                selected_parts = [
                    part for part in selected_folder_path.split("/") if part
                ]
                relative_parts = full_parts[len(selected_parts):]
                if len(relative_parts) > 1:
                    topic_folder = relative_parts[0]
        else:
            topic_folder = mega_folder_for_file(label)
        entries.append(
            (label, node["h"], node["s"], material, topic_folder)
        )
    entries.sort(key=lambda entry: entry[0])
    if sub_handle and not entries:
        raise RuntimeError("Selected subfolder contains no downloadable files")
    return entries


async def list_mega_files(link: str):
    """List the individual files contained in a MEGA folder link.

    Returns (name, node handle, size, key material, topic folder)
    tuples, or None when listing is not possible, in which case the
    caller falls back to one bulk download of the whole link. Links
    that select a subfolder or file never fall back: downloading the
    entire share would be the opposite of what was asked for.
    """
    if link_kind(link) == "file":
        return None
    log(f"[dl] Listing folder contents of {link}")
    # A link ending in a subfolder/file path must not silently degrade
    # to a bulk download of the whole share when listing fails.
    selects_subtree = bool(MEGA_SUBPATH_REGEX.search(link))
    try:
        # The listing is plain blocking HTTP; run it in a thread.
        entries = await asyncio.to_thread(list_public_folder, link)
    except Exception as exc:
        if selects_subtree:
            log(f"[dl] Listing the selected subfolder failed: {exc}")
            raise
        log(f"[dl] Listing failed, falling back to bulk download: {exc}")
        return None
    total_size = sum(entry[2] for entry in entries)
    log(
        f"[dl] Folder contains {len(entries)} file(s), "
        f"{format_size(total_size)}"
    )
    return entries or None


class MegaCtrDecryptor:
    """Incremental AES-128-CTR decryptor for MEGA file content.

    The counter block is the 8-byte key nonce followed by 8 zero
    bytes, counting as one 128-bit big-endian integer (megajs
    semantics). With the cryptography package this is OpenSSL-backed;
    the pyaes fallback assembles the keystream block by block and
    keeps it across calls because read chunks are not block aligned.
    """

    def __init__(self, key: bytes, nonce: bytes, offset: int = 0) -> None:
        # Resuming at a 16-byte aligned offset only advances the counter.
        if offset % 16:
            raise ValueError("CTR offset must be a multiple of 16")
        self._counter = (
            int.from_bytes(nonce + bytes(8), "big") + offset // 16
        ) % (1 << 128)
        self._keystream = b""

        if HAVE_FAST_CRYPTO:
            self._openssl = Cipher(
                algorithms.AES(key),
                modes.CTR(self._counter.to_bytes(16, "big")),
            ).decryptor()
        else:
            self._pyaes = pyaes.AESModeOfOperationECB(key)

    def update(self, data: bytes) -> bytes:
        """Decrypt the next arbitrary-length piece of ciphertext."""
        if HAVE_FAST_CRYPTO:
            return self._openssl.update(data)

        while len(self._keystream) < len(data):
            self._keystream += self._pyaes.encrypt(
                self._counter.to_bytes(16, "big")
            )
            self._counter = (self._counter + 1) % (1 << 128)

        stream, self._keystream = (
            self._keystream[:len(data)],
            self._keystream[len(data):],
        )
        plain = int.from_bytes(data, "big") ^ int.from_bytes(stream, "big")
        return plain.to_bytes(len(data), "big")


class MegaMac:
    """Chunked MAC over decrypted content (the megajs MAC class).

    Chunks start at 128 KiB and grow by 128 KiB per chunk up to 1 MiB.
    Requires the cryptography package; the pyaes fallback is too slow
    for per-block encryption.
    """

    def __init__(self, key: bytes, nonce: bytes) -> None:
        self._encryptor = Cipher(
            algorithms.AES(key), modes.ECB()
        ).encryptor()
        self._nonce = nonce
        self._mac = nonce + nonce
        self._macs: list[bytes] = []
        self._pos = 0
        self._increment = 131072
        self._pos_next = 131072

    @property
    def position(self) -> int:
        """Number of plaintext bytes folded in so far."""
        return self._pos

    def get_state(self) -> dict:
        """Serializable state, so a resumed download continues the MAC."""
        return {
            "mac": self._mac.hex(),
            "macs": [item.hex() for item in self._macs],
            "pos": self._pos,
            "increment": self._increment,
            "pos_next": self._pos_next,
        }

    def set_state(self, state: dict) -> None:
        """Restore a state produced by get_state()."""
        self._mac = bytes.fromhex(state["mac"])
        self._macs = [bytes.fromhex(item) for item in state["macs"]]
        self._pos = int(state["pos"])
        self._increment = int(state["increment"])
        self._pos_next = int(state["pos_next"])

    @staticmethod
    def _xor16(left: bytes, right: bytes) -> bytes:
        return bytes(a ^ b for a, b in zip(left, right))

    def update(self, data: bytes) -> None:
        """Fold the next piece of plaintext into the running MAC."""
        index, length = 0, len(data)

        while index < length:
            block = data[index:index + 16]
            if len(block) < 16:
                block = block + bytes(16 - len(block))

            self._mac = self._xor16(self._mac, block)
            self._mac = self._encryptor.update(self._mac)
            index += 16
            self._pos += 16

            if self._pos >= self._pos_next:
                self._macs.append(self._mac)
                self._mac = self._nonce + self._nonce
                if self._increment < 1048576:
                    self._increment += 131072
                self._pos_next += self._increment

    def digest(self) -> bytes:
        """Condense all chunk MACs into the final 8-byte MAC."""
        accumulator = bytes(16)

        for item in self._macs + [self._mac]:
            accumulator = self._encryptor.update(
                self._xor16(accumulator, item)
            )

        left = int.from_bytes(accumulator[0:4], "big") ^ int.from_bytes(
            accumulator[4:8], "big"
        )
        right = int.from_bytes(accumulator[8:12], "big") ^ int.from_bytes(
            accumulator[12:16], "big"
        )
        return left.to_bytes(4, "big") + right.to_bytes(4, "big")


def fetch_download_info(handle: str, folder_handle: str) -> tuple[str, int]:
    """Ask MEGA for a temporary download URL within the folder context.

    The file handle goes into the body as "n" and the folder share
    handle into the URL query; standalone file links to folder
    children answer with ENOENT instead.
    """
    result = mega_api(
        [{"a": "g", "g": 1, "ssl": 2, "n": handle}], folder_handle
    )

    url = result.get("g")
    if not url:
        raise RuntimeError(f"No download URL returned for {handle}")

    return url, int(result.get("s", 0))


def fetch_public_file_info(handle: str, key: bytes) -> tuple[str, int, str | None]:
    """Ask MEGA for a standalone file link's download URL and name.

    Single /file/ links are requested with "p" (public handle) and no
    folder context. Returns (temporary URL, size, decrypted name or
    None when the attributes cannot be decrypted with the link key).
    """
    result = mega_api([{"a": "g", "g": 1, "ssl": 2, "p": handle}])

    url = result.get("g")
    if not url:
        raise RuntimeError(f"No download URL returned for {handle}")

    name = None
    if result.get("at"):
        attrs = decrypt_mega_attrs(key, result["at"])
        if attrs is None:
            raise RuntimeError(
                "Could not decrypt the file attributes (wrong key in link?)"
            )
        name = attrs.get("n")
    return url, int(result.get("s", 0)), name


def _existing_dir(path: Path) -> Path:
    """Return path or its nearest existing parent (for disk_usage)."""
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def check_disk_space(
    size: int, dest_dir: Path, name: str, resume_key: str | None = None
) -> None:
    """Raise InsufficientDiskSpace when a download of `size` won't fit.

    The temp directory needs DISK_SPACE_FACTOR x size (the download and
    the copy written by the media preparation) plus MIN_FREE_SPACE.
    Resumable downloads are written to MEGA_RESUME_DIR first; bytes
    already there are subtracted, and when that directory sits on a
    different filesystem it is checked separately for the rest of the
    download.
    """
    if size <= 0:
        return

    resume_part = None
    resume_bytes = 0
    if resume_key and MEGA_RESUME:
        resume_part, _ = mega_resume_paths(resume_key)
        with suppress(OSError):
            resume_bytes = min(resume_part.stat().st_size, size)

    temp_dir = _existing_dir(dest_dir)
    checks: list[tuple[Path, int]] = []
    temp_need = DISK_SPACE_FACTOR * size
    if resume_part is not None:
        resume_dir = _existing_dir(resume_part.parent)
        if os.stat(resume_dir).st_dev == os.stat(temp_dir).st_dev:
            # Same filesystem: the finished file is renamed, not copied.
            temp_need -= resume_bytes
        else:
            checks.append((resume_dir, size - resume_bytes))
    checks.insert(0, (temp_dir, temp_need))

    for directory, need in checks:
        needed = need + MIN_FREE_SPACE
        free = shutil.disk_usage(directory).free
        if free < needed:
            resumed = (
                f", {format_size(resume_bytes)} already downloaded"
                if resume_bytes else ""
            )
            raise InsufficientDiskSpace(
                f"{directory} full: {format_size(free)} free, "
                f"{format_size(needed)} needed for {name} "
                f"({format_size(size)}{resumed}, incl. "
                f"{format_size(MIN_FREE_SPACE)} reserve)"
            )


def mega_resume_paths(node_handle: str) -> tuple[Path, Path]:
    """Return the (partial data, metadata) paths for a MEGA node."""
    safe = re.sub(r"[^\w-]", "_", node_handle)
    return (
        MEGA_RESUME_DIR / f"{safe}.part",
        MEGA_RESUME_DIR / f"{safe}.json",
    )


def mega_resume_fingerprint(key: bytes, nonce: bytes, size: int) -> str:
    """Identify the exact file content a partial download belongs to.

    The node handle alone is not enough: the key and size make sure a
    partial file is never continued with data of a different file.
    """
    return hashlib.sha256(
        key + nonce + size.to_bytes(8, "big")
    ).hexdigest()[:32]


def discard_mega_resume(part: Path, meta: Path | None) -> None:
    """Delete partial data and its metadata."""
    part.unlink(missing_ok=True)
    if meta is not None:
        meta.unlink(missing_ok=True)
        meta.with_name(meta.name + ".tmp").unlink(missing_ok=True)


def load_mega_resume(
    part: Path,
    meta: Path,
    fingerprint: str,
    size: int,
    need_mac: bool,
) -> tuple[int, dict | None]:
    """Return (offset, MAC state) of usable partial data.

    Anything inconsistent (missing metadata, other file, unaligned or
    too large offset, data shorter than recorded, missing MAC state
    while the MAC is verified) is discarded and (0, None) returned.
    """
    if not part.exists() and not meta.exists():
        return 0, None

    try:
        state = json.loads(meta.read_text())
        offset = int(state.get("offset", 0))
        valid = (
            state.get("fingerprint") == fingerprint
            and 0 < offset <= size
            and offset % 16 == 0
            and part.stat().st_size >= offset
        )
        mac_state = state.get("mac")
        if valid and need_mac:
            valid = bool(mac_state) and int(mac_state.get("pos", -1)) == offset
    except (OSError, ValueError, TypeError, AttributeError):
        valid = False

    if not valid:
        log(f"[dl] Discarding unusable partial download {part.name}")
        discard_mega_resume(part, meta)
        return 0, None

    return offset, (mac_state if need_mac else None)


def save_mega_resume(
    meta: Path,
    fingerprint: str,
    size: int,
    offset: int,
    verifier: "MegaMac | None",
    name: str,
) -> bool:
    """Persist a resume checkpoint for the first `offset` bytes.

    Only 16-byte aligned offsets whose MAC state covers exactly those
    bytes can be continued; anything else keeps the previous
    checkpoint. The caller must have flushed the data first.
    """
    if offset <= 0 or offset % 16:
        return False
    if verifier is not None and verifier.position != offset:
        return False

    data = {
        "fingerprint": fingerprint,
        "name": name,
        "size": size,
        "offset": offset,
        "mac": verifier.get_state() if verifier is not None else None,
        "updated": time.time(),
    }
    tmp = meta.with_name(meta.name + ".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(meta)
    return True


def prune_mega_resume(max_age: float | None = None) -> int:
    """Delete resume files untouched for longer than max_age seconds."""
    if max_age is None:
        max_age = MEGA_RESUME_MAX_AGE
    if not MEGA_RESUME_DIR.is_dir():
        return 0

    cutoff = time.time() - max_age
    removed = 0
    for path in MEGA_RESUME_DIR.iterdir():
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError:
            pass

    if removed:
        log(f"[dl] Pruned {removed} stale partial download file(s)")
    return removed


def download_file_blocking(
    url: str,
    key: bytes,
    nonce: bytes,
    expected_mac: bytes | None,
    size: int,
    dest: Path,
    resume_key: str | None = None,
) -> None:
    """Download one file from a temporary URL, decrypting it on the fly.

    With resume_key (the MEGA node handle) and MEGA_RESUME enabled, the
    partial data lives in MEGA_RESUME_DIR and survives cancels, network
    errors, HTTP 509 and restarts; a later call continues with a
    byte-range request ("<url>/<start>-<end>", as megajs does) and the
    persisted MAC state. Otherwise "<dest>.part" is used and deleted on
    any failure, as before.
    """
    # MAC verification guards against silent corruption; it is only
    # available with the cryptography package installed.
    verifier = None
    if expected_mac and HAVE_FAST_CRYPTO:
        verifier = MegaMac(key, nonce)

    fingerprint = mega_resume_fingerprint(key, nonce, size)
    meta: Path | None = None
    offset = 0

    if resume_key and MEGA_RESUME and size > 0:
        MEGA_RESUME_DIR.mkdir(parents=True, exist_ok=True)
        part, meta = mega_resume_paths(resume_key)
        offset, mac_state = load_mega_resume(
            part, meta, fingerprint, size, verifier is not None
        )
        if verifier is not None and mac_state:
            verifier.set_state(mac_state)
    else:
        # Write to "<dest>.part" and rename only after all checks
        # passed (same pattern as the Bunkr downloader).
        part = dest.with_name(dest.name + ".part")

    if offset:
        log(
            f"[dl] Resuming {dest.name} at {format_size(offset)} of "
            f"{format_size(size)} ({offset / size * 100:.1f}%)"
        )
    else:
        log(f"[dl] Fetching from storage server ({format_size(size)})")

    written = offset
    started = time.monotonic()
    last_log = started
    last_checkpoint = offset
    # Whether partial data may survive a failure (decided per error).
    keep_partial = False

    def checkpoint(out) -> None:
        """Flush the data, then record how much of it is valid."""
        nonlocal last_checkpoint
        out.flush()
        os.fsync(out.fileno())
        if save_mega_resume(
            meta, fingerprint, size, written, verifier, dest.name
        ):
            last_checkpoint = written

    try:
        if written < size:
            request_url = f"{url}/{offset}-{size - 1}" if offset else url
            with urllib.request.urlopen(
                request_url, timeout=MEGA_API_TIMEOUT
            ) as response:
                # A resume needs the server to honour the byte range.
                length = (
                    response.headers.get("Content-Length") if offset else None
                )
                if length is not None and (
                    int(length) != size - offset
                ):
                    raise MegaResumeInvalid(
                        f"storage server ignored the byte range "
                        f"({length} bytes for offset {offset})"
                    )

                decryptor = MegaCtrDecryptor(key, nonce, offset)
                with open(part, "r+b" if offset else "wb") as out:
                    if offset:
                        # Drop anything written after the checkpoint.
                        out.truncate(offset)
                        out.seek(offset)

                    try:
                        while True:
                            if RUNNING["cancel"]:
                                raise JobCancelled()

                            chunk = response.read(DOWNLOAD_CHUNK)
                            if not chunk:
                                break

                            plain = decryptor.update(chunk)
                            out.write(plain)
                            written += len(plain)
                            if verifier:
                                verifier.update(plain)

                            if written > size:
                                raise RuntimeError(
                                    f"Storage server sent more than "
                                    f"{size} bytes"
                                )

                            if meta is not None and (
                                written - last_checkpoint
                                >= MEGA_RESUME_CHECKPOINT
                            ):
                                checkpoint(out)

                            now = time.monotonic()
                            if now - last_log >= PROGRESS_INTERVAL:
                                last_log = now
                                speed = (written - offset) / max(
                                    now - started, 0.001
                                )
                                log(
                                    f"[dl] {dest.name}: "
                                    f"{written / size * 100:.1f}% "
                                    f"({format_size(written)} of "
                                    f"{format_size(size)}, "
                                    f"{format_size(speed)}/s)"
                                )
                    except BaseException:
                        # Record the progress made before the failure;
                        # whether it is kept is decided below.
                        if meta is not None and written > last_checkpoint:
                            with suppress(Exception):
                                checkpoint(out)
                        raise

                    if meta is not None and written > last_checkpoint:
                        checkpoint(out)

        if written < size:
            raise MegaTransientError(
                f"Incomplete download: {written} of {size} bytes"
            )

        if verifier and verifier.digest() != expected_mac:
            if offset:
                raise MegaResumeInvalid(
                    "MAC verification failed after resuming; partial "
                    "data discarded"
                )
            raise RuntimeError("MAC verification failed (download is corrupt)")

        if written != size:
            raise RuntimeError(
                f"Incomplete download: {written} of {size} bytes"
            )

        # The resume directory may sit on another filesystem.
        shutil.move(str(part), str(dest))
        if meta is not None:
            discard_mega_resume(part, meta)
    except JobCancelled:
        keep_partial = True
        raise
    except MegaTransientError:
        keep_partial = True
        raise
    except urllib.error.HTTPError as exc:
        if exc.code == 509:
            keep_partial = True
            raise MegaBandwidthLimit(
                "MEGA bandwidth limit exceeded (HTTP 509)"
            ) from None
        if exc.code == 416 and offset:
            raise MegaResumeInvalid(
                "storage server rejected the resume range (HTTP 416)"
            ) from None
        if exc.code == 403 or exc.code >= 500:
            # Expired temporary URL or server trouble: a fresh URL on
            # the next attempt continues from the partial data.
            keep_partial = True
            raise MegaTransientError(
                f"HTTP {exc.code} while downloading"
            ) from None
        raise RuntimeError(f"HTTP {exc.code} while downloading") from None
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        OSError,  # connection resets, timeouts, TLS errors
    ) as exc:
        keep_partial = True
        raise MegaTransientError(
            f"Network error while downloading: {exc}"
        ) from None
    finally:
        if meta is None or not keep_partial:
            discard_mega_resume(part, meta)
        elif part.exists():
            log(
                f"[dl] Kept partial download of {dest.name} "
                f"({format_size(last_checkpoint)} of {format_size(size)}) "
                "for resuming"
            )


async def sleep_cancellable(seconds: float) -> None:
    """Sleep in short steps and raise JobCancelled on /cancel."""
    waited = 0.0
    while waited < seconds:
        if RUNNING["cancel"]:
            raise JobCancelled()
        step = min(1.0, seconds - waited)
        await asyncio.sleep(step)
        waited += step


async def download_mega_file(
    folder_handle: str | None,
    entry: tuple[str, str, int, bytes, str | None],
    dest_dir: Path,
    fetch_info=None,
) -> Path:
    """Download one MEGA file into dest_dir and return its path.

    Transient errors and HTTP 509 are retried (resuming from the kept
    partial data); unusable partial data is dropped once and the file
    fetched from scratch. fetch_info, a blocking callable returning
    (temporary URL, size), replaces the folder-context URL request; the
    single-file path uses it. Before anything is fetched, the free disk
    space is checked (InsufficientDiskSpace stops the job).
    """
    name, handle, size, material = entry[:4]
    key = unmerge_key_mac(material)
    nonce = material[16:24] if len(material) == 32 else bytes(8)
    expected_mac = material[24:32] if len(material) == 32 else None

    # Names can carry the folder path; the file itself gets a flat name.
    dest = dest_dir / (Path(name).name or handle)

    check_disk_space(size, dest_dir, dest.name, handle)

    if fetch_info is None:
        def fetch_info():
            return fetch_download_info(handle, folder_handle)

    attempt = 0
    restarted = False
    while True:
        attempt += 1
        try:
            url, info_size = await asyncio.to_thread(fetch_info)
            await asyncio.to_thread(
                download_file_blocking,
                url,
                key,
                nonce,
                expected_mac,
                info_size or size,
                dest,
                handle,
            )
            return dest
        except JobCancelled:
            raise
        except MegaResumeInvalid as exc:
            if restarted:
                raise RuntimeError(str(exc)) from None
            restarted = True
            attempt -= 1
            log(f"[dl] {exc}; downloading {dest.name} from scratch")
        except MegaBandwidthLimit as exc:
            if attempt >= MEGA_DOWNLOAD_RETRIES:
                raise

            log(
                f"[dl] {exc}; retrying in {BANDWIDTH_RETRY_DELAY}s "
                f"(attempt {attempt + 1}/{MEGA_DOWNLOAD_RETRIES})"
            )
            await sleep_cancellable(BANDWIDTH_RETRY_DELAY)
        except MegaTransientError as exc:
            if attempt >= MEGA_DOWNLOAD_RETRIES:
                raise

            log(
                f"[dl] {exc}; retrying in {MEGA_NETWORK_RETRY_DELAY}s "
                f"(attempt {attempt + 1}/{MEGA_DOWNLOAD_RETRIES})"
            )
            await sleep_cancellable(MEGA_NETWORK_RETRY_DELAY)


async def iter_single_file(
    link: str,
    temp_dir: Path,
    job_id: int,
    stats: dict | None = None,
    file_folders: dict[str, str | None] | None = None,
    force_dupes: bool = False,
    file_handles: dict[str, str] | None = None,
):
    """Download a standalone /file/<handle>#<key> link (yields <= 1 file).

    Uses the bot's own downloader, so single files get the same
    duplicate pre-check (node handle, then name and size), resume,
    MAC verification, retries and disk space check as folder files.
    The 32-byte link key has the same layout as folder file keys:
    AES key merged with nonce (bytes 16-24) and MAC (bytes 24-32).
    megadl remains the fallback for legacy "#!" links, malformed keys
    and failed API requests. HTTP 509 still raises MegaBandwidthLimit.
    """
    match = re.search(r"/file/([\w-]+)#([\w-]+)", link)
    material = None
    if match:
        try:
            material = mega_d64(match.group(2))
        except ValueError:
            material = None

    if material is None or len(material) != 32:
        log("[dl] Legacy or unsupported file link; using megadl")
        for path in await run_megadl(link, temp_dir):
            yield path
        return

    handle = match.group(1)
    key = unmerge_key_mac(material)
    if MEGA_RESUME:
        await asyncio.to_thread(prune_mega_resume)

    try:
        url, size, name = await asyncio.to_thread(
            fetch_public_file_info, handle, key
        )
    except (JobCancelled, MegaBandwidthLimit):
        raise
    except Exception as exc:
        log(f"[dl] File info request failed ({exc}); falling back to megadl")
        for path in await run_megadl(link, temp_dir):
            yield path
        return

    name = name or handle
    progress = JOB_PROGRESS.setdefault(job_id, {})
    progress.update(total=1, current=1, current_name=Path(name).name)

    if not force_dupes and stats is not None and size:
        reason = None
        if seen_by_node(handle):
            reason = ("node", "same file node")
        elif seen_by_name_size(Path(name).name, size):
            reason = ("name/size", "same name and size")
        if reason:
            stats["skipped_pre"] = stats.get("skipped_pre", 0) + 1
            progress["skipped"] = progress.get("skipped", 0) + 1
            mark_skipped(Path(name).name, size, reason[0], job_id)
            log(f"[dl] Skipping {name}: already uploaded ({reason[1]})")
            return

    log(f"[dl] Downloading {name} ({format_size(size)})")
    sub_dir = temp_dir / "file0001"
    sub_dir.mkdir(exist_ok=True)

    # The first attempt reuses the URL fetched above; retries ask again.
    prefetched = [(url, size)]

    def fetch_info() -> tuple[str, int]:
        if prefetched:
            return prefetched.pop()
        fresh_url, fresh_size, _ = fetch_public_file_info(handle, key)
        return fresh_url, fresh_size

    path = await download_mega_file(
        None, (name, handle, size, material, None), sub_dir, fetch_info
    )

    if file_folders is not None:
        file_folders[str(path)] = None
    if file_handles is not None:
        file_handles[str(path)] = handle

    yield path


async def iter_download(
    link: str,
    temp_dir: Path,
    job_id: int,
    stats: dict | None = None,
    file_folders: dict[str, str | None] | None = None,
    force_dupes: bool = False,
    file_delay: float = 0.0,
    file_handles: dict[str, str] | None = None,
):
    """Download a MEGA link file by file, yielding each file as it arrives.

    Folder links are resolved through the public MEGA API and every
    file is downloaded with the folder as context, so each file can be
    uploaded immediately after its download and disk usage stays
    bounded by the largest single file. If the folder cannot be
    listed, everything is downloaded in one bulk megadl run instead.
    Single file links use the same downloader (see iter_single_file);
    megadl is only their fallback. The optional
    stats dict collects files skipped before any download; job_id is
    stored with every recorded skip event. file_folders preserves the
    original folder path for per-folder topic routing in the worker,
    file_handles the MEGA node handle for the pre-download duplicate
    check of later jobs. With force_dupes set, the duplicate checks are bypassed so files
    already uploaded in earlier jobs are downloaded and sent again.
    file_delay overrides the per-file pacing; the automatic re-queue
    after an API error -16 abort uses it to slow down each attempt.
    A MegaBandwidthLimit (HTTP 509) is never treated as a single-file
    failure: the whole job aborts and is re-queued with a wait, so
    already uploaded files stay recorded and are skipped on resume.
    """
    if RUNNING["cancel"]:
        raise JobCancelled()

    if link_kind(link) == "file":
        async with aclosing(iter_single_file(
            link, temp_dir, job_id, stats, file_folders, force_dupes,
            file_handles,
        )) as downloads:
            async for path in downloads:
                yield path

        return

    match = re.search(r"/folder/([\w-]+)#([\w-]+)", link)
    folder_handle = match.group(1) if match else None
    if MEGA_RESUME:
        await asyncio.to_thread(prune_mega_resume)

    entries = await list_mega_files(link)

    if entries is None or folder_handle is None:
        # Bulk fallback: uploads only start after the full download.
        for path in await run_megadl(link, temp_dir):
            yield path

        return

    total = len(entries)
    failed = 0
    fail_streak = 0
    throttle_streak = 0
    throttle_cooldowns = 0
    pace_delay = file_delay if file_delay > 0 else MEGA_FILE_DELAY
    progress = JOB_PROGRESS.setdefault(job_id, {})
    progress.update(total=total, current=0, current_name="")

    for index, entry in enumerate(entries, start=1):
        if RUNNING["cancel"]:
            raise JobCancelled()

        name, node_handle, size, _ = entry[:4]

        # Skip known duplicates before spending any download bandwidth:
        # first by the file's MEGA node handle (stable across renames),
        # then by name and size from the folder listing. The -dupes
        # flag turns this off for the whole job.
        if not force_dupes and stats is not None and size:
            if node_handle and seen_by_node(node_handle):
                stats["skipped_pre"] = stats.get("skipped_pre", 0) + 1
                progress["skipped"] = progress.get("skipped", 0) + 1
                mark_skipped(Path(name).name, size, "node", job_id)
                log(
                    f"[dl] ({index}/{total}) Skipping {name}: "
                    "already uploaded (same file node)"
                )
                continue

            if seen_by_name_size(Path(name).name, size):
                stats["skipped_pre"] = stats.get("skipped_pre", 0) + 1
                progress["skipped"] = progress.get("skipped", 0) + 1
                mark_skipped(Path(name).name, size, "name/size", job_id)
                log(
                    f"[dl] ({index}/{total}) Skipping {name}: "
                    "already uploaded (same name and size)"
                )
                continue

        # Gentle pacing so huge shares do not trip MEGA rate limits.
        # After a -16 the delay doubles per failure (capped), so a
        # throttled share is polled ever more slowly.
        if pace_delay > 0:
            await asyncio.sleep(pace_delay)

        size_text = f" ({format_size(size)})" if size else ""
        log(f"[dl] ({index}/{total}) Downloading {name}{size_text}")
        progress["current"] = index
        progress["current_name"] = Path(name).name

        # Fresh subfolder per file so exactly one file accumulates.
        sub_dir = temp_dir / f"file{index:04d}"
        sub_dir.mkdir()

        try:
            path = await download_mega_file(folder_handle, entry, sub_dir)
        except JobCancelled:
            raise
        except InsufficientDiskSpace:
            # The next files would not fit either: stop the whole job.
            raise
        except MegaBandwidthLimit:
            # MEGA's bandwidth quota (HTTP 509) is exhausted. Waiting
            # 60s and moving on to the next file would fail the same
            # way for every remaining file of the folder, so abort the
            # whole job instead: the worker re-queues it with a wait
            # long enough for the quota to reset.
            raise
        except MegaThrottled as exc:
            # MEGA answered -16. Skipping to the next file only hammers
            # the API with the same request, so slow down, probe the
            # share and stop the job when the throttle does not lift.
            failed += 1
            fail_streak += 1
            throttle_streak += 1
            progress["failed"] = progress.get("failed", 0) + 1
            pace_delay = min(10.0, max(pace_delay * 2, 1.0))
            log(f"[dl] Download failed for {name}: {exc}")

            if throttle_streak < MEGA_FAIL_STREAK:
                continue

            # Is the share still listable at all, or is API access to
            # it throttled as a whole?
            log(
                f"[dl] {throttle_streak} downloads failed with -16 in "
                "a row; probing the share handle"
            )
            try:
                await asyncio.to_thread(
                    request_folder_nodes, folder_handle
                )
            except Exception as probe_exc:
                raise MegaJobAborted(
                    "MEGA keeps answering -16 for this share and the "
                    f"share handle no longer lists ({probe_exc}). "
                    "Stopping the job instead of hammering the API."
                ) from None

            if MEGA_COOLDOWN <= 0 or throttle_cooldowns >= 2:
                raise MegaJobAborted(
                    "MEGA keeps answering -16 for this share even "
                    "though it still lists. Stopping the job after "
                    f"{throttle_cooldowns} cooldown(s)."
                ) from None

            throttle_cooldowns += 1
            log(
                f"[dl] Share still lists; cooling down for "
                f"{int(MEGA_COOLDOWN)}s before continuing "
                f"(cooldown {throttle_cooldowns}/2)"
            )
            waited = 0.0
            while waited < MEGA_COOLDOWN:
                if RUNNING["cancel"]:
                    raise JobCancelled()

                step = min(1.0, MEGA_COOLDOWN - waited)
                await asyncio.sleep(step)
                waited += step

            fail_streak = 0
            throttle_streak = 0
            continue
        except Exception as exc:
            # One broken file should not kill the whole folder job.
            failed += 1
            fail_streak += 1
            progress["failed"] = progress.get("failed", 0) + 1
            log(f"[dl] Download failed for {name}: {exc}")

            # A run of consecutive failures means MEGA is having
            # trouble; pushing on would only hammer the API harder,
            # so pause and cool down instead.
            if fail_streak >= MEGA_FAIL_STREAK and MEGA_COOLDOWN > 0:
                log(
                    f"[dl] {fail_streak} downloads failed in a row; "
                    f"cooling down for {int(MEGA_COOLDOWN)}s before "
                    "continuing"
                )
                waited = 0.0
                while waited < MEGA_COOLDOWN:
                    if RUNNING["cancel"]:
                        raise JobCancelled()

                    step = min(1.0, MEGA_COOLDOWN - waited)
                    await asyncio.sleep(step)
                    waited += step

                fail_streak = 0

            continue

        fail_streak = 0
        throttle_streak = 0
        pace_delay = MEGA_FILE_DELAY

        if file_folders is not None:
            file_folders[str(path)] = (
                entry[4] if len(entry) > 4 else mega_folder_for_file(name)
            )

        if file_handles is not None:
            file_handles[str(path)] = node_handle

        yield path

    if failed:
        log(f"[dl] {failed} of {total} file(s) failed")


# TemporaryDirectory names: "tmp" plus random lowercase letters,
# digits and underscores.
STALE_TEMP_REGEX = re.compile(r"tmp[a-z0-9_]+")


def cleanup_stale_temp_dirs() -> tuple[int, int]:
    """Remove job temp directories left behind by a crash or kill.

    Only runs with an explicit WORK_DIR, only at startup (before the
    worker starts, so no job can own one), and only touches "tmp*"
    directories directly inside WORK_DIR. MEGA_RESUME_DIR (and any
    directory containing it), symlinks, files and the system temporary
    directory are never removed. Returns (directories removed, bytes
    freed).
    """
    if not WORK_DIR:
        return 0, 0

    root = Path(WORK_DIR).resolve()
    system_temp = {
        Path(tempfile.gettempdir()).resolve(),
        Path("/tmp").resolve(),
        Path("/var/tmp").resolve(),
    }
    if root in system_temp or not root.is_dir():
        return 0, 0

    resume_dir = MEGA_RESUME_DIR.resolve()
    removed = 0
    freed = 0
    for child in root.iterdir():
        try:
            if (
                not STALE_TEMP_REGEX.fullmatch(child.name)
                or child.is_symlink()
                or not child.is_dir()
            ):
                continue
            resolved = child.resolve()
            if resolved == resume_dir or resolved in resume_dir.parents:
                continue
            size = sum(
                entry.stat().st_size
                for entry in resolved.rglob("*")
                if entry.is_file() and not entry.is_symlink()
            )
            shutil.rmtree(resolved)
        except OSError as exc:
            log(f"[bot] Could not remove stale temp dir {child}: {exc}")
            continue
        removed += 1
        freed += size
        log(f"[bot] Removed stale temp dir {child} ({format_size(size)})")

    if removed:
        log(
            f"[bot] Cleaned up {removed} stale temp dir(s), "
            f"{format_size(freed)} freed"
        )
    return removed, freed


def write_part(src: Path, dst: Path, index: int) -> None:
    """Write one zero-based part of a file to a new file."""
    remaining = PART_SIZE

    with open(src, "rb") as source, open(dst, "wb") as destination:
        source.seek(index * PART_SIZE)

        while remaining > 0:
            buffer = source.read(min(COPY_BUF, remaining))
            if not buffer:
                break

            destination.write(buffer)
            remaining -= len(buffer)


def make_progress_logger(name: str):
    """Log upload percentage, bytes and average speed at timed intervals."""
    started = time.monotonic()
    last_logged = started
    last_current = 0
    completed = False

    def callback(current: int, total: int) -> None:
        nonlocal started, last_logged, last_current, completed
        if RUNNING["cancel"]:
            raise JobCancelled()

        now = time.monotonic()
        if current < last_current:
            started = now
            last_logged = now
            completed = False
        last_current = current
        finished = total > 0 and current >= total
        if completed and finished:
            return
        if not finished and now - last_logged < PROGRESS_INTERVAL:
            return

        speed = current / max(now - started, 0.001)
        percent = min(current / total * 100, 100.0) if total else 0.0
        total_text = format_size(total) if total else "unknown"
        log(
            f"[ul] {name}: {percent:.1f}% "
            f"({format_size(current)} of {total_text}, "
            f"{format_size(speed)}/s)"
        )
        last_logged = now
        completed = finished

    return callback


# Errors worth another upload attempt: connection problems, timeouts
# and Telegram-side server failures. File-level OSErrors (missing file,
# permissions) are excluded in send_with_retry().
UPLOAD_TRANSIENT_ERRORS = (
    ConnectionError,
    TimeoutError,
    asyncio.TimeoutError,
    OSError,
    ServerError,  # includes RpcCallFailError
    RpcCallFailError,
    TimedOutError,
)
UPLOAD_PERMANENT_OS_ERRORS = (
    FileNotFoundError,
    PermissionError,
    IsADirectoryError,
    NotADirectoryError,
)


async def send_with_retry(path: Path, **kwargs) -> None:
    """client.send_file() with flood-wait handling and network retries.

    FloodWaitError (raised by Telethon for waits above its 60s
    flood_sleep_threshold): wait the requested seconds plus a margin,
    cancellably, then send again; waits above UPLOAD_FLOOD_WAIT_MAX
    fail instead. Transient network / server errors are retried up to
    UPLOAD_RETRIES attempts, UPLOAD_RETRY_DELAY seconds apart. All other
    errors, and JobCancelled (also from the progress callback), are
    raised immediately.
    """
    attempt = 0
    floods = 0
    while True:
        if RUNNING["cancel"]:
            raise JobCancelled()
        attempt += 1
        try:
            await client.send_file(CHAT_ID, str(path), **kwargs)
            return
        except JobCancelled:
            raise
        except FloodWaitError as exc:
            # A flood wait is not a failed attempt.
            attempt -= 1
            floods += 1
            seconds = max(int(getattr(exc, "seconds", 0) or 0), 0)
            if UPLOAD_FLOOD_WAIT_MAX > 0 and seconds > UPLOAD_FLOOD_WAIT_MAX:
                raise RuntimeError(
                    f"Telegram flood wait of {seconds}s for {path.name} "
                    f"exceeds UPLOAD_FLOOD_WAIT_MAX "
                    f"({int(UPLOAD_FLOOD_WAIT_MAX)}s); use /retry later"
                ) from None
            if floods > UPLOAD_FLOOD_RETRIES:
                raise
            wait = seconds + UPLOAD_FLOOD_WAIT_MARGIN
            log(
                f"[ul] Telegram flood wait for {path.name}: waiting "
                f"{wait}s before retrying (flood wait {floods}/"
                f"{UPLOAD_FLOOD_RETRIES})"
            )
            await sleep_cancellable(wait)
        except UPLOAD_TRANSIENT_ERRORS as exc:
            if isinstance(exc, UPLOAD_PERMANENT_OS_ERRORS):
                raise
            if attempt >= UPLOAD_RETRIES:
                raise
            log(
                f"[ul] Upload of {path.name} failed "
                f"({type(exc).__name__}: {exc}); retrying in "
                f"{UPLOAD_RETRY_DELAY:g}s (attempt {attempt + 1}/"
                f"{UPLOAD_RETRIES})"
            )
            await sleep_cancellable(UPLOAD_RETRY_DELAY)


async def send_file(path: Path, topic_id: int | None, progress=None) -> None:
    """Send prepared media; retry Telegram processing errors as a document.

    Both sends go through send_with_retry(), so flood waits and
    transient network errors are handled for media, the document
    fallback and split parts alike.
    """
    if RUNNING["cancel"]:
        raise JobCancelled()
    try:
        await send_with_retry(
            path,
            force_document=False,
            supports_streaming=path.suffix.lower() == ".mp4",
            reply_to=topic_id,
            progress_callback=progress,
        )
    except (
        PhotoInvalidDimensionsError,
        PhotoSaveFileInvalidError,
        ImageProcessFailedError,
        MediaInvalidError,
    ) as error:
        if RUNNING["cancel"]:
            raise JobCancelled()
        log(
            f"[ul] Telegram rejected {path.name} as media "
            f"({type(error).__name__}); retrying prepared file as document"
        )
        await send_with_retry(
            path,
            force_document=True,
            reply_to=topic_id,
            progress_callback=make_progress_logger(path.name),
        )


# ---- Large-file prompt ----
# Files larger than PART_SIZE are normally split into parts before
# upload. For every such file the user now gets a one-time question:
#
#   Try unsplit     - one upload attempt as a single document. Telegram
#                    rejects files beyond its own upload limit (about
#                    2 GB for this bot account); the bot then falls
#                    back to splitting automatically.
#   Split (N)       - the previous behaviour: N parts of PART_SIZE.
#   Keep on server  - the unsplit file is moved into KEPT_DIR on the
#                    data volume and nothing is uploaded.
#
# Not answering within LARGE_FILE_ASK_TIMEOUT seconds splits the file,
# so an unattended queue keeps working as before.

ASK_KEEP = "keep"
ASK_SPLIT = "split"
ASK_DISK = "disk"

ASK_ANSWERS = {
    ASK_KEEP: "Will try to send it unsplit.",
    ASK_SPLIT: "Will split the file.",
    ASK_DISK: "Will keep it on the server.",
}

# Pending questions, keyed by the token embedded in the callback data.
PENDING_FILE_ASKS: dict[int, asyncio.Future] = {}


@client.on(events.CallbackQuery(pattern=b"^lfile:"))
async def on_large_file_choice(event) -> None:
    """Resolve a pending large-file question from an authorized user."""
    if not is_allowed(event):
        await event.answer("Not allowed.")
        return

    try:
        _, token_text, choice = event.data.decode().split(":")
        token = int(token_text)
    except (ValueError, UnicodeDecodeError):
        await event.answer()
        return

    future = PENDING_FILE_ASKS.get(token)
    if future is None or future.done() or choice not in ASK_ANSWERS:
        await event.answer("This question is no longer active.")
        return

    future.set_result(choice)
    await event.answer(ASK_ANSWERS[choice])


async def ask_large_file(path: Path, topic_id: int | None) -> str:
    """Ask whether to send unsplit, split, or keep the file locally.

    Returns ASK_KEEP, ASK_SPLIT or ASK_DISK. A timeout, a cancel or
    any problem with the prompt leads back to ASK_SPLIT, the behaviour
    this bot had before the question existed.
    """
    token = random.getrandbits(48)
    future = asyncio.get_running_loop().create_future()
    PENDING_FILE_ASKS[token] = future

    size = path.stat().st_size
    total_parts = (size + PART_SIZE - 1) // PART_SIZE
    prompt = None

    try:
        prompt = await client.send_message(
            CHAT_ID,
            f"{path.name} ({format_size(size)}) exceeds the split "
            f"threshold ({format_size(PART_SIZE)}). What should I do?",
            reply_to=topic_id,
            link_preview=False,
            buttons=[[
                Button.inline(
                    "Try unsplit", f"lfile:{token}:{ASK_KEEP}".encode()
                ),
                Button.inline(
                    f"Split ({total_parts})",
                    f"lfile:{token}:{ASK_SPLIT}".encode(),
                ),
                Button.inline(
                    "Keep on server",
                    f"lfile:{token}:{ASK_DISK}".encode(),
                ),
            ]],
        )
    except Exception as exc:
        log(f"[ul] Large-file prompt failed for {path.name}: {exc}")
        PENDING_FILE_ASKS.pop(token, None)
        return ASK_SPLIT

    # Wait for a button press, but re-check /cancel every second so a
    # cancelled job does not linger until the full timeout.
    choice = None
    waited = 0.0
    while choice is None and waited < LARGE_FILE_ASK_TIMEOUT:
        if RUNNING["cancel"]:
            break

        try:
            choice = await asyncio.wait_for(asyncio.shield(future), 1.0)
        except asyncio.TimeoutError:
            waited += 1.0

    PENDING_FILE_ASKS.pop(token, None)

    if RUNNING["cancel"]:
        if prompt:
            try:
                await prompt.delete()
            except Exception:
                pass
        raise JobCancelled()

    if choice is None:
        # Timeout: tidy the prompt away and split, as before.
        try:
            await prompt.edit("No choice received; splitting by default.")
            spawn(
                delete_status([prompt], STATUS_DELETE_DELAY)
            )
        except Exception:
            pass

        return ASK_SPLIT

    try:
        await prompt.edit(f"{path.name}: {ASK_ANSWERS[choice]}")
        spawn(delete_status([prompt], STATUS_DELETE_DELAY))
    except Exception:
        pass

    return choice


async def upload_one(path: Path, topic_id: int | None) -> bool:
    """Upload or retain a file; return True only when sent to Telegram."""
    size = path.stat().st_size

    if size <= PART_SIZE:
        log(f"[ul] Uploading {path.name} ({format_size(size)})")
        upload_started = asyncio.get_running_loop().time()
        await send_file(path, topic_id, make_progress_logger(path.name))
        took = asyncio.get_running_loop().time() - upload_started
        speed = size / took if took > 0 else 0
        log(
            f"[ul] Uploaded {path.name} in {int(took)}s "
            f"({format_size(speed)}/s)"
        )
        return True

    total_parts = (size + PART_SIZE - 1) // PART_SIZE

    choice = ASK_SPLIT
    if LARGE_FILE_ASK_TIMEOUT > 0:
        choice = await ask_large_file(path, topic_id)

    if choice == ASK_DISK:
        KEPT_DIR.mkdir(parents=True, exist_ok=True)
        target = KEPT_DIR / path.name
        if target.exists():
            target = (
                KEPT_DIR / f"{path.stem}_{int(time.time())}{path.suffix}"
            )

        await asyncio.to_thread(shutil.move, str(path), str(target))
        log(f"[ul] Kept {path.name} unsplit at {target}")
        await notify(
            f"Kept {path.name} ({format_size(size)}) unsplit on the "
            f"server at {target}; nothing was uploaded.",
            topic_id,
        )
        return False

    if choice == ASK_KEEP:
        log(
            f"[ul] Trying unsplit upload: {path.name} "
            f"({format_size(size)})"
        )
        try:
            await send_file(path, topic_id, make_progress_logger(path.name))
            return True
        except JobCancelled:
            raise
        except Exception as exc:
            log(
                f"[ul] Unsplit upload of {path.name} failed: {exc}; "
                "falling back to splitting"
            )
            await notify(
                f"Could not upload {path.name} unsplit; splitting into "
                f"{total_parts} part(s) instead.",
                topic_id,
            )

    log(
        f"[ul] {path.name} is larger than {format_size(PART_SIZE)}, "
        f"splitting into {total_parts} part(s)"
    )

    for part_index in range(total_parts):
        part_path = path.with_name(f"{path.name}.{part_index + 1:03d}")

        log(
            f"[ul] Writing part {part_index + 1}/{total_parts} "
            f"of {path.name}"
        )
        await asyncio.to_thread(write_part, path, part_path, part_index)

        try:
            log(
                f"[ul] Uploading part {part_index + 1}/{total_parts} "
                f"({format_size(part_path.stat().st_size)})"
            )
            await send_file(
                part_path, topic_id, make_progress_logger(part_path.name)
            )
        finally:
            # Remove each temporary part after upload.
            part_path.unlink(missing_ok=True)

        log(f"[ul] Part {part_index + 1}/{total_parts} uploaded")

    return True


# ---- YouTube and Netzkino via yt-dlp ----
# /yt downloads a YouTube video (public, or private/authenticated when
# YT_COOKIES points to an exported cookies.txt). /nk downloads a movie
# page from Netzkino, for which yt-dlp ships a built-in extractor.
# ffmpeg (installed in the image) merges the best video and audio
# streams into one MP4 file.


def ytdlp_command(url: str, target: Path, max_size: int) -> list[str]:
    """Build the yt-dlp command line for one video URL."""
    # Prefer formats known to fit the size cap; the trailing "b" catches
    # sources without size information, where --max-filesize is the guard.
    format_spec = (
        f"b[filesize<{max_size}]/"
        f"bv*[filesize<{max_size}]+ba[filesize<{max_size}]/"
        f"b"
    )

    command = [
        "yt-dlp",
        "--newline",
        "--no-playlist",
        "--merge-output-format", "mp4",
        "--max-filesize", str(max_size),
        "--format", format_spec,
        "--output", str(target / "%(title).80B [%(id)s].%(ext)s"),
        "--", url,
    ]

    if YT_COOKIES:
        command[1:1] = ["--cookies", YT_COOKIES]

    return command


async def run_ytdlp(url: str, target: Path, max_size: int) -> list[Path]:
    """Download one video URL with yt-dlp and return the produced files."""
    if RUNNING["cancel"]:
        raise JobCancelled()

    log(f"[ytdl] Starting download of {url}")

    process = await asyncio.create_subprocess_exec(
        *ytdlp_command(url, target, max_size),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    RUNNING["process"] = process
    log(f"[ytdl] yt-dlp running as PID {process.pid}")

    pump = asyncio.gather(
        _pump_stream(process.stdout, "ytdl"),
        _pump_stream(process.stderr, "ytdl:err"),
    )

    try:
        output = await pump
        await process.wait()
    finally:
        RUNNING["process"] = None

    if RUNNING["cancel"]:
        raise JobCancelled()

    if process.returncode != 0:
        all_lines = output[0] + output[1]
        last_line = all_lines[-1] if all_lines else "unknown error"
        raise RuntimeError(f"yt-dlp failed: {last_line}")

    files = sorted(
        path
        for path in target.rglob("*")
        if path.is_file() and not path.name.endswith((".part", ".ytdl"))
    )

    if not files:
        raise RuntimeError("yt-dlp produced no files")

    total_size = sum(path.stat().st_size for path in files)
    log(
        f"[ytdl] Download finished: {len(files)} file(s), "
        f"{format_size(total_size)}"
    )

    return files


# ---- Bunkr via requests + BeautifulSoup ----
# /bunkr (alias /bk) queues "bk:<url>" jobs. Album pages (/a/<id>) are
# scraped for their file page links (/f/, /v/, /i/); every file page is
# scraped for the direct CDN URL, which is streamed to a temp file. The
# worker then runs the same path as MEGA files: SHA-256 duplicate check,
# local media preparation, upload into the topic, hash registration and
# temp-file removal. The blocking HTTP helpers run in worker threads.


class BunkrError(RuntimeError):
    """A Bunkr page or file could not be fetched or parsed."""


def _bunkr_host(url: str) -> str:
    """Return the lowercase host of a URL without a leading 'www.'."""
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _bunkr_site_domain(host: str) -> str | None:
    """Return the Bunkr site domain a host belongs to (e.g. bunkr.cr)."""
    for domain in BUNKR_DOMAINS:
        if host == domain or host.endswith("." + domain):
            return domain
    match = BUNKR_SITE_REGEX.search(host)
    return match.group(1) if match else None


def is_bunkr_url(url: str) -> bool:
    """Return True when the URL's host is a Bunkr site domain."""
    try:
        host = _bunkr_host(url)
    except ValueError:
        return False
    return _bunkr_site_domain(host) == host


def is_bunkr_cdn_url(url: str) -> bool:
    """Return True for a Bunkr CDN host such as cdn12.bunkr.ru."""
    try:
        host = (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    domain = _bunkr_site_domain(host)
    if not domain or host == domain:
        return False
    label = host[: -len(domain) - 1].split(".")[-1]
    return bool(BUNKR_CDN_LABEL_REGEX.match(label))


def bunkr_links_in(text: str) -> list[str]:
    """Return the Bunkr album/file URLs contained in a message text."""
    links = []
    for url in clean_urls(BUNKR_URL_REGEX.findall(text)):
        path = urlsplit(url).path
        if is_bunkr_url(url) and (
            path.startswith(BUNKR_ALBUM_PATH_PREFIX)
            or path.startswith(BUNKR_FILE_PATH_PREFIXES)
        ):
            links.append(url)
    return links


def bunkr_session():
    """Create a requests session with browser-like default headers."""
    if not HAVE_BUNKR_DEPS:
        raise BunkrError(
            "Bunkr support needs the requests and beautifulsoup4 packages: "
            "pip install requests beautifulsoup4 lxml"
        )
    session = requests.Session()
    session.headers.update({
        "User-Agent": BUNKR_USER_AGENT,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    })
    return session


def _bunkr_sleep(seconds: float) -> None:
    """Sleep in a worker thread while honouring /cancel once per second."""
    waited = 0.0
    while waited < seconds:
        if RUNNING["cancel"]:
            raise JobCancelled()
        step = min(1.0, seconds - waited)
        time.sleep(step)
        waited += step


def bunkr_request(
    session,
    url: str,
    *,
    stream: bool = False,
    headers: dict | None = None,
    what: str = "page",
):
    """GET a Bunkr URL, retrying on HTTP 429, 5xx and connection errors.

    The waits before the three retries follow BUNKR_RETRY_DELAYS
    (10s, 30s, 60s); a longer Retry-After header (capped at 5 minutes)
    wins. HTTP 403, 404 and other client errors fail immediately with a
    descriptive BunkrError. Returns the open response.
    """
    attempts = len(BUNKR_RETRY_DELAYS) + 1
    last_error = "unknown error"

    for attempt in range(attempts):
        if RUNNING["cancel"]:
            raise JobCancelled()

        retry_after = 0
        try:
            response = session.get(
                url,
                stream=stream,
                headers=headers,
                timeout=BUNKR_TIMEOUT,
                allow_redirects=True,
            )
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = f"connection error: {exc}"
        else:
            status = response.status_code
            if status == 429 or status >= 500:
                last_error = (
                    "HTTP 429 (rate limited)" if status == 429
                    else f"HTTP {status} (server error)"
                )
                header = response.headers.get("Retry-After", "")
                if header.isdigit():
                    retry_after = min(int(header), 300)
                response.close()
            elif status >= 400:
                response.close()
                if status == 403:
                    reason = "access denied (blocked IP or hotlink protection)"
                elif status == 404:
                    reason = "not found (removed or wrong link)"
                else:
                    reason = "client error"
                raise BunkrError(f"HTTP {status} for Bunkr {what} {url}: {reason}")
            else:
                return response

        if attempt < len(BUNKR_RETRY_DELAYS):
            delay = max(BUNKR_RETRY_DELAYS[attempt], retry_after)
            log(
                f"[bk] {what} request failed ({last_error}); retrying in "
                f"{delay}s ({attempt + 2}/{attempts})"
            )
            _bunkr_sleep(delay)

    raise BunkrError(
        f"Giving up on Bunkr {what} {url} after {attempts} attempts: "
        f"{last_error}"
    )


def _bunkr_soup(html: str):
    """Parse HTML with lxml when installed, else the stdlib parser."""
    try:
        return BeautifulSoup(html, "lxml")
    except FeatureNotFound:
        return BeautifulSoup(html, "html.parser")


def parse_bunkr_album(html: str, base_url: str) -> tuple[str | None, list[str]]:
    """Return (album title, file page URLs) from album page HTML.

    File page links are <a href> targets on any Bunkr domain whose path
    starts with /f/, /v/ or /i/; relative links are resolved against
    base_url, query strings dropped and duplicates removed in order.
    """
    soup = _bunkr_soup(html)
    links: list[str] = []
    seen: set[str] = set()

    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, anchor["href"].strip())
        parts = urlsplit(href)
        if parts.scheme not in ("http", "https") or not is_bunkr_url(href):
            continue
        if not parts.path.startswith(BUNKR_FILE_PATH_PREFIXES):
            continue
        # Skip JS template fragments such as "/f/' + file.slug + '".
        if not re.fullmatch(r"/[fvi]/[\w.-]+/?", unquote(parts.path)):
            continue
        clean = urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
        if clean not in seen:
            seen.add(clean)
            links.append(clean)

    title = None
    og_title = soup.find("meta", attrs={"property": "og:title"})
    if og_title and og_title.get("content", "").strip():
        title = og_title["content"].strip()
    elif soup.h1 and soup.h1.get_text(strip=True):
        title = soup.h1.get_text(" ", strip=True)
    elif soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(" ", strip=True)
    if title:
        # Drop a trailing site name such as " | Bunkr" or " - Bunkr".
        title = re.sub(r"\s*[|\-\u2013]\s*bunkr\b.*$", "", title, flags=re.I)
        title = title.strip() or None

    return title, links


def bunkr_filename(cdn_url: str, fallback: str = "bunkr_file") -> str:
    """Return a safe local filename from the last URL path segment."""
    name = unquote(urlsplit(cdn_url).path.rsplit("/", 1)[-1])
    name = re.sub(r"[\x00-\x1f/\\]+", "_", name).strip(" .")
    if not name:
        name = re.sub(r"[^\w.-]+", "_", fallback).strip("._") or "bunkr_file"
    # Keep the name within common filesystem limits, preserving the suffix.
    if len(name.encode("utf-8")) > 200:
        suffix = Path(name).suffix[:16]
        stem = name[: len(name) - len(suffix)]
        stem = stem.encode("utf-8")[: 200 - len(suffix.encode())]
        name = stem.decode("utf-8", errors="ignore") + suffix
    return name


# Inline JS variables on current Bunkr file pages, e.g.
#   var jsCDN = "https:\/\/c2ru-b.cdn.cr\/storage\/media\/<uuid>.mp4";
#   var signUrl = "https://glb-apisign.cdn.cr/sign";
BUNKR_JS_VAR_REGEX = re.compile(
    r"\bvar\s+(jsCDN|signUrl)\s*=\s*[\"']([^\"']+)[\"']"
)


def parse_bunkr_js_vars(html: str) -> dict[str, str]:
    """Return the jsCDN / signUrl values embedded in a file page."""
    return {
        name: value.replace("\\/", "/").strip()
        for name, value in BUNKR_JS_VAR_REGEX.findall(html)
        if value.strip()
    }


def _bunkr_page_title_name(soup, cdn_url: str) -> str | None:
    """Return the original filename from og:title when it looks like one.

    The CDN path only holds a UUID; og:title carries the uploader's file
    name (e.g. "Katjak.mp4"). It is used when it has the same suffix as
    the CDN file, so a page title never replaces the real extension.
    """
    meta = soup.find("meta", attrs={"property": "og:title"})
    title = (meta.get("content", "") if meta else "").strip()
    cdn_suffix = Path(urlsplit(cdn_url).path).suffix.lower()
    if title and cdn_suffix and Path(title).suffix.lower() == cdn_suffix:
        return bunkr_filename("x/" + quote(title, safe=""))
    return None


def parse_bunkr_file_page(html: str, page_url: str) -> tuple[str, str]:
    """Return (direct CDN URL, filename) from file page HTML.

    Order: the jsCDN variable of the current player script (needs a
    signature, see sign_bunkr_url), then <source src> (older player),
    <a href> pointing to a Bunkr CDN host (download button), then
    <video src> and <img src> on a CDN host (image pages). Raises
    BunkrError when nothing fits.
    """
    soup = _bunkr_soup(html)
    slug = urlsplit(page_url).path.rstrip("/").rsplit("/", 1)[-1]

    js_cdn = parse_bunkr_js_vars(html).get("jsCDN")
    if js_cdn and urlsplit(js_cdn).scheme in ("http", "https"):
        name = _bunkr_page_title_name(soup, js_cdn)
        return js_cdn, name or bunkr_filename(js_cdn, fallback=slug)

    def absolute(value: str) -> str | None:
        url = urljoin(page_url, value.strip())
        return url if urlsplit(url).scheme in ("http", "https") else None

    candidates: list[str] = []
    for tag in soup.find_all("source", src=True):
        url = absolute(tag["src"])
        if url:
            candidates.append(url)
    for tag in soup.find_all("a", href=True):
        url = absolute(tag["href"])
        if url and is_bunkr_cdn_url(url):
            candidates.append(url)
    for tag in soup.find_all(["video", "img"], src=True):
        url = absolute(tag["src"])
        if url and is_bunkr_cdn_url(url):
            candidates.append(url)

    if not candidates:
        raise BunkrError(
            f"No direct download link found on {page_url}: no <source src> "
            "and no CDN <a href>. The file may be removed or the page may "
            "need JavaScript."
        )

    cdn_url = candidates[0]
    return cdn_url, bunkr_filename(cdn_url, fallback=slug)


def sign_bunkr_url(
    session, raw_url: str, sign_url: str, referer: str | None = None
) -> str:
    """Add the token/ex parameters the Bunkr CDN requires.

    Mirrors getSignedUrl() of the Bunkr player: GET <signUrl>?path=<cdn
    path> returns {"token": ..., "ex": ...}; both are appended to the
    CDN URL. Unsigned requests are answered with HTTP 403.
    """
    parts = urlsplit(raw_url)
    response = bunkr_request(
        session,
        f"{sign_url}?path={quote(unquote(parts.path), safe='')}",
        headers={
            "Referer": referer or _bunkr_referer(raw_url),
            "Accept": "application/json",
        },
        what="signature",
    )
    with response:
        try:
            data = response.json()
            token, expires = data["token"], data["ex"]
        except (ValueError, KeyError, TypeError) as exc:
            raise BunkrError(
                f"Unexpected Bunkr signature response for {raw_url}: {exc}"
            ) from exc
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parts.query) if k not in ("token", "ex")]
        + [("token", str(token)), ("ex", str(expires))]
    )
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def fetch_bunkr_album(
    album_url: str, session=None
) -> tuple[str | None, list[str]]:
    """Download an album page and return (title, file page URLs)."""
    own_session = session is None
    session = session or bunkr_session()
    try:
        response = bunkr_request(session, album_url, what="album page")
        with response:
            title, links = parse_bunkr_album(response.text, response.url)
    finally:
        if own_session:
            session.close()

    if not links:
        raise BunkrError(
            f"Bunkr album {album_url} contains no file links (empty, removed "
            "or rendered by JavaScript)"
        )
    log(f"[bk] Album '{title or album_url}': {len(links)} file page(s)")
    return title, links


def extract_bunkr_links(album_or_file_url: str, session=None) -> list[str]:
    """Return the file page URLs behind a Bunkr album or file URL.

    /a/<id> album pages are fetched and scraped; /f/, /v/ and /i/ file
    page URLs are returned unchanged as a one-element list.
    """
    path = urlsplit(album_or_file_url).path
    if path.startswith(BUNKR_FILE_PATH_PREFIXES):
        return [album_or_file_url]
    if path.startswith(BUNKR_ALBUM_PATH_PREFIX):
        return fetch_bunkr_album(album_or_file_url, session)[1]
    raise BunkrError(
        f"Unsupported Bunkr URL {album_or_file_url}: expected /a/, /f/, /v/ "
        "or /i/"
    )


def get_bunkr_direct_url(file_page_url: str, session=None) -> tuple[str, str]:
    """Fetch a Bunkr file page and return (direct CDN URL, filename)."""
    own_session = session is None
    session = session or bunkr_session()
    try:
        response = bunkr_request(session, file_page_url, what="file page")
        with response:
            html, page_url = response.text, response.url
        cdn_url, filename = parse_bunkr_file_page(html, page_url)
        # The current player only streams signed URLs; sign right before
        # the download so the short-lived token is still valid.
        sign_url = parse_bunkr_js_vars(html).get("signUrl")
        if sign_url and cdn_url == parse_bunkr_js_vars(html).get("jsCDN"):
            cdn_url = sign_bunkr_url(
                session, cdn_url, sign_url, _bunkr_referer(page_url)
            )
        return cdn_url, filename
    finally:
        if own_session:
            session.close()


def _bunkr_referer(url: str) -> str:
    """Return the base URL of the Bunkr site that a URL belongs to."""
    domain = _bunkr_site_domain(_bunkr_host(url))
    return f"https://{domain}/" if domain else "https://bunkr.si/"


def download_bunkr_file(
    cdn_url: str,
    dest_path: str,
    progress_cb=None,
    referer: str | None = None,
    session=None,
) -> None:
    """Stream a Bunkr CDN file to dest_path in BUNKR_CHUNK-sized chunks.

    Sends the browser User-Agent plus a Referer of the Bunkr page base
    (CDNs reject hotlinks without it). progress_cb(bytes_done, total)
    is called per chunk; total is 0 when the server sends no length.
    Data goes to "<dest>.part" first and is renamed only when complete,
    so a failed download never leaves a truncated file behind.
    """
    dest = Path(dest_path)
    part = dest.with_name(dest.name + ".part")
    own_session = session is None
    session = session or bunkr_session()
    headers = {"Referer": referer or _bunkr_referer(cdn_url)}

    try:
        response = bunkr_request(
            session, cdn_url, stream=True, headers=headers, what="file"
        )
        with response:
            content_type = response.headers.get("Content-Type", "").lower()
            if content_type.startswith("text/html"):
                raise BunkrError(
                    f"CDN returned an HTML page instead of the file for "
                    f"{cdn_url} (maintenance or blocked)"
                )
            # Content-Length is only comparable to the decoded byte count
            # when the body is not transfer-compressed.
            total = int(response.headers.get("Content-Length") or 0)
            if response.headers.get("Content-Encoding"):
                total = 0

            written = 0
            with open(part, "wb") as out:
                for chunk in response.iter_content(BUNKR_CHUNK):
                    if RUNNING["cancel"]:
                        raise JobCancelled()
                    if not chunk:
                        continue
                    out.write(chunk)
                    written += len(chunk)
                    if progress_cb:
                        progress_cb(written, total)

        if written == 0:
            raise BunkrError(f"CDN sent an empty file for {cdn_url}")
        if total and written != total:
            raise BunkrError(
                f"Incomplete Bunkr download: {written} of {total} bytes"
            )
        part.replace(dest)
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        if own_session:
            session.close()


def make_download_logger(name: str, tag: str = "bk"):
    """Log download progress at most every PROGRESS_INTERVAL seconds."""
    started = time.monotonic()
    last_logged = started

    def callback(current: int, total: int) -> None:
        nonlocal last_logged
        now = time.monotonic()
        if now - last_logged < PROGRESS_INTERVAL:
            return
        last_logged = now
        speed = current / max(now - started, 0.001)
        if total:
            log(
                f"[{tag}] {name}: {current / total * 100:.1f}% "
                f"({format_size(current)} of {format_size(total)}, "
                f"{format_size(speed)}/s)"
            )
        else:
            log(f"[{tag}] {name}: {format_size(current)} ({format_size(speed)}/s)")

    return callback


async def iter_bunkr(
    link: str,
    temp_dir: Path,
    job_id: int,
    file_folders: dict[str, str | None] | None = None,
    status_cb=None,
):
    """Download a Bunkr album or file one by one, yielding each file.

    Mirrors iter_download for MEGA folders: one fresh subfolder per
    file keeps disk usage bounded by the largest file, a broken file
    does not stop the album, and JOB_PROGRESS feeds /queue. For album
    links, file_folders maps each path to the album title so the worker
    can route it into a per-album topic. status_cb(text) is awaited
    with "Bunkr: <n>/<total> - <filename>" before each download.
    """
    if RUNNING["cancel"]:
        raise JobCancelled()

    session = bunkr_session()
    try:
        album_title = None
        if urlsplit(link).path.startswith(BUNKR_ALBUM_PATH_PREFIX):
            album_title, pages = await asyncio.to_thread(
                fetch_bunkr_album, link, session
            )
        else:
            pages = await asyncio.to_thread(extract_bunkr_links, link, session)

        total = len(pages)
        failed = 0
        last_error = ""
        progress = JOB_PROGRESS.setdefault(job_id, {})
        progress.update(total=total, current=0, current_name="")

        for index, page_url in enumerate(pages, start=1):
            if RUNNING["cancel"]:
                raise JobCancelled()
            if index > 1 and BUNKR_FILE_DELAY > 0:
                await asyncio.sleep(BUNKR_FILE_DELAY)

            progress["current"] = index
            try:
                cdn_url, filename = await asyncio.to_thread(
                    get_bunkr_direct_url, page_url, session
                )
                progress["current_name"] = filename
                log(f"[bk] ({index}/{total}) Downloading {filename}")
                if status_cb is not None:
                    await status_cb(f"Bunkr: {index}/{total} \u2014 {filename}")

                sub_dir = temp_dir / f"bunkr{index:04d}"
                sub_dir.mkdir()
                dest = sub_dir / filename
                await asyncio.to_thread(
                    download_bunkr_file,
                    cdn_url,
                    str(dest),
                    make_download_logger(filename),
                    _bunkr_referer(page_url),
                    session,
                )
            except JobCancelled:
                raise
            except Exception as exc:
                # One broken file should not kill the whole album job.
                failed += 1
                last_error = str(exc) or type(exc).__name__
                progress["failed"] = progress.get("failed", 0) + 1
                log(f"[bk] ({index}/{total}) Failed {page_url}: {last_error}")
                continue

            log(
                f"[bk] ({index}/{total}) Downloaded {filename} "
                f"({format_size(dest.stat().st_size)})"
            )
            if file_folders is not None:
                file_folders[str(dest)] = album_title
            yield dest

        if failed:
            log(f"[bk] {failed} of {total} file(s) failed")
            if failed == total:
                raise BunkrError(
                    f"All {total} Bunkr file(s) failed; last error: "
                    f"{last_error}"
                )
    finally:
        session.close()


# ---- Helpers ----
def get_topic_id(event) -> int | None:
    """Get the forum topic ID from the command message, if available."""
    reply = event.message.reply_to

    if not reply:
        return None

    top_id = getattr(reply, "reply_to_top_id", None)
    if top_id:
        return top_id

    if getattr(reply, "forum_topic", False):
        return getattr(reply, "reply_to_msg_id", None)

    return None


def is_allowed(event) -> bool:
    """Allow only configured users in the configured group."""
    return event.chat_id == CHAT_ID and event.sender_id in ALLOWED_USERS


def clamp_topic_name(name: str) -> str:
    """Trim a topic title to Telegram's limit of 128 UTF-8 bytes."""
    return name.encode("utf-8")[:128].decode("utf-8", errors="ignore")


def topic_error_hint(error: str) -> str:
    """Return a human-friendly hint for a failed topic creation."""
    if "CHANNEL_FORUM_MISSING" in error:
        return (
            "Topics are not enabled for this group. "
            "Enable 'Topics' in the group settings first."
        )
    if "ADMIN" in error or "FORBIDDEN" in error or "PREMIUM" in error:
        return (
            "Make me an admin with the 'Manage Topics' right, or allow "
            "all members to create topics."
        )
    return ""


def mega_folder_for_file(name: str) -> str | None:
    """Return the top-level MEGA folder for a file path, or None for root files."""
    parts = [part for part in name.replace("\\", "/").split("/") if part]
    return parts[0] if len(parts) > 1 else None


async def get_or_create_folder_topic(
    folder_name: str,
    topic_cache: dict[str, int],
) -> int:
    """Resolve the folder through the persistent registry on every file."""
    title = clamp_topic_name(folder_name.strip())
    if not title:
        raise RuntimeError("MEGA folder name is empty; cannot create topic")

    topic_id, _created = await get_or_create_topic(title)
    topic_cache[folder_name] = topic_id
    return topic_id


async def create_topic(name: str) -> int:
    """Create a forum topic and return its topic ID."""
    peer = await client.get_input_entity(CHAT_ID)
    result = await client(
        CreateForumTopicRequest(
            **{CREATE_TOPIC_PEER_KWARG: peer},
            title=name,
            icon_color=random.choice(TOPIC_ICON_COLORS),
            random_id=random.getrandbits(63),
        )
    )

    # The response contains the topic-creation service message; its ID
    # is the topic ID used for posting. Topic IDs are always positive.
    for update in getattr(result, "updates", []):
        message = getattr(update, "message", None)
        action = getattr(message, "action", None)
        if type(action).__name__ == "MessageActionTopicCreate":
            if isinstance(message.id, int) and message.id > 0:
                return message.id

    raise RuntimeError("Topic created, but its ID could not be determined.")


def topic_name_key(title: str) -> str:
    """Compare titles without surrounding whitespace or case differences."""
    return title.strip().casefold()


async def find_existing_topic(title: str) -> int | None:
    """Find a registered topic; database errors propagate to avoid duplicates."""
    wanted = topic_name_key(title)
    if not wanted:
        return None
    matches = [
        topic_id for topic_id, stored_title in saved_topics()
        if topic_name_key(stored_title) == wanted
    ]
    if not matches:
        return None
    if len(matches) > 1:
        log(
            f"[topic] Multiple registered topics match '{title}': "
            f"{matches}; reusing ID {matches[0]}"
        )
    return matches[0]


async def get_or_create_topic(name: str) -> tuple[int, bool]:
    """Return (topic_id, created), reusing a registered matching topic."""
    title = clamp_topic_name(name.strip())
    if not title:
        raise ValueError("Topic name must not be empty")
    async with TOPIC_CREATE_LOCK:
        existing_id = await find_existing_topic(title)
        if existing_id is not None:
            log(
                f"[topic] Topic '{title}' found in local registry "
                f"(ID: {existing_id}); reusing it"
            )
            return existing_id, False
        topic_id = await create_topic(title)
        save_topic(topic_id, title)
        log(f"[topic] Created and registered topic '{title}' (ID: {topic_id})")
        return topic_id, True


async def resolve_target_topic(
    event, text: str, usage: str
) -> tuple[int | None, str | None]:
    """Resolve the target topic of a queued-link command.

    -newtopic wins over -t, which wins over replying inside a topic.
    A -newtopic name that already exists as a topic is reused instead
    of creating a duplicate. Returns (topic_id, error_text); exactly
    one of the two is None.
    """
    newtopic_match = NEWTOPIC_REGEX.search(text)
    topic_match = TOPIC_REGEX.search(text)

    if newtopic_match:
        new_name = clamp_topic_name(newtopic_match.group(1).strip())
        if not new_name:
            return None, usage

        try:
            topic_id, _created = await get_or_create_topic(new_name)
        except Exception as exc:
            error = str(exc) or type(exc).__name__
            print(f"Topic creation failed: {error}", flush=True)

            hint = topic_error_hint(error)
            reply = f"Could not create topic '{new_name}': {error}"
            if hint:
                reply += f"\n{hint}"

            return None, reply

        return topic_id, None

    if topic_match:
        explicit_id = int(topic_match.group(1))
        # Topic IDs are positive message IDs; "-t 0" would silently post
        # into the General topic instead of the intended one.
        if explicit_id <= 0:
            return None, "Topic IDs are positive numbers, e.g. -t 42."
        return explicit_id, None

    return get_topic_id(event), None


async def notify(text: str, topic_id: int | None):
    """Send a status message and return it for possible deletion."""
    try:
        return await client.send_message(
            CHAT_ID,
            text,
            reply_to=topic_id,
        )
    except Exception as exc:
        print(f"Notification failed: {exc}", flush=True)
        return None


async def delete_status(messages: list, delay: int) -> None:
    """Delete the supplied bot messages after the configured delay."""
    if delay > 0:
        await asyncio.sleep(delay)

    message_ids = [message.id for message in messages if message is not None]
    if not message_ids:
        return

    try:
        await client.delete_messages(CHAT_ID, message_ids)
    except Exception as exc:
        print(f"Message deletion failed: {exc}", flush=True)


# ---- Local media preparation for Telegram ----
# Hashes are computed before preparation. Animated images bypass cleaning.
# Preparation errors abort the job rather than uploading uncleaned static
# images or videos that do not meet the requested resolution limit.
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".gif",
}
VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".flv",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp", ".ogv", ".vob",
}
# Videos are uploaded as downloaded unless a job carries -recode.
# VIDEO_REENCODE=1 turns 720p preparation on for every job (default off).
VIDEO_REENCODE = (
    os.environ.get("VIDEO_REENCODE", "0").strip().lower()
    in ("1", "true", "yes")
)
FFMPEG_TIMEOUT = 7200


def media_target(path: Path, suffix: str) -> Path:
    """Keep the original name when possible, without overwriting siblings."""
    if path.suffix.lower() == suffix and path.suffix == suffix:
        return path
    target = path.with_suffix(suffix)
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.stem}_{counter}{suffix}")
        counter += 1
    return target


def prepare_image_for_telegram(path: Path) -> Path:
    """Apply EXIF orientation and save static pixels without source metadata."""
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        return path
    if RUNNING["cancel"]:
        raise JobCancelled()

    suffix = path.suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        suffix = ".png"
    target = media_target(path, suffix)

    with Image.open(path) as image:
        if getattr(image, "is_animated", False):
            log(f"[opt] Animated image left unchanged: {path.name}")
            return path
        if getattr(image, "n_frames", 1) > 1:
            log(f"[opt] Multi-frame image left unchanged: {path.name}")
            return path
        image.load()
        oriented = ImageOps.exif_transpose(image)
        try:
            has_alpha = (
                "A" in oriented.getbands() or "transparency" in oriented.info
            )
            mode = "RGBA" if has_alpha else "RGB"
            with oriented.convert(mode) as pixels:
                with Image.new(mode, pixels.size) as clean:
                    clean.paste(pixels)
                    clean.info.clear()
                    with tempfile.TemporaryDirectory(
                        prefix="image-clean-", dir=str(path.parent)
                    ) as temporary:
                        output = Path(temporary) / ("clean" + suffix)
                        if suffix in {".jpg", ".jpeg"}:
                            with Image.new("RGB", clean.size, "white") as rgb:
                                if mode == "RGBA":
                                    with clean.getchannel("A") as alpha:
                                        rgb.paste(clean, mask=alpha)
                                else:
                                    rgb.paste(clean)
                                rgb.save(
                                    output, format="JPEG", quality=95,
                                    optimize=True, progressive=True, exif=b"",
                                )
                        elif suffix == ".webp":
                            clean.save(
                                output, format="WEBP", quality=90,
                                method=6, exif=b"", xmp=b"",
                            )
                        else:
                            clean.save(output, format="PNG", optimize=True)
                        with Image.open(output) as check:
                            check.load()
                            if check.getexif() or check.info.get("exif"):
                                raise RuntimeError(
                                    f"EXIF removal verification failed: {path.name}"
                                )
                        if RUNNING["cancel"]:
                            raise JobCancelled()
                        output.replace(target)
        finally:
            if oriented is not image:
                oriented.close()

    if target != path:
        path.unlink(missing_ok=True)
    log(
        f"[opt] Image metadata removed: {target.name} "
        f"({format_size(target.stat().st_size)})"
    )
    return target


def _probe_video(path: Path) -> dict:
    """Return basic codec/geometry info for a video file via ffprobe.

    Keys returned: video_codec, pix_fmt, width, height, audio_codec.
    Any value that cannot be determined is None.  An empty dict is returned
    when ffprobe is unavailable or exits with a non-zero status.
    """
    if not shutil.which("ffprobe"):
        return {}
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_streams",
                str(path),
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout)
    except Exception:
        return {}

    info: dict = {
        "video_codec": None,
        "pix_fmt": None,
        "width": None,
        "height": None,
        "audio_codec": None,
    }
    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type")
        if codec_type == "video" and info["video_codec"] is None:
            info["video_codec"] = stream.get("codec_name")
            info["pix_fmt"] = stream.get("pix_fmt")
            info["width"] = stream.get("width")
            info["height"] = stream.get("height")
        elif codec_type == "audio" and info["audio_codec"] is None:
            info["audio_codec"] = stream.get("codec_name")
    return info


def _needs_reencode(info: dict) -> bool:
    """Return True when the video must be transcoded for Telegram delivery.

    Stream-copy (remux only) is sufficient when ALL of the following hold:
      - Video codec is H.264
      - Pixel format is yuv420p  (required by all H.264 Baseline/Main profiles)
      - Fits inside the oriented 720p box:
            landscape  → width ≤ 1280 and height ≤ 720
            portrait   → width ≤ 720  and height ≤ 1280
        (equivalent: long edge ≤ 1280 and short edge ≤ 720)
      - Audio codec is AAC or no audio track is present

    An empty info dict (probe failed) is treated as "must re-encode" so that
    the safe path is always taken when metadata cannot be read.
    """
    if not info:
        return True

    codec = info.get("video_codec")
    pix = info.get("pix_fmt")
    w = info.get("width") or 0
    h = info.get("height") or 0
    audio = info.get("audio_codec")

    if codec != "h264" or pix != "yuv420p":
        return True
    long_edge = max(w, h)
    short_edge = min(w, h)
    if long_edge > 1280 or short_edge > 720:
        return True
    if audio is not None and audio != "aac":
        return True
    return False


async def _run_ffmpeg(
    ffmpeg_cmd: list[str], path: Path, target: Path, action: str
) -> None:
    """Run ffmpeg_cmd writing to a temp .mp4, then move it to target.

    Honors job cancellation and FFMPEG_TIMEOUT; raises RuntimeError with
    the tail of FFmpeg's stderr on failure.
    """
    with tempfile.TemporaryDirectory(
        prefix="video-prep-", dir=str(path.parent)
    ) as temporary:
        output = Path(temporary) / "prepared.mp4"
        with tempfile.TemporaryFile() as error_log:
            process = await asyncio.create_subprocess_exec(
                *ffmpeg_cmd, str(output),
                stdout=asyncio.subprocess.DEVNULL, stderr=error_log,
            )
            RUNNING["process"] = process
            wait_task = asyncio.create_task(process.wait())
            started = time.monotonic()
            try:
                while not wait_task.done():
                    if RUNNING["cancel"]:
                        raise JobCancelled()
                    if time.monotonic() - started > FFMPEG_TIMEOUT:
                        raise RuntimeError(f"FFmpeg timed out: {path.name}")
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(wait_task), timeout=1
                        )
                    except asyncio.TimeoutError:
                        pass
                await wait_task
                if RUNNING["cancel"]:
                    raise JobCancelled()
                if process.returncode:
                    error_log.seek(0, os.SEEK_END)
                    length = error_log.tell()
                    error_log.seek(max(0, length - 4000))
                    detail = error_log.read().decode("utf-8", errors="replace")
                    raise RuntimeError(
                        f"{action} failed for {path.name}: {detail.strip()}"
                    )
            finally:
                if process.returncode is None:
                    try:
                        process.terminate()
                    except ProcessLookupError:
                        pass
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(wait_task), timeout=5
                        )
                    except asyncio.TimeoutError:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                        await wait_task
                if RUNNING["process"] is process:
                    RUNNING["process"] = None

        if not output.is_file() or not output.stat().st_size:
            raise RuntimeError(f"FFmpeg produced no output for: {path.name}")
        output.replace(target)



async def prepare_video_for_telegram(path: Path, recode: bool = True) -> Path:
    """Prepare a video for Telegram upload within a 720p H.264/AAC envelope.

    With recode=False (and VIDEO_REENCODE off) every video goes through
    container_to_mp4() (stream copy into an optimised MP4, no re-encoding).

    If the file is already H.264/yuv420p/AAC and fits inside the 720p box,
    only a fast remux is performed (stream copy + faststart + metadata strip).
    Otherwise the video is fully transcoded to libx264 CRF 26, AAC 128k and
    scaled to at most 1280×720 (landscape) / 720×1280 (portrait).

    Landscape box: at most 1280×720.  Portrait box: at most 720×1280.
    FFmpeg applies stored rotation metadata before scaling when re-encoding.
    Smaller-than-box videos are never enlarged.
    """
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        return path
    if RUNNING["cancel"]:
        raise JobCancelled()
    if not (recode or VIDEO_REENCODE):
        # Without -recode the streams stay untouched: every video
        # (MP4 included) is stream-copied into a Telegram-optimised MP4
        # container (faststart, metadata stripped), no re-encoding.
        return await container_to_mp4(path)
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is required for 720p video preparation")

    target = media_target(path, ".mp4")
    scale_filter = (
        "scale="
        "w='if(gte(iw,ih),min(iw,1280),min(iw,720))':"
        "h='if(gte(iw,ih),min(ih,720),min(ih,1280))':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2"
    )

    # Probe the file and decide whether a full re-encode is needed.
    probe = await asyncio.to_thread(_probe_video, path)
    if probe and probe.get("video_codec") is None:
        # Audio-only file in a video container (e.g. .mp4/.webm with
        # just an audio track): "-map 0:V:0" would make FFmpeg fail.
        log(f"[opt] {path.name} has no video stream; uploading unchanged")
        return path
    reencode = _needs_reencode(probe)

    if reencode:
        log(f"[opt] Re-encoding {path.name} → 720p H.264/AAC + faststart "
            f"(codec={probe.get('video_codec')}, "
            f"pix={probe.get('pix_fmt')}, "
            f"{probe.get('width')}×{probe.get('height')}, "
            f"audio={probe.get('audio_codec')})")
        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-nostdin", "-y", "-i", str(path),
            "-map", "0:V:0", "-map", "0:a:0?",
            "-map_metadata", "-1", "-map_chapters", "-1",
            "-vf", scale_filter,
            "-c:v", "libx264", "-preset", "slow", "-crf", "26",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart",
        ]
    else:
        log(f"[opt] Remuxing {path.name} (already 720p H.264/AAC) + faststart")
        ffmpeg_cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-nostdin", "-y", "-i", str(path),
            "-map", "0:V:0", "-map", "0:a:0?",
            "-map_metadata", "-1", "-map_chapters", "-1",
            "-c", "copy",
            "-movflags", "+faststart",
        ]

    action = "Re-encoding" if reencode else "Remux"
    await _run_ffmpeg(ffmpeg_cmd, path, target, action)

    if target != path:
        path.unlink(missing_ok=True)
    action_label = "Re-encoded" if reencode else "Remuxed"
    log(
        f"[opt] {action_label}: {target.name} "
        f"({format_size(target.stat().st_size)})"
    )
    return target


async def container_to_mp4(path: Path) -> Path:
    """Stream-copy a video into a Telegram-optimised MP4 container.

    Used for every video (MP4 included) when -recode is not given.

    Streams are stream-copied (no quality loss, fast): first real video
    stream (cover art skipped), all audio tracks, faststart for instant
    playback, metadata and chapters stripped, subtitles dropped (most
    subtitle formats cannot be stored in MP4). HEVC gets the hvc1 tag so
    Apple clients can play it. If the codecs do not fit into MP4 (e.g.
    WMV/FLV legacy codecs) or FFmpeg is missing, the original file is
    uploaded unchanged instead of failing the job.
    """
    if not shutil.which("ffmpeg"):
        log(f"[opt] FFmpeg missing; uploading {path.name} unchanged")
        return path

    probe = await asyncio.to_thread(_probe_video, path)
    target = media_target(path, ".mp4")
    ffmpeg_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-nostdin", "-y", "-i", str(path),
        "-map", "0:V:0", "-map", "0:a?",
        "-map_metadata", "-1", "-map_chapters", "-1",
        "-sn", "-dn",
        "-c", "copy",
    ]
    if probe.get("video_codec") == "hevc":
        ffmpeg_cmd += ["-tag:v", "hvc1"]
    ffmpeg_cmd += ["-movflags", "+faststart"]

    log(
        f"[opt] Copying {path.name} into MP4 container (stream copy, "
        f"codec={probe.get('video_codec')}, audio={probe.get('audio_codec')})"
    )
    try:
        await _run_ffmpeg(ffmpeg_cmd, path, target, "MP4 container copy")
    except JobCancelled:
        raise
    except RuntimeError as exc:
        log(f"[opt] {exc}; uploading {path.name} unchanged")
        return path

    if target != path:
        path.unlink(missing_ok=True)
    log(
        f"[opt] Container copied: {target.name} "
        f"({format_size(target.stat().st_size)})"
    )
    return target


# ---- Queue worker ----
def requeue_running_jobs() -> int:
    """Reset orphaned 'running' jobs to 'pending'; return how many.

    There is exactly one worker, so a 'running' row without a live
    worker iteration was interrupted (crash or shutdown).
    """
    with closing(db_connect()) as db, db:
        cursor = db.execute(
            "UPDATE jobs SET status = 'pending' WHERE status = 'running'"
        )
        return cursor.rowcount


async def worker() -> None:
    """Supervise the queue loop and restart it after unexpected errors.

    Without this, a single unexpected exception (for example a SQLite
    error inside an error handler) would end the worker task silently
    and the queue would stop until the container restarts.
    """
    log("[worker] Queue worker started")

    while True:
        try:
            await _worker_loop()
        except asyncio.CancelledError:
            log("[worker] Queue worker stopped")
            raise
        except Exception as exc:
            log(
                f"[worker] Unexpected error ({type(exc).__name__}: {exc}); "
                "restarting the queue loop in 5s"
            )
            try:
                requeued = requeue_running_jobs()
                if requeued:
                    log(f"[worker] Re-queued {requeued} interrupted job(s)")
            except Exception as db_exc:
                log(f"[worker] Could not re-queue interrupted jobs: {db_exc}")
            await asyncio.sleep(5)


async def _worker_loop() -> None:
    """Process queued jobs one at a time."""
    while True:
        job = claim_job()

        if job is None:
            await asyncio.sleep(2)
            continue

        (
            job_id,
            link,
            topic_id,
            requester,
            auto_folder_topics,
            force_dupes,
            file_delay,
            retry_attempt,
            recode,
        ) = job
        RUNNING.update(job_id=job_id, process=None, cancel=False)
        JOB_PROGRESS[job_id] = {
            "total": None,
            "current": 0,
            "current_name": "",
            "uploaded": 0,
            "kept": 0,
            "skipped": 0,
            "failed": 0,
        }
        log(
            f"[worker] Claimed job #{job_id} "
            f"(topic: {topic_id or 'default'}): {link}"
        )
        start_message = await notify(f"Starting job #{job_id}.", topic_id)

        try:
            file_count = 0  # files sent to Telegram
            kept_count = 0  # files retained on the server
            skipped_count = 0  # duplicates found after download
            stats = {"skipped_pre": 0}
            file_folders: dict[str, str | None] = {}
            file_handles: dict[str, str] = {}
            folder_topics: dict[str, int] = {}

            async def handle_downloaded(path: Path) -> None:
                """Dedup-check the original, prepare locally, then upload."""
                nonlocal file_count, kept_count, skipped_count
                if RUNNING["cancel"]:
                    raise JobCancelled()
                hash_value = await asyncio.to_thread(file_hash, path)

                # Store the original hash, name and size, never the encoded
                # output, so existing duplicate records remain compatible.
                if not force_dupes and seen_file(hash_value):
                    skipped_count += 1
                    JOB_PROGRESS[job_id]["skipped"] += 1
                    mark_skipped(
                        path.name, path.stat().st_size, "hash", job_id,
                    )
                    log(
                        f"[worker] Job #{job_id}: duplicate skipped: "
                        f"{path.name} ({hash_value[:16]}...)"
                    )
                    file_folders.pop(str(path), None)
                    file_handles.pop(str(path), None)
                    path.unlink(missing_ok=True)
                    return

                original_name = path.name
                original_size = path.stat().st_size
                folder_name = file_folders.pop(str(path), None)
                node_handle = file_handles.pop(str(path), None)

                upload_path = await asyncio.to_thread(
                    prepare_image_for_telegram, path
                )
                upload_path = await prepare_video_for_telegram(
                    upload_path, recode
                )
                if RUNNING["cancel"]:
                    raise JobCancelled()

                file_topic_id = topic_id
                if auto_folder_topics and folder_name:
                    try:
                        file_topic_id = await get_or_create_folder_topic(
                            folder_name, folder_topics
                        )
                    except Exception as exc:
                        raise RuntimeError(
                            f"Could not create topic for folder "
                            f"'{folder_name}': {exc}"
                        ) from exc

                # Size checks and splitting apply to the prepared file.
                try:
                    uploaded = await upload_one(upload_path, file_topic_id)
                except TopicDeletedError:
                    # A registered folder topic was deleted in Telegram.
                    # Forget the stale ID, recreate the topic once and
                    # retry; explicit targets (-t/-newtopic) still fail.
                    if not (auto_folder_topics and folder_name):
                        raise
                    log(
                        f"[topic] Topic {file_topic_id} for folder "
                        f"'{folder_name}' was deleted; recreating it"
                    )
                    forget_topic(file_topic_id)
                    folder_topics.pop(folder_name, None)
                    file_topic_id = await get_or_create_folder_topic(
                        folder_name, folder_topics
                    )
                    uploaded = await upload_one(upload_path, file_topic_id)

                if uploaded:
                    file_count += 1
                    JOB_PROGRESS[job_id]["uploaded"] = file_count
                    mark_seen(
                        hash_value, original_name, original_size,
                        job_id, node_handle,
                    )
                    upload_path.unlink(missing_ok=True)
                    log(f"[worker] Job #{job_id}: uploaded {upload_path.name}")
                else:
                    # Retained files are not recorded as uploaded.
                    kept_count += 1
                    JOB_PROGRESS[job_id]["kept"] = kept_count
                    log(
                        f"[worker] Job #{job_id}: kept {upload_path.name} "
                        "on the server"
                    )

            with tempfile.TemporaryDirectory(dir=WORK_DIR) as temp_dir:
                log(f"[worker] Job #{job_id} using temp dir {temp_dir}")

                if link.startswith("yt:"):
                    for path in await run_ytdlp(
                        link[3:], Path(temp_dir), YT_MAX_SIZE
                    ):
                        await handle_downloaded(path)

                elif link.startswith("nk:"):
                    for path in await run_ytdlp(
                        link[3:], Path(temp_dir), NZ_MAX_SIZE
                    ):
                        await handle_downloaded(path)

                elif link.startswith("bk:"):
                    async def bunkr_status(text: str) -> None:
                        """Show per-file progress in the start message."""
                        if start_message is None:
                            return
                        try:
                            await start_message.edit(f"Job #{job_id}: {text}")
                        except Exception as exc:
                            log(f"[bunkr] Status update failed: {exc}")

                    # Same pipeline as MEGA: each downloaded file goes
                    # through hash dedup, preparation and upload.
                    async with aclosing(iter_bunkr(
                        link[3:],
                        Path(temp_dir),
                        job_id,
                        file_folders,
                        bunkr_status,
                    )) as downloads:
                        async for path in downloads:
                            await handle_downloaded(path)

                elif link.startswith("cm:"):
                    async def coomer_status(text: str) -> None:
                        """Show per-file progress in the start message."""
                        if start_message is None:
                            return
                        try:
                            await start_message.edit(f"Job #{job_id}: {text}")
                        except Exception as exc:
                            log(f"[coomer] Status update failed: {exc}")

                    # Same pipeline as MEGA: each downloaded file goes
                    # through hash dedup, preparation and upload.
                    async with aclosing(iter_coomer(
                        link[3:],
                        Path(temp_dir),
                        job_id,
                        file_folders,
                        coomer_status,
                    )) as downloads:
                        async for path in downloads:
                            await handle_downloaded(path)

                elif link.startswith("cf:"):
                    async def coomerfans_status(text: str) -> None:
                        """Show per-file progress in the start message."""
                        if start_message is None:
                            return
                        try:
                            await start_message.edit(f"Job #{job_id}: {text}")
                        except Exception as exc:
                            log(f"[coomerfans] Status update failed: {exc}")

                    # Same pipeline as MEGA: each downloaded file goes
                    # through hash dedup, preparation and upload.
                    async with aclosing(iter_coomerfans(
                        link[3:],
                        Path(temp_dir),
                        job_id,
                        file_folders,
                        coomerfans_status,
                    )) as downloads:
                        async for path in downloads:
                            await handle_downloaded(path)

                else:
                    # aclosing() finalizes the generator deterministically
                    # when handle_downloaded() raises, before the temp
                    # directory is removed.
                    async with aclosing(iter_download(
                        link,
                        Path(temp_dir),
                        job_id,
                        stats,
                        file_folders,
                        force_dupes,
                        file_delay,
                        file_handles,
                    )) as downloads:
                        async for path in downloads:
                            await handle_downloaded(path)

            skipped_total = skipped_count + stats["skipped_pre"]

            if file_count + kept_count + skipped_total == 0:
                raise RuntimeError("No files found")

            finish_job(job_id)

            summary = f"{file_count} file(s) sent"
            if kept_count:
                summary += f", {kept_count} kept on server"
            if skipped_total:
                summary += f", {skipped_total} duplicate(s) skipped"

            done_message = await notify(
                f"Job #{job_id} finished ({summary}).",
                topic_id,
            )

            log(f"[worker] Job #{job_id} finished successfully.")

            # Delete start and success messages after the configured delay.
            spawn(
                delete_status(
                    [start_message, done_message],
                    STATUS_DELETE_DELAY,
                )
            )

        except JobCancelled:
            finish_job(job_id, error="Cancelled by user")

            log(f"[worker] Job #{job_id} cancelled by user.")
            cancel_text = f"Job #{job_id} cancelled."
            if MEGA_RESUME and re.match(
                r"https?://mega\.(?:nz|io)/(?:folder|file)/", link
            ):
                # Uploaded files are skipped and the interrupted file
                # continues from its partial data on the next run.
                cancel_text += (
                    f" /retry {job_id} continues where it stopped."
                )
            await notify(cancel_text, topic_id)

            # Keep the cancellation message visible.
            spawn(delete_status([start_message], 0))

        except InsufficientDiskSpace as exc:
            # The work volume is too full for the next file. Stopping
            # the whole job keeps the remaining files from failing one
            # by one; partial resume data stays and uploaded files are
            # skipped by the duplicate registry on /retry.
            finish_job(job_id, error=f"disk full: {exc}")
            log(f"[worker] Job #{job_id} stopped: {exc}")
            await notify(
                f"Job #{job_id} stopped: {exc}. Free up disk space (or "
                f"lower MIN_FREE_SPACE_MIB), then /retry {job_id}; "
                "already sent files are skipped and the interrupted "
                "download resumes.",
                topic_id,
            )

            # Keep the error message visible, like other failures.
            spawn(delete_status([start_message], 0))

        except MegaJobAborted as exc:
            finish_job(job_id, error=str(exc))
            log(f"[worker] Job #{job_id} aborted: {exc}")

            base_delay = file_delay if file_delay > 0 else MEGA_FILE_DELAY

            if retry_attempt >= MEGA_AUTO_RETRIES:
                log(
                    f"[worker] Job #{job_id}: automatic retry limit "
                    f"({MEGA_AUTO_RETRIES}) reached"
                )
                await notify(
                    f"Job #{job_id} aborted: {str(exc).rstrip('.')} "
                    f"No automatic retry is left (limit "
                    f"{MEGA_AUTO_RETRIES}); use /retry once MEGA "
                    "accepts downloads from this share again.",
                    topic_id,
                )
            else:
                next_delay = base_delay * 2
                new_id = add_jobs(
                    [link],
                    topic_id,
                    requester,
                    auto_folder_topics,
                    force_dupes,
                    file_delay=next_delay,
                    retry_attempt=retry_attempt + 1,
                    recode=recode,
                )[0]
                finish_job(
                    job_id,
                    error=f"re-queued as job #{new_id}: {exc}",
                    status="requeued",
                )
                log(
                    f"[worker] Job #{job_id} re-queued as job #{new_id} "
                    f"with a {next_delay}s per-file delay (run "
                    f"{retry_attempt + 2} of {MEGA_AUTO_RETRIES + 1})"
                )
                await notify(
                    f"Job #{job_id} aborted by MEGA error -16. "
                    f"Re-queued as job #{new_id} with a {next_delay}s "
                    f"per-file delay (run {retry_attempt + 2} of "
                    f"{MEGA_AUTO_RETRIES + 1}).",
                    topic_id,
                )

            # Keep the error message visible, like other failures.
            spawn(delete_status([start_message], 0))

        except MegaBandwidthLimit as exc:
            # MEGA's bandwidth quota for this IP is exhausted (HTTP
            # 509). Continuing with the next file would fail the same
            # way, so the job pauses and is re-queued with a wait that
            # doubles per pause. Files already uploaded stay in the
            # duplicate registry and are skipped when the job resumes.
            finish_job(job_id, error=f"paused: {exc}")
            log(f"[worker] Job #{job_id} paused: {exc}")

            if retry_attempt >= MEGA_AUTO_RETRIES:
                log(
                    f"[worker] Job #{job_id}: 509 re-queue limit "
                    f"({MEGA_AUTO_RETRIES}) reached"
                )
                await notify(
                    f"Job #{job_id} paused ({exc}). No automatic "
                    f"re-queue is left (limit {MEGA_AUTO_RETRIES}); "
                    "use /retry once MEGA accepts downloads again.",
                    topic_id,
                )
            else:
                wait = BANDWIDTH_REQUEUE_DELAY * (2 ** retry_attempt)
                new_id = add_jobs(
                    [link],
                    topic_id,
                    requester,
                    auto_folder_topics,
                    force_dupes,
                    file_delay=file_delay,
                    retry_attempt=retry_attempt + 1,
                    recode=recode,
                    not_before=time.time() + wait,
                )[0]
                finish_job(
                    job_id,
                    error=f"re-queued as job #{new_id}: paused: {exc}",
                    status="requeued",
                )
                log(
                    f"[worker] Job #{job_id} re-queued as job #{new_id} "
                    f"after a bandwidth pause; it starts in about "
                    f"{int(wait / 60)} min (run {retry_attempt + 2} "
                    f"of {MEGA_AUTO_RETRIES + 1})"
                )
                await notify(
                    f"Job #{job_id} paused ({exc}). Re-queued as job "
                    f"#{new_id}; it starts in about {int(wait / 60)} "
                    "minutes. Already sent files are skipped then.",
                    topic_id,
                )

            # Keep the error message visible, like other failures.
            spawn(delete_status([start_message], 0))

        except Exception as exc:
            error = str(exc) or type(exc).__name__
            finish_job(job_id, error=error)

            log(f"[worker] Job #{job_id} FAILED: {error}")
            await notify(f"Job #{job_id} failed: {error}", topic_id)

            # Keep the error message visible, but remove the start message.
            spawn(delete_status([start_message], 0))

        finally:
            RUNNING.update(
                job_id=None, process=None, cancel=RUNNING["shutdown"]
            )
            JOB_PROGRESS.pop(job_id, None)

        _, pending = queue_status()
        log(f"[worker] Pending jobs: {pending}")


# ---- Telegram commands ----
@client.on(events.NewMessage(pattern=rf"^{GAP}/mega(?:@\w+)?{GAP}(?:\s|$)"))
async def on_mega(event) -> None:
    """/mega <link(s)> [-t topic_id] [-newtopic <name>] [-dupes] [-recode]."""
    if not is_allowed(event):
        return

    # Remove invisible characters so the flag parsing below works
    # reliably on pasted text.
    text = INVISIBLE_RE.sub("", event.raw_text)
    log(f"[mega] Command from {event.sender_id}: {text!r}")

    # "-dupes" re-transfers files the duplicate registry already knows.
    force_dupes = bool(DUPES_REGEX.search(text))
    if force_dupes:
        text = DUPES_REGEX.sub(" ", text)
    recode, text = pop_flag(RECODE_REGEX, text)

    links = clean_urls(MEGA_REGEX.findall(text))
    # Bunkr URLs pasted alongside MEGA links share the same queue.
    links += [f"bk:{url}" for url in bunkr_links_in(text)]
    links += [f"cm:{url}" for url in coomer_links_in(text)]
    links += [f"cf:{url}" for url in coomerfans_links_in(text)]

    if not links:
        await event.reply(
            "Usage: /mega <mega-link(s)> [-t topic_id] "
            "[-newtopic <name>] [-dupes] [-recode]"
        )
        return

    topic_id, error = await resolve_target_topic(
        event, text,
        "Usage: /mega <mega-link(s)> -newtopic <name> [-dupes] [-recode]",
    )
    if error:
        await event.reply(error)
        return

    auto_folder_topics = (
        not NEWTOPIC_REGEX.search(text)
        and not TOPIC_REGEX.search(text)
        and get_topic_id(event) is None
    )
    add_jobs(
        links,
        topic_id,
        event.sender_id,
        auto_folder_topics=auto_folder_topics,
        force_dupes=force_dupes,
        recode=recode,
    )
    _, pending = queue_status()

    acknowledgement = await event.reply(
        f"Saved {len(links)} link(s). Pending: {pending}"
    )

    # Delete the acknowledgement after the configured delay.
    spawn(
        delete_status([acknowledgement], STATUS_DELETE_DELAY)
    )


@client.on(events.NewMessage(pattern=rf"^{GAP}/yt(?:@\w+)?{GAP}(?:\s|$)"))
async def on_yt(event) -> None:
    """/yt <url(s)> [-t <topic_id>] [-newtopic <name>] [-dupes] [-recode] - queue YouTube videos."""
    if not is_allowed(event):
        return

    text = INVISIBLE_RE.sub("", event.raw_text)
    log(f"[yt] Command from {event.sender_id}: {text!r}")
    # "-dupes" re-sends videos the duplicate registry already knows.
    force_dupes, text = pop_flag(DUPES_REGEX, text)
    recode, text = pop_flag(RECODE_REGEX, text)

    urls = clean_urls(YT_REGEX.findall(text))

    if not urls:
        await event.reply(
            "Usage: /yt <youtube-url> [-t topic_id] [-newtopic <name>] "
            "[-dupes] [-recode]"
        )
        return

    topic_id, error = await resolve_target_topic(
        event, text, "Usage: /yt <youtube-url> -newtopic <name>"
    )
    if error:
        await event.reply(error)
        return

    add_jobs(
        [f"yt:{url}" for url in urls], topic_id, event.sender_id,
        force_dupes=force_dupes, recode=recode,
    )
    _, pending = queue_status()

    acknowledgement = await event.reply(
        f"Saved {len(urls)} video(s). Pending: {pending}"
    )
    spawn(
        delete_status([acknowledgement], STATUS_DELETE_DELAY)
    )


@client.on(events.NewMessage(pattern=rf"^{GAP}/nk(?:@\w+)?{GAP}(?:\s|$)"))
async def on_nk(event) -> None:
    """/nk <url(s)> [-t <topic_id>] [-newtopic <name>] [-dupes] [-recode] - queue Netzkino movies."""
    if not is_allowed(event):
        return

    text = INVISIBLE_RE.sub("", event.raw_text)
    log(f"[nk] Command from {event.sender_id}: {text!r}")
    # "-dupes" re-sends videos the duplicate registry already knows.
    force_dupes, text = pop_flag(DUPES_REGEX, text)
    recode, text = pop_flag(RECODE_REGEX, text)

    urls = clean_urls(NETZKINO_REGEX.findall(text))

    if not urls:
        await event.reply(
            "Usage: /nk <netzkino-url> [-t topic_id] [-newtopic <name>] "
            "[-dupes] [-recode]"
        )
        return

    topic_id, error = await resolve_target_topic(
        event, text, "Usage: /nk <netzkino-url> -newtopic <name>"
    )
    if error:
        await event.reply(error)
        return

    add_jobs(
        [f"nk:{url}" for url in urls], topic_id, event.sender_id,
        force_dupes=force_dupes, recode=recode,
    )
    _, pending = queue_status()

    acknowledgement = await event.reply(
        f"Saved {len(urls)} movie(s). Pending: {pending}"
    )
    spawn(
        delete_status([acknowledgement], STATUS_DELETE_DELAY)
    )


@client.on(events.NewMessage(
    pattern=rf"^{GAP}/(?:bunkr|bk)(?:@\w+)?{GAP}(?:\s|$)"
))
async def on_bunkr(event) -> None:
    """/bunkr <url(s)> [-t topic_id] [-newtopic <name>] [-dupes] [-recode]."""
    if not is_allowed(event):
        return

    text = INVISIBLE_RE.sub("", event.raw_text)
    log(f"[bunkr] Command from {event.sender_id}: {text!r}")

    force_dupes = bool(DUPES_REGEX.search(text))
    if force_dupes:
        text = DUPES_REGEX.sub(" ", text)
    recode, text = pop_flag(RECODE_REGEX, text)

    links = bunkr_links_in(text)
    usage = (
        "Usage: /bunkr <bunkr-album-or-file-url(s)> [-t topic_id] "
        "[-newtopic <name>] [-dupes] [-recode]"
    )
    if not links:
        rejected = clean_urls(re.findall(r"https?://\S+", text))
        if rejected:
            log(f"[bunkr] Not a recognised Bunkr URL: {rejected}")
            usage = (
                f"Not a recognised Bunkr album/file URL: {rejected[0]}\n"
                + usage
            )
        await event.reply(usage, link_preview=False)
        return

    topic_id, error = await resolve_target_topic(
        event, text,
        "Usage: /bunkr <bunkr-url(s)> -newtopic <name> [-dupes] [-recode]",
    )
    if error:
        await event.reply(error)
        return

    # Without an explicit target, each album gets its own topic named
    # after the album title, like MEGA folder topics.
    auto_folder_topics = (
        not NEWTOPIC_REGEX.search(text)
        and not TOPIC_REGEX.search(text)
        and get_topic_id(event) is None
    )
    add_jobs(
        [f"bk:{url}" for url in links],
        topic_id,
        event.sender_id,
        auto_folder_topics=auto_folder_topics,
        force_dupes=force_dupes,
        recode=recode,
    )
    _, pending = queue_status()

    acknowledgement = await event.reply(
        f"Saved {len(links)} Bunkr link(s). Pending: {pending}"
    )
    spawn(
        delete_status([acknowledgement], STATUS_DELETE_DELAY)
    )


@client.on(events.NewMessage(
    pattern=rf"^{GAP}/(?:coomer|cm)(?:@\w+)?{GAP}(?:\s|$)"
))
async def on_coomer(event) -> None:
    """/coomer <url(s)> - queue Coomer profiles or posts (alias /cm)."""
    if not is_allowed(event):
        return

    text = INVISIBLE_RE.sub("", event.raw_text)
    log(f"[coomer] Command from {event.sender_id}: {text!r}")

    force_dupes = bool(DUPES_REGEX.search(text))
    if force_dupes:
        text = DUPES_REGEX.sub(" ", text)
    recode, text = pop_flag(RECODE_REGEX, text)

    links = coomer_links_in(text)
    usage = (
        "Usage: /coomer <coomer-profile-or-post-url(s)> [-t topic_id] "
        "[-newtopic <name>] [-dupes] [-recode]"
    )
    if not links:
        rejected = clean_urls(re.findall(r"https?://\S+", text))
        if rejected:
            log(f"[coomer] Not a recognised Coomer URL: {rejected}")
            usage = (
                f"Not a recognised Coomer profile/post URL: {rejected[0]}\n"
                + usage
            )
        await event.reply(usage, link_preview=False)
        return

    topic_id, error = await resolve_target_topic(
        event, text,
        "Usage: /coomer <coomer-url(s)> -newtopic <name> [-dupes] [-recode]",
    )
    if error:
        await event.reply(error)
        return

    # Without an explicit target, each profile gets its own topic named
    # after the profile, like Bunkr album topics.
    auto_folder_topics = (
        not NEWTOPIC_REGEX.search(text)
        and not TOPIC_REGEX.search(text)
        and get_topic_id(event) is None
    )
    add_jobs(
        [f"cm:{url}" for url in links],
        topic_id,
        event.sender_id,
        auto_folder_topics=auto_folder_topics,
        force_dupes=force_dupes,
        recode=recode,
    )
    _, pending = queue_status()

    acknowledgement = await event.reply(
        f"Saved {len(links)} Coomer link(s). Pending: {pending}"
    )
    spawn(
        delete_status([acknowledgement], STATUS_DELETE_DELAY)
    )


async def on_coomerfans(event) -> None:
    """/coomerfans <url(s)> - queue Coomerfans posts or profiles (alias /cf)."""
    if not is_allowed(event):
        return

    text = INVISIBLE_RE.sub("", event.raw_text)
    log(f"[coomerfans] Command from {event.sender_id}: {text!r}")

    force_dupes = bool(DUPES_REGEX.search(text))
    if force_dupes:
        text = DUPES_REGEX.sub(" ", text)
    recode, text = pop_flag(RECODE_REGEX, text)

    links = coomerfans_links_in(text)
    usage = (
        "Usage: /coomerfans <coomerfans-post-or-profile-url(s)> "
        "[-t topic_id] [-newtopic <name>] [-dupes] [-recode]"
    )
    if not links:
        rejected = clean_urls(re.findall(r"https?://\S+", text))
        if rejected:
            log(f"[coomerfans] Not a recognised Coomerfans URL: {rejected}")
            usage = (
                f"Not a recognised Coomerfans post/profile URL: {rejected[0]}\n"
                + usage
            )
        await event.reply(usage, link_preview=False)
        return

    topic_id, error = await resolve_target_topic(
        event, text,
        "Usage: /coomerfans <coomerfans-url(s)> -newtopic <name> "
        "[-dupes] [-recode]",
    )
    if error:
        await event.reply(error)
        return

    # Without an explicit target, each profile gets its own topic named
    # after the profile, like Bunkr album topics.
    auto_folder_topics = (
        not NEWTOPIC_REGEX.search(text)
        and not TOPIC_REGEX.search(text)
        and get_topic_id(event) is None
    )
    add_jobs(
        [f"cf:{url}" for url in links],
        topic_id,
        event.sender_id,
        auto_folder_topics=auto_folder_topics,
        force_dupes=force_dupes,
        recode=recode,
    )
    _, pending = queue_status()

    acknowledgement = await event.reply(
        f"Saved {len(links)} Coomerfans link(s). Pending: {pending}"
    )
    spawn(
        delete_status([acknowledgement], STATUS_DELETE_DELAY)
    )


# Registered only when enabled: with COOMERFANS_ENABLED=0 the command
# does not exist at all (no handler, no /help line).
if COOMERFANS_ENABLED:
    client.add_event_handler(
        on_coomerfans,
        events.NewMessage(
            pattern=rf"^{GAP}/(?:coomerfans|cf)(?:@\w+)?{GAP}(?:\s|$)"
        ),
    )


@client.on(events.NewMessage(pattern=rf"^{GAP}/queue(?:@\w+)?{GAP}$"))
async def on_queue(event) -> None:
    """/queue - show running job progress and the pending jobs."""
    if not is_allowed(event):
        return

    running_id, pending = queue_status()
    parts = []

    if running_id is not None:
        progress = JOB_PROGRESS.get(running_id, {})
        total = progress.get("total")

        if total:
            current_line = (
                f"Job #{running_id}: file "
                f"{progress.get('current', 0)}/{total}"
            )
            if progress.get("current_name"):
                current_line += f" — {progress['current_name']}"
            parts.append(current_line)
        else:
            parts.append(f"Job #{running_id} is running.")

        counts = []
        if progress.get("uploaded"):
            counts.append(f"{progress['uploaded']} sent")
        if progress.get("skipped"):
            counts.append(f"{progress['skipped']} duplicate(s) skipped")
        if progress.get("kept"):
            counts.append(f"{progress['kept']} kept on server")
        if progress.get("failed"):
            counts.append(f"{progress['failed']} failed")
        if counts:
            parts.append("So far: " + ", ".join(counts))
    else:
        parts.append("Current job: none")

    jobs = pending_jobs()
    if jobs:
        lines = []
        for job_id, job_link, not_before in jobs:
            short = (
                job_link if len(job_link) <= 60 else job_link[:57] + "..."
            )
            if not_before and not_before > time.time():
                minutes = max(
                    1, int(round((not_before - time.time()) / 60))
                )
                lines.append(
                    f"#{job_id} — paused, starts in ~{minutes} min: "
                    f"{short}"
                )
            else:
                lines.append(f"#{job_id}: {short}")
        parts.append(f"Pending ({pending}):\n" + "\n".join(lines))

        if pending > len(jobs):
            parts.append(f"...and {pending - len(jobs)} more.")
    else:
        parts.append("Pending: 0")

    _, failed_total = list_failed(limit=1)
    if failed_total:
        parts.append(f"Failed: {failed_total} (re-queue with /retry)")

    await event.reply("\n".join(parts))


@client.on(events.NewMessage(pattern=rf"^{GAP}/dupes(?:@\w+)?{GAP}(?:\s{GAP}(reset))?{GAP}$"))
async def on_dupes(event) -> None:
    """/dupes [reset] - show (or clear) dedup registry and skip history."""
    if not is_allowed(event):
        return

    if event.pattern_match.group(1):
        known = forget_seen_files()
        skipped = forget_skipped_files()
        await event.reply(
            f"Registry cleared. {known} file(s) forgotten and the history "
            f"of {skipped} skip event(s) removed; everything will be "
            "sent again on the next job."
        )
        return

    parts = []

    # Part 1: the known-uploads registry.
    count, total_size = seen_stats()
    if count:
        parts.append(
            f"{count} file(s) remembered ({format_size(total_size)}). "
            "Copies of these are skipped in future jobs."
        )
    else:
        parts.append(
            "No uploaded files remembered yet. From now on, every "
            "uploaded file is recorded and skipped when seen again."
        )

    # Part 2: the persistent skip history.
    total_skips, node_skips, pre_skips, hash_skips = skipped_stats()
    if total_skips:
        parts.append(
            f"Skipped so far: {total_skips} duplicate(s) "
            f"({node_skips} by file node, {pre_skips} by name/size, "
            f"{hash_skips} by hash)."
        )

        recent = recent_skipped()
        lines = ["Most recent skips:"]
        lines.extend(
            f"- {name} ({format_size(size)}, {reason}, {when})"
            for name, size, reason, when in recent
        )
        if total_skips > len(recent):
            lines.append(f"... and {total_skips - len(recent)} more.")

        parts.append("\n".join(lines))

    await event.reply("\n\n".join(parts)[:4000], link_preview=False)


@client.on(events.NewMessage(pattern=rf"^{GAP}/help(?:@\w+)?{GAP}$"))
async def on_help(event) -> None:
    """/help - list all commands."""
    if not is_allowed(event):
        return

    await event.reply(
        "Available commands:\n"
        "/mega <link(s)> [-t <topic_id>] [-newtopic <name>] [-dupes] [-recode] - "
        "queue MEGA links\n"
        "/yt <url(s)> [-t <topic_id>] [-newtopic <name>] [-dupes] [-recode] - "
        "queue YouTube videos\n"
        "/nk <url(s)> [-t <topic_id>] [-newtopic <name>] [-dupes] [-recode] - "
        "queue Netzkino movies\n"
        "/bunkr <url(s)> [-t <topic_id>] [-newtopic <name>] [-dupes] [-recode] - "
        "queue Bunkr albums/files (alias /bk)\n"
        "/coomer <url(s)> [-t <topic_id>] [-newtopic <name>] [-dupes] [-recode] - "
        "queue Coomer profiles/posts (alias /cm)\n"
        + (
            "/coomerfans <url(s)> [-t <topic_id>] [-newtopic <name>] "
            "[-dupes] [-recode] - "
            "queue Coomerfans posts/profiles (alias /cf)\n"
            if COOMERFANS_ENABLED else ""
        )
        + "/queue - show job progress and pending jobs\n"
        "/cancel [job_id] - cancel the running job or remove a pending one\n"
        "/failed - list recent failed jobs\n"
        "/retry [job_id] - re-queue failed jobs\n"
        "/newtopic <name> - create a forum topic\n"
        "/topics - list known forum topics\n"
        "/deltopic [id|all] [purge] - remove topics from the registry\n"
        "/dupes [reset] - duplicate registry and skip history\n"
        "-recode on any download command: convert videos to max. 720p "
        "(default: upload videos as-is)\n"
        "/help - show this text",
        link_preview=False,
    )


@client.on(events.NewMessage(pattern=rf"^{GAP}/retry(?:@\w+)?{GAP}(?:\s{GAP}(\d+))?{GAP}$"))
async def on_retry(event) -> None:
    """/retry [job_id] - retry all failed jobs or one specific job."""
    if not is_allowed(event):
        return

    argument = event.pattern_match.group(1)
    count = retry_jobs(int(argument) if argument else None)

    if count == 0:
        await event.reply("No failed jobs found.")
    else:
        await event.reply(f"Re-queued {count} job(s).")


@client.on(events.NewMessage(pattern=rf"^{GAP}/cancel(?:@\w+)?{GAP}(?:\s{GAP}(\d+))?{GAP}$"))
async def on_cancel(event) -> None:
    """/cancel [job_id] - cancel the running job or remove a pending one."""
    if not is_allowed(event):
        return

    argument = event.pattern_match.group(1)

    if argument:
        job_id = int(argument)

        if job_id == RUNNING["job_id"]:
            pass  # The running job is cancelled below.
        elif remove_pending_job(job_id):
            await event.reply(f"Removed pending job #{job_id}.")
            return
        else:
            await event.reply(
                f"Job #{job_id} is neither running nor pending."
            )
            return

    elif RUNNING["job_id"] is None:
        await event.reply("No job is running.")
        return

    # Ask the worker to stop and kill the running download directly.
    RUNNING["cancel"] = True
    process = RUNNING["process"]

    if process is not None and process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass

    await event.reply(f"Cancelling job #{RUNNING['job_id']}...")


@client.on(events.NewMessage(pattern=rf"^{GAP}/failed(?:@\w+)?{GAP}$"))
async def on_failed(event) -> None:
    """/failed - list recent failed jobs and their error messages."""
    if not is_allowed(event):
        return

    rows, total = list_failed()

    if not rows:
        await event.reply("No failed jobs.")
        return

    lines = [f"Failed jobs: {total}"]

    for job_id, link, error in rows:
        short_error = error if len(error) <= 100 else error[:97] + "..."
        lines.append(
            f"\n#{job_id}: {link}\n"
            f"   {short_error or 'Unknown error'}"
        )

    if total > len(rows):
        lines.append(
            f"\n... and {total - len(rows)} more; showing the newest only."
        )

    lines.append("\nRetry with /retry or /retry <job_id>.")

    # Keep the reply below Telegram's message length limit.
    await event.reply(
        "\n".join(lines)[:4000],
        link_preview=False,
    )


@client.on(events.NewMessage(pattern=rf"^{GAP}/newtopic(?:@\w+)?{GAP}(?:\s{GAP}(.+))?$"))
async def on_newtopic(event) -> None:
    """/newtopic <name> - create a forum topic with the given name."""
    if not is_allowed(event):
        return

    name = INVISIBLE_RE.sub("", (event.pattern_match.group(1) or "")).strip()
    if not name:
        await event.reply("Usage: /newtopic <name>")
        return

    name = clamp_topic_name(name)

    try:
        topic_id, created = await get_or_create_topic(name)
    except Exception as exc:
        error = str(exc) or type(exc).__name__
        print(f"Topic creation failed: {error}", flush=True)

        hint = topic_error_hint(error)
        reply = f"Could not create topic '{name}': {error}"
        if hint:
            reply += f"\n{hint}"

        await event.reply(reply)
        return

    if created:
        await event.reply(f"Created topic '{name}' (ID: {topic_id}).")
    else:
        await event.reply(
            f"Topic '{name}' already exists (ID: {topic_id}); using it."
        )


@client.on(events.NewMessage(pattern=rf"^{GAP}/topics(?:@\w+)?{GAP}$"))
async def on_topics(event) -> None:
    """/topics - list the forum topics known to the bot."""
    if not is_allowed(event):
        return

    topics = saved_topics()

    if not topics:
        await event.reply(
            "No topics known yet. Topics I create or see being created "
            "are listed here. You can also address a topic directly with "
            "/mega <link> -t <topic_id>."
        )
        return

    lines = ["Known topics:"]
    lines.extend(f"- {title} (ID: {topic_id})" for topic_id, title in topics)
    lines.append("")
    lines.append(
        "Post into one with /mega, /yt or /nk and -t <topic_id>."
    )

    # Keep the reply below Telegram's message length limit.
    await event.reply("\n".join(lines)[:4000], link_preview=False)


@client.on(events.Raw())
async def on_topic_service_message(update) -> None:
    """Track topic service messages (NewMessage excludes service messages)."""
    message = getattr(update, "message", None)
    if message is None or getattr(message, "chat_id", None) != CHAT_ID:
        return
    action = getattr(message, "action", None)
    kind = type(action).__name__
    title = getattr(action, "title", None)
    if not title or not title.strip():
        return

    if kind == "MessageActionTopicCreate":
        save_topic(message.id, title)
        log(f"[topic] Learned topic '{title}' (ID: {message.id})")
    elif kind == "MessageActionTopicEdit":
        reply = getattr(message, "reply_to", None)
        topic_id = (
            getattr(reply, "reply_to_top_id", None)
            or getattr(reply, "reply_to_msg_id", None)
        )
        if topic_id:
            save_topic(topic_id, title)
            log(f"[topic] Updated topic {topic_id}: '{title}'")
        else:
            log("[topic] Rename observed without topic ID; registry unchanged")


@client.on(events.NewMessage(
    pattern=rf"^{GAP}/addtopic(?:@\w+)?{GAP}\s+(\d+)\s+(.+)$"
))
async def on_addtopic(event) -> None:
    """/addtopic <topic_id> <name> - register an existing topic manually."""
    if not is_allowed(event):
        return
    topic_id = int(event.pattern_match.group(1))
    title = INVISIBLE_RE.sub("", event.pattern_match.group(2)).strip()
    if topic_id <= 0 or not title:
        await event.reply("Usage: /addtopic <topic_id> <name>")
        return
    async with TOPIC_CREATE_LOCK:
        save_topic(topic_id, title)
    log(f"[topic] Manually registered topic '{title}' (ID: {topic_id})")
    await event.reply(
        f"Registered topic '{title}' (ID: {topic_id}). "
        "This records the supplied mapping; it does not verify "
        "the topic in Telegram."
    )


@client.on(events.NewMessage(pattern=rf"^{GAP}/(?:deltopic|topicrooms)(?:@\w+)?{GAP}(?:\s{GAP}(\S+))?(?:\s{GAP}((?i:purge)))?{GAP}$"))
async def on_deltopic(event) -> None:
    """/deltopic [id|all] [purge] - forget topics; purge also deletes them in Telegram."""
    if not is_allowed(event):
        return

    argument = event.pattern_match.group(1)
    purge = bool(event.pattern_match.group(2))

    # "/deltopic purge" was meant as "/deltopic" (list only).
    if argument and argument.lower() == "purge":
        argument, purge = None, True

    topics = saved_topics()

    # Without an argument: list the registry and explain the usage.
    if not argument:
        lines = []
        if topics:
            lines.append("Known topics:")
            lines.extend(
                f"- {title} (ID: {topic_id})" for topic_id, title in topics
            )
        else:
            lines.append("No topics known yet.")
        lines.append("")
        lines.append(
            "Remove one from my registry with /deltopic <topic_id>, or "
            "all of them with /deltopic all. Add the word 'purge' to "
            "also delete the forum topic in Telegram itself, for "
            "example /deltopic 5 purge."
        )
        await event.reply("\n".join(lines)[:4000], link_preview=False)
        return

    # Resolve the targets: a numeric topic ID or the word 'all'.
    if argument.lower() == "all":
        targets = list(topics)
    elif argument.isdigit():
        targets = [t for t in topics if t[0] == int(argument)]
    else:
        await event.reply(
            f"Unknown topic '{argument}'. Use the numeric ID shown by "
            "/deltopic or /topics, or the word 'all'.",
            link_preview=False,
        )
        return

    if not targets:
        await event.reply(
            "No such topic in my registry. /deltopic shows the known IDs.",
            link_preview=False,
        )
        return

    removed, purged, failed = 0, 0, []
    for topic_id, title in targets:
        forget_topic(topic_id)
        removed += 1
        if purge:
            if DeleteTopicHistoryRequest is None:
                failed.append(
                    f"{title} (ID: {topic_id}): this Telethon version cannot "
                    "delete forum topics; run pip install -U telethon"
                )
            elif topic_id == 1:
                # The 'General' topic cannot be deleted in Telegram.
                failed.append(
                    f"{title} (ID: {topic_id}): the General topic cannot be deleted"
                )
            else:
                try:
                    chat = await event.get_chat()
                    if DELETE_TOPIC_VARIANT == "messages":
                        request = DeleteTopicHistoryRequest(
                            peer=chat, top_msg_id=topic_id
                        )
                    else:
                        request = DeleteTopicHistoryRequest(
                            channel=chat, top_msg_id=topic_id
                        )
                    await event.client(request)
                    purged += 1
                except Exception as error:
                    failed.append(f"{title} (ID: {topic_id}): {error}")

    if removed == 1:
        title = targets[0][1]
        parts = [f"Removed {title} (ID: {targets[0][0]}) from my registry."]
    else:
        parts = [f"Removed {removed} topic(s) from my registry."]

    if purge:
        parts.append(f"Deleted in Telegram: {purged} of {removed}.")
        for failure in failed:
            parts.append(f"Could not delete in Telegram: {failure}")

    parts.append(
        "Future jobs no longer target it; jobs that were already queued "
        "keep the topic they were queued with."
    )
    await event.reply("\n\n".join(parts)[:4000], link_preview=False)


# ---- Startup ----
async def main() -> None:
    """Validate configuration, initialize storage, and start the bot."""
    if shutil.which("megadl"):
        log("[bot] megadl found (used for single file links and as fallback)")
    else:
        log("[bot] megadl NOT found: single file links and legacy folders will fail")

    if shutil.which("yt-dlp"):
        log("[bot] yt-dlp found (used for /yt and /nk)")
    else:
        log("[bot] yt-dlp NOT found: /yt and /nk will fail")

    if HAVE_BUNKR_DEPS:
        log("[bot] requests + beautifulsoup4 found (used for /bunkr)")
    else:
        log(
            "[bot] requests/beautifulsoup4 NOT installed: /bunkr will fail "
            "(pip install requests beautifulsoup4 lxml)"
        )

    if shutil.which("ffmpeg"):
        log("[bot] ffmpeg found (used for 720p video preparation)")
    else:
        log(
            "[bot] ffmpeg NOT found: video jobs will fail during "
            "preparation and yt-dlp cannot merge streams"
        )

    if not ALLOWED_USERS:
        raise SystemExit(
            "TG_ALLOWED_USERS is empty. The bot would not accept any users."
        )

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if WORK_DIR:
        Path(WORK_DIR).mkdir(parents=True, exist_ok=True)

    init_db()

    known_count, _ = seen_stats()
    total_skips = skipped_stats()[0]

    log(f"[bot] Chat ID: {CHAT_ID}")
    log(f"[bot] Allowed users: {', '.join(map(str, sorted(ALLOWED_USERS)))}")
    log(f"[bot] Database: {DB_PATH}")
    log(f"[bot] Work dir: {WORK_DIR or 'system temporary directory'}")
    # No job is running yet: leftovers of a killed run can go.
    with suppress(OSError):
        cleanup_stale_temp_dirs()
    log(
        f"[bot] Disk check: {DISK_SPACE_FACTOR}x file size + "
        f"{format_size(MIN_FREE_SPACE)} reserve before each MEGA download"
    )
    log(
        f"[bot] Upload retries: {UPLOAD_RETRIES} attempt(s), "
        f"{UPLOAD_RETRY_DELAY:g}s apart; flood waits up to "
        f"{f'{int(UPLOAD_FLOOD_WAIT_MAX)}s' if UPLOAD_FLOOD_WAIT_MAX > 0 else 'any length'}"
    )
    if MEGA_RESUME:
        log(
            f"[bot] MEGA resume: on ({MEGA_RESUME_DIR}, partial files "
            f"kept for {MEGA_RESUME_MAX_AGE / 3600:g} h)"
        )
        with suppress(OSError):
            prune_mega_resume()
    else:
        log("[bot] MEGA resume: off (MEGA_RESUME=0)")
    log(f"[bot] Status delete delay: {STATUS_DELETE_DELAY}s")
    log(
        f"[bot] AES backend: "
        f"{'cryptography (fast)' if HAVE_FAST_CRYPTO else 'pyaes (slow)'}"
    )
    log(
        f"[bot] MAC verification: "
        f"{'enabled' if HAVE_FAST_CRYPTO else 'disabled (needs cryptography)'}"
    )
    log(
        f"[bot] Duplicate check: on "
        f"({known_count} file(s) known, will be skipped)"
    )
    log(f"[bot] Skip history: {total_skips} duplicate skip(s) recorded")
    log(f"[bot] YouTube max size: {format_size(YT_MAX_SIZE)}")
    log(f"[bot] Netzkino max size: {format_size(NZ_MAX_SIZE)}")

    if YT_COOKIES:
        if Path(YT_COOKIES).is_file():
            log(f"[bot] yt-dlp cookies: {YT_COOKIES} (found)")
        else:
            log(
                f"[bot] yt-dlp cookies: {YT_COOKIES} does NOT exist; "
                "only public videos will work"
            )
    else:
        log("[bot] yt-dlp cookies: not set (public videos only)")

    if LARGE_FILE_ASK_TIMEOUT > 0:
        log(
            f"[bot] Large files: prompted "
            f"({LARGE_FILE_ASK_TIMEOUT}s timeout, then split)"
        )
    else:
        log("[bot] Large files: prompt disabled, always split")

    await client.start(bot_token=BOT_TOKEN)
    worker_task = asyncio.create_task(worker())

    # Graceful shutdown on SIGTERM (docker stop) and SIGINT (Ctrl+C):
    # disconnecting ends run_until_disconnected(), then the worker is
    # cancelled below. The interrupted job stays 'running' in SQLite
    # and init_db() puts it back to 'pending' on the next start.
    loop = asyncio.get_running_loop()

    def request_shutdown(sig_name: str) -> None:
        log(f"[bot] {sig_name} received, shutting down")
        spawn(client.disconnect())

    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, request_shutdown, sig.name)

    log("Bot is running.")
    try:
        await client.run_until_disconnected()
    finally:
        # Capture the child process first: the worker's cleanup resets
        # RUNNING["process"] while it unwinds.
        process = RUNNING["process"]
        # Cancel the task first, then raise the flag without an await in
        # between: the CancelledError reaches the worker before any
        # JobCancelled from a download thread, so the interrupted job
        # stays 'running' and is resumed on the next start. The flag
        # makes blocking download threads (polled per chunk) stop now
        # instead of running to the end of the current file.
        worker_task.cancel()
        RUNNING["shutdown"] = True
        RUNNING["cancel"] = True
        with suppress(asyncio.CancelledError):
            await worker_task
        # Also stop any downloader/ffmpeg child that is still alive.
        if process is not None and process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
        if client.is_connected():
            await client.disconnect()
        log("[bot] Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
