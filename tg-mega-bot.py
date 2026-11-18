import asyncio
import base64
import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

from telethon import TelegramClient, events, Button
from telethon.tl.functions.messages import (
    CreateForumTopicRequest,
    GetForumTopicsRequest,
)
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
SESSION_PATH = str(DATA_DIR / "bot_session")
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

# Bytes fetched from the storage server per read (also the decrypt unit).
DOWNLOAD_CHUNK = 1024 * 1024

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

# Queued jobs are prefixed with their source so the worker can pick the
# right downloader: "yt:<url>" for YouTube, "nk:<url>" for Netzkino and
# unprefixed MEGA links (the original format).

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

# Maximum number of existing forum topics fetched when checking for a
# duplicate topic name before creating a new one (10 pages of 100).
TOPIC_LOOKUP_LIMIT = 1000

client = TelegramClient(SESSION_PATH, API_ID, API_HASH)


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
RUNNING = {"job_id": None, "process": None, "cancel": False}


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
                retry_attempt INTEGER NOT NULL DEFAULT 0
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
) -> list[int]:
    """Add new jobs to the persistent queue; return their job IDs."""
    with closing(db_connect()) as db, db:
        ids: list[int] = []

        for link in links:
            cursor = db.execute(
                """
                INSERT INTO jobs (
                    link, topic_id, requester, status, auto_folder_topics,
                    force_dupes, file_delay, retry_attempt
                )
                VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    link,
                    topic_id,
                    requester,
                    int(auto_folder_topics),
                    int(force_dupes),
                    float(file_delay),
                    int(retry_attempt),
                ),
            )
            ids.append(int(cursor.lastrowid))

        return ids


def claim_job() -> (
    tuple[int, str, int | None, int, bool, bool, float, int] | None
):
    """Get the oldest pending job and mark it as running."""
    with closing(db_connect()) as db, db:
        row = db.execute(
            """
            SELECT id, link, topic_id, requester, auto_folder_topics,
                   force_dupes, file_delay, retry_attempt
            FROM jobs
            WHERE status = 'pending'
            ORDER BY id
            LIMIT 1
            """
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
        )


def finish_job(job_id: int, error: str | None = None) -> None:
    """Mark a job as done or failed."""
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
    """Remember a forum topic in the local registry."""
    with closing(db_connect()) as db, db:
        db.execute(
            "INSERT OR IGNORE INTO topics (id, title) VALUES (?, ?)",
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

            plain = aes_cbc_decrypt_zero_iv(
                unmerge_key_mac(material), mega_d64(raw)
            )
            # Attributes are "MEGA" plus a JSON object, zero padded.
            plain = plain.split(b"\x00", 1)[0]
            if not plain.startswith(b"MEGA{"):
                continue
            attrs = json.loads(plain[4:].decode("utf-8"))
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

            first = result[0]
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
        if sub_handle not in by_handle:
            raise RuntimeError(
                "Selected subfolder or file is not part of the shared folder"
            )
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

    def __init__(self, key: bytes, nonce: bytes) -> None:
        self._counter = int.from_bytes(nonce + bytes(8), "big")
        self._keystream = b""

        if HAVE_FAST_CRYPTO:
            self._openssl = Cipher(
                algorithms.AES(key), modes.CTR(nonce + bytes(8))
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


def download_file_blocking(
    url: str,
    key: bytes,
    nonce: bytes,
    expected_mac: bytes | None,
    size: int,
    dest: Path,
) -> None:
    """Download one file from a temporary URL, decrypting it on the fly."""
    log(f"[dl] Fetching from storage server ({format_size(size)})")

    decryptor = MegaCtrDecryptor(key, nonce)

    # MAC verification guards against silent corruption; it is only
    # available with the cryptography package installed.
    verifier = None
    if expected_mac and HAVE_FAST_CRYPTO:
        verifier = MegaMac(key, nonce)

    written = 0
    started = time.monotonic()
    last_log = started

    try:
        with urllib.request.urlopen(
            url, timeout=MEGA_API_TIMEOUT
        ) as response:
            with open(dest, "wb") as out:
                while True:
                    if RUNNING["cancel"]:
                        raise JobCancelled()

                    chunk = response.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break

                    plain = decryptor.update(chunk)
                    if verifier:
                        verifier.update(plain)

                    out.write(plain)
                    written += len(plain)

                    now = time.monotonic()
                    if size and now - last_log >= PROGRESS_INTERVAL:
                        last_log = now
                        speed = written / max(now - started, 0.001)
                        log(
                            f"[dl] {dest.name}: "
                            f"{written / size * 100:.1f}% "
                            f"({format_size(written)} of "
                            f"{format_size(size)}, "
                            f"{format_size(speed)}/s)"
                        )
    except urllib.error.HTTPError as exc:
        if exc.code == 509:
            raise MegaBandwidthLimit(
                "MEGA bandwidth limit exceeded (HTTP 509)"
            ) from None
        raise RuntimeError(f"HTTP {exc.code} while downloading") from None

    if verifier and verifier.digest() != expected_mac:
        raise RuntimeError("MAC verification failed (download is corrupt)")

    if written != size:
        raise RuntimeError(f"Incomplete download: {written} of {size} bytes")


async def download_mega_file(
    folder_handle: str,
    entry: tuple[str, str, int, bytes, str | None],
    dest_dir: Path,
) -> Path:
    """Download one folder file into dest_dir and return its path."""
    name, handle, size, material = entry[:4]
    key = unmerge_key_mac(material)
    nonce = material[16:24] if len(material) == 32 else bytes(8)
    expected_mac = material[24:32] if len(material) == 32 else None

    # Names can carry the folder path; the file itself gets a flat name.
    dest = dest_dir / (Path(name).name or handle)

    for attempt in range(1, MEGA_DOWNLOAD_RETRIES + 1):
        try:
            url, info_size = await asyncio.to_thread(
                fetch_download_info, handle, folder_handle
            )
            await asyncio.to_thread(
                download_file_blocking,
                url,
                key,
                nonce,
                expected_mac,
                info_size or size,
                dest,
            )
            return dest
        except JobCancelled:
            raise
        except MegaBandwidthLimit as exc:
            if attempt >= MEGA_DOWNLOAD_RETRIES:
                raise

            log(
                f"[dl] {exc}; retrying in {BANDWIDTH_RETRY_DELAY}s "
                f"(attempt {attempt + 1}/{MEGA_DOWNLOAD_RETRIES})"
            )
            await asyncio.sleep(BANDWIDTH_RETRY_DELAY)

    raise RuntimeError("unreachable")


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
    Single file links are handled by megadl directly. The optional
    stats dict collects files skipped before any download; job_id is
    stored with every recorded skip event. file_folders preserves the
    original folder path for per-folder topic routing in the worker,
    file_handles the MEGA node handle for the pre-download duplicate
    check of later jobs. With force_dupes set, the duplicate checks are bypassed so files
    already uploaded in earlier jobs are downloaded and sent again.
    file_delay overrides the per-file pacing; the automatic re-queue
    after an API error -16 abort uses it to slow down each attempt.
    """
    if RUNNING["cancel"]:
        raise JobCancelled()

    if link_kind(link) == "file":
        for path in await run_megadl(link, temp_dir):
            yield path

        return

    match = re.search(r"/folder/([\w-]+)#([\w-]+)", link)
    folder_handle = match.group(1) if match else None
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
                mark_skipped(Path(name).name, size, "node", job_id)
                log(
                    f"[dl] ({index}/{total}) Skipping {name}: "
                    "already uploaded (same file node)"
                )
                continue

            if seen_by_name_size(Path(name).name, size):
                stats["skipped_pre"] = stats.get("skipped_pre", 0) + 1
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

        # Fresh subfolder per file so exactly one file accumulates.
        sub_dir = temp_dir / f"file{index:04d}"
        sub_dir.mkdir()

        try:
            path = await download_mega_file(folder_handle, entry, sub_dir)
        except JobCancelled:
            raise
        except MegaThrottled as exc:
            # MEGA answered -16. Skipping to the next file only hammers
            # the API with the same request, so slow down, probe the
            # share and stop the job when the throttle does not lift.
            failed += 1
            fail_streak += 1
            throttle_streak += 1
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
    """Return a Telethon progress callback that logs every 10 percent."""
    last_percent = 0

    def callback(current: int, total: int) -> None:
        # Raising inside the callback aborts the upload.
        if RUNNING["cancel"]:
            raise JobCancelled()

        nonlocal last_percent
        percent = int(current / total * 100) if total else 100
        if percent >= last_percent + 10:
            last_percent = percent
            log(
                f"[ul] {name}: {percent}% "
                f"({format_size(current)} of {format_size(total)})"
            )

    return callback


async def send_file(path: Path, topic_id: int | None, progress=None) -> None:
    """Upload one file without a caption. Telegram detects the media type."""
    await client.send_file(
        CHAT_ID,
        str(path),
        force_document=False,
        # MP4 videos get the streaming flag so Telegram offers instant
        # playback instead of a plain file download.
        supports_streaming=True,
        reply_to=topic_id,
        progress=progress,
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
            asyncio.create_task(
                delete_status([prompt], STATUS_DELETE_DELAY)
            )
        except Exception:
            pass

        return ASK_SPLIT

    try:
        await prompt.edit(f"{path.name}: {ASK_ANSWERS[choice]}")
        asyncio.create_task(delete_status([prompt], STATUS_DELETE_DELAY))
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
    """Return the cached topic ID or create one for this MEGA folder.

    Topics that already exist in the group under the same name
    (case-insensitive) are reused instead of duplicated.
    """
    if folder_name in topic_cache:
        return topic_cache[folder_name]

    title = clamp_topic_name(folder_name)
    if not title:
        raise RuntimeError("MEGA folder name is empty; cannot create topic")

    topic_id, _created = await get_or_create_topic(title)
    topic_cache[folder_name] = topic_id
    return topic_id


async def create_topic(name: str) -> int:
    """Create a forum topic and return its topic ID."""
    result = await client(
        CreateForumTopicRequest(
            peer=await client.get_input_entity(CHAT_ID),
            title=name,
            icon_color=random.choice(TOPIC_ICON_COLORS),
            random_id=random.getrandbits(63),
        )
    )

    # The response contains the topic-creation service message; its ID
    # is the topic ID used for posting.
    for update in getattr(result, "updates", []):
        message = getattr(update, "message", None)
        action = getattr(message, "action", None)
        if type(action).__name__ == "MessageActionTopicCreate":
            return message.id

    raise RuntimeError("Topic created, but its ID could not be determined.")


async def find_existing_topic(title: str) -> int | None:
    """Return the topic ID of an existing forum topic with this name.

    The Telegram group itself is queried (not the local registry), so
    topics that were created manually or outside this bot are found as
    well. The comparison is case-insensitive and ignores surrounding
    whitespace. Returns None when no topic matches or the lookup
    fails, so a broken lookup never blocks topic creation.
    """
    wanted = title.strip().casefold()
    if not wanted:
        return None

    peer = await client.get_input_entity(CHAT_ID)
    offset_date = 0
    offset_id = 0
    offset_topic_id = 0
    fetched = 0

    while fetched < TOPIC_LOOKUP_LIMIT:
        try:
            result = await client(
                GetForumTopicsRequest(
                    peer=peer,
                    offset_date=offset_date,
                    offset_id=offset_id,
                    offset_topic_id=offset_topic_id,
                    limit=100,
                )
            )
        except Exception as exc:
            log(f"[topic] Topic lookup failed: {exc}")
            return None

        topics = list(getattr(result, "topics", []) or [])
        if not topics:
            break

        # Each topic's root service message comes with the response;
        # its date feeds the pagination offset of the next page.
        root_messages = {
            message.id: message
            for message in getattr(result, "messages", []) or []
        }

        for topic in topics:
            topic_title = getattr(topic, "title", "") or ""
            if topic_title.strip().casefold() == wanted:
                topic_id = getattr(topic, "id", None)
                if topic_id:
                    return topic_id

        fetched += len(topics)

        if len(topics) < 100:
            break

        # Continue with the page after the last returned topic.
        last = topics[-1]
        offset_topic_id = getattr(last, "id", 0) or 0
        offset_id = offset_topic_id
        root_message = root_messages.get(offset_topic_id)
        if root_message is not None and getattr(root_message, "date", None):
            offset_date = int(root_message.date.timestamp())

    return None


async def get_or_create_topic(name: str) -> tuple[int, bool]:
    """Return the topic ID for name, reusing an existing topic.

    Returns (topic_id, created). created is False when a topic with
    the same name (case-insensitive) already exists in the group; that
    topic is then reused instead of creating a duplicate.
    """
    existing_id = await find_existing_topic(name)

    if existing_id is not None:
        log(
            f"[topic] Topic '{name}' already exists "
            f"(ID: {existing_id}); reusing it"
        )
        save_topic(existing_id, name)
        return existing_id, False

    topic_id = await create_topic(name)
    save_topic(topic_id, name)
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
        return int(topic_match.group(1)), None

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


# ---- Video preparation for Telegram ----
# Telegram streams videos best from MP4 files (H.264/AAC) whose index
# sits at the front of the file ("faststart"). Every downloaded video
# is therefore normalized before it is uploaded:
#
#   .mp4/.m4v/.mov    repacked with faststart; streams are copied.
#   other containers  remuxed into MP4 (stream copy: fast, lossless).
#
# Codecs Telegram cannot play trigger a conversion: first only the
# audio track is converted to AAC (the video stream is kept), and if
# the video codec itself is unsupported (DivX, MPEG2, VP9, ...) the
# whole video is re-encoded to H.264/AAC, which is slow. Non-video
# files, and any video FFmpeg cannot process, are uploaded unchanged.
# Set VIDEO_REENCODE=0 to disable the slow full re-encode.
VIDEO_EXTENSIONS = {
    ".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".flv",
    ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp", ".ogv", ".vob",
}
FASTSTART_EXTENSIONS = {".mp4"}
VIDEO_REENCODE = (
    os.environ.get("VIDEO_REENCODE", "1").strip().lower()
    not in ("0", "false", "no")
)

# Seconds per FFmpeg run before it is killed and the original file is
# uploaded unchanged instead.
FFMPEG_TIMEOUT = 7200


def probe_video_codecs(path: Path) -> tuple[bool, bool]:
    """Check with ffprobe whether a video's codecs are Telegram-friendly.

    Returns (video_ok, audio_ok); ok means every stream of that kind
    can be copied into an MP4 that Telegram plays. A file without an
    audio stream counts as audio_ok, and any probe problem counts as
    ok so the caller simply tries the lossless remux first.
    """
    command = [
        "ffprobe", "-v", "error",
        "-show_entries", "stream=codec_type,codec_name",
        "-of", "json", str(path),
    ]
    try:
        result = subprocess.run(
            command, check=True, capture_output=True, timeout=60
        )
        streams = json.loads(result.stdout).get("streams", [])
    except Exception as error:
        log(f"[opt] Could not probe {path.name}: {error}")
        return True, True

    video_ok, audio_ok = True, True
    for stream in streams:
        if stream.get("disposition", {}).get("attached_pic"):
            continue  # cover art, not a real video stream
        codec = stream.get("codec_name", "")
        if stream.get("codec_type") == "video":
            video_ok = video_ok and codec in ("h264", "hevc", "h265")
        elif stream.get("codec_type") == "audio":
            audio_ok = audio_ok and codec in ("aac", "mp3")
    return video_ok, audio_ok


def _run_ffmpeg(command: list[str], what: str) -> bool:
    """Run one FFmpeg command; return True on success.

    The last element of the command must be the output file; it is
    removed again when the run fails or writes nothing.
    """
    try:
        log(f"[opt] {what}")
        subprocess.run(command, check=True, timeout=FFMPEG_TIMEOUT)
        if not os.path.getsize(command[-1]):
            raise RuntimeError("no output was written")
        return True
    except Exception as error:
        log(f"[opt] {what} failed: {error}")
        Path(command[-1]).unlink(missing_ok=True)
        return False


def prepare_video_for_telegram(path: Path) -> Path:
    """Bring one video into Telegram's preferred form: MP4 + faststart.

    Returns the path to upload. On success the original download is
    consumed (replaced in place, or renamed to .mp4); on any problem
    the input path is returned unchanged so the upload proceeds with
    the original file.
    """
    if path.suffix.lower() not in VIDEO_EXTENSIONS:
        return path

    if not shutil.which("ffmpeg"):
        log(f"[opt] FFmpeg not found; leaving {path.name} unchanged")
        return path

    # The MP4 family keeps its name; every other container becomes
    # .mp4 so Telegram always receives a real MP4 video.
    is_mp4 = path.suffix.lower() in FASTSTART_EXTENSIONS
    if is_mp4:
        target = path
    else:
        target = path.with_suffix(".mp4")
        counter = 1
        while target.exists():
            target = path.with_name(f"{path.stem}_{counter}.mp4")
            counter += 1

    # FFmpeg always writes a sibling temp file first, so a failed run
    # can never destroy the downloaded original.
    output = path.with_name(f"{path.stem}.opt.mp4")

    video_ok, audio_ok = probe_video_codecs(path)
    attempts: list[tuple[str, list[str]]] = []

    if video_ok and audio_ok:
        # All codecs are Telegram-friendly: a pure remux suffices
        # (faststart for MP4 sources, a container change otherwise).
        attempts.append((
            f"Repacking {path.name} with faststart" if is_mp4
            else f"Converting {path.name} to MP4 (stream copy)",
            ["-c", "copy", "-movflags", "+faststart"],
        ))

    if video_ok and not audio_ok:
        # Video is fine, the audio is not (DTS, Opus, ...): keep the
        # video stream and convert the audio only.
        attempts.append((
            f"Converting the audio of {path.name} to AAC",
            ["-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
             "-movflags", "+faststart"],
        ))

    if not video_ok and VIDEO_REENCODE:
        # Unsupported video codec (DivX, MPEG2, VP9, ...): a full
        # re-encode also fixes the audio in the same pass.
        attempts.append((
            f"Re-encoding {path.name} to H.264/AAC (this is slow)",
            ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"],
        ))

    for what, codec_args in attempts:
        # Subtitles and data streams cannot live in an MP4 and would
        # abort the remux of typical MKVs, so they are dropped.
        command = [
            "ffmpeg", "-y", "-nostdin", "-i", str(path), *codec_args,
            "-sn", "-dn", "-loglevel", "error", str(output),
        ]
        if _run_ffmpeg(command, what):
            output.replace(target)
            if target is not path:
                path.unlink(missing_ok=True)
            log(
                f"[opt] {target.name} ready "
                f"({format_size(target.stat().st_size)})"
            )
            return target

    log(f"[opt] Leaving {path.name} unchanged")
    return path


# ---- Queue worker ----
async def worker() -> None:
    """Process queued jobs one at a time."""
    log("[worker] Queue worker started")

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
        ) = job
        RUNNING.update(job_id=job_id, process=None, cancel=False)
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
                """Dedup-check, optimize and upload one downloaded file."""
                nonlocal file_count, kept_count, skipped_count

                hash_value = await asyncio.to_thread(file_hash, path)

                # -dupes re-sends files the registry already knows.
                if not force_dupes and seen_file(hash_value):
                    skipped_count += 1
                    mark_skipped(
                        path.name,
                        path.stat().st_size,
                        "hash",
                        job_id,
                    )
                    log(
                        f"[worker] Job #{job_id}: duplicate skipped: "
                        f"{path.name} ({hash_value[:16]}...)"
                    )
                    path.unlink(missing_ok=True)
                    return

                # Name and size of the original download go into the
                # duplicate registry, so it keeps matching the source
                # file even when the upload is converted (MKV to MP4).
                original_name = path.name
                original_size = path.stat().st_size

                # Topic routing is keyed by the original download path;
                # look the folder up before the video conversion can
                # rename the file.
                folder_name = file_folders.pop(str(path), None)
                node_handle = file_handles.pop(str(path), None)

                # Normalize videos for Telegram (MP4 with faststart);
                # a no-op for other file types or without FFmpeg.
                upload_path = await asyncio.to_thread(
                    prepare_video_for_telegram, path
                )

                file_topic_id = topic_id
                if auto_folder_topics and folder_name:
                    try:
                        file_topic_id = await get_or_create_folder_topic(
                            folder_name, folder_topics
                        )
                    except Exception as exc:
                        raise RuntimeError(
                            f"Could not create topic for MEGA folder "
                            f"'{folder_name}': {exc}"
                        ) from exc

                if await upload_one(upload_path, file_topic_id):
                    file_count += 1
                    mark_seen(
                        hash_value,
                        original_name,
                        original_size,
                        job_id,
                        node_handle,
                    )
                    upload_path.unlink(missing_ok=True)
                    log(f"[worker] Job #{job_id}: uploaded {upload_path.name}")
                else:
                    # Kept on the server: not sent, so it stays
                    # unrecorded in the duplicate registry and the same
                    # link queued again will be downloaded and asked
                    # about again instead of being skipped.
                    kept_count += 1
                    log(
                        f"[worker] Job #{job_id}: kept {upload_path.name} "
                        "on the server"
                    )

            with tempfile.TemporaryDirectory(dir=WORK_DIR) as temp_dir:
                log(f"[worker] Job #{job_id} using temp dir {temp_dir}")

                if link.startswith("yt:"):
                    # YouTube video(s) via yt-dlp.
                    for path in await run_ytdlp(
                        link[3:], Path(temp_dir), YT_MAX_SIZE
                    ):
                        await handle_downloaded(path)

                elif link.startswith("nk:"):
                    # Netzkino movie via yt-dlp (built-in extractor).
                    for path in await run_ytdlp(
                        link[3:], Path(temp_dir), NZ_MAX_SIZE
                    ):
                        await handle_downloaded(path)

                else:
                    # MEGA: download and upload overlap file by file,
                    # so disk usage stays bounded by the largest file.
                    async for path in iter_download(
                        link,
                        Path(temp_dir),
                        job_id,
                        stats,
                        file_folders,
                        force_dupes,
                        file_delay,
                        file_handles,
                    ):
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
            asyncio.create_task(
                delete_status(
                    [start_message, done_message],
                    STATUS_DELETE_DELAY,
                )
            )

        except JobCancelled:
            finish_job(job_id, error="Cancelled by user")

            log(f"[worker] Job #{job_id} cancelled by user.")
            await notify(f"Job #{job_id} cancelled.", topic_id)

            # Keep the cancellation message visible.
            asyncio.create_task(delete_status([start_message], 0))

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
                )[0]
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
            asyncio.create_task(delete_status([start_message], 0))

        except Exception as exc:
            error = str(exc) or type(exc).__name__
            finish_job(job_id, error=error)

            log(f"[worker] Job #{job_id} FAILED: {error}")
            await notify(f"Job #{job_id} failed: {error}", topic_id)

            # Keep the error message visible, but remove the start message.
            asyncio.create_task(delete_status([start_message], 0))

        finally:
            RUNNING.update(job_id=None, process=None, cancel=False)

        _, pending = queue_status()
        log(f"[worker] Pending jobs: {pending}")


# ---- Telegram commands ----
@client.on(events.NewMessage(pattern=rf"^{GAP}/mega(?:@\w+)?{GAP}(?:\s|$)"))
async def on_mega(event) -> None:
    """/mega <link(s)> [-t topic_id] [-newtopic <name>] [-dupes]."""
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

    links = clean_urls(MEGA_REGEX.findall(text))

    if not links:
        await event.reply(
            "Usage: /mega <mega-link(s)> [-t topic_id] "
            "[-newtopic <name>] [-dupes]"
        )
        return

    topic_id, error = await resolve_target_topic(
        event, text,
        "Usage: /mega <mega-link(s)> -newtopic <name> [-dupes]",
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
    )
    _, pending = queue_status()

    acknowledgement = await event.reply(
        f"Saved {len(links)} link(s). Pending: {pending}"
    )

    # Delete the acknowledgement after the configured delay.
    asyncio.create_task(
        delete_status([acknowledgement], STATUS_DELETE_DELAY)
    )


@client.on(events.NewMessage(pattern=rf"^{GAP}/yt(?:@\w+)?{GAP}(?:\s|$)"))
async def on_yt(event) -> None:
    """/yt <url(s)> [-t <topic_id>] [-newtopic <name>] - queue YouTube videos."""
    if not is_allowed(event):
        return

    text = INVISIBLE_RE.sub("", event.raw_text)
    log(f"[yt] Command from {event.sender_id}: {text!r}")

    urls = clean_urls(YT_REGEX.findall(text))

    if not urls:
        await event.reply(
            "Usage: /yt <youtube-url> [-t topic_id] [-newtopic <name>]"
        )
        return

    topic_id, error = await resolve_target_topic(
        event, text, "Usage: /yt <youtube-url> -newtopic <name>"
    )
    if error:
        await event.reply(error)
        return

    add_jobs([f"yt:{url}" for url in urls], topic_id, event.sender_id)
    _, pending = queue_status()

    acknowledgement = await event.reply(
        f"Saved {len(urls)} video(s). Pending: {pending}"
    )
    asyncio.create_task(
        delete_status([acknowledgement], STATUS_DELETE_DELAY)
    )


@client.on(events.NewMessage(pattern=rf"^{GAP}/nk(?:@\w+)?{GAP}(?:\s|$)"))
async def on_nk(event) -> None:
    """/nk <url(s)> [-t <topic_id>] [-newtopic <name>] - queue Netzkino movies."""
    if not is_allowed(event):
        return

    text = INVISIBLE_RE.sub("", event.raw_text)
    log(f"[nk] Command from {event.sender_id}: {text!r}")

    urls = clean_urls(NETZKINO_REGEX.findall(text))

    if not urls:
        await event.reply(
            "Usage: /nk <netzkino-url> [-t topic_id] [-newtopic <name>]"
        )
        return

    topic_id, error = await resolve_target_topic(
        event, text, "Usage: /nk <netzkino-url> -newtopic <name>"
    )
    if error:
        await event.reply(error)
        return

    add_jobs([f"nk:{url}" for url in urls], topic_id, event.sender_id)
    _, pending = queue_status()

    acknowledgement = await event.reply(
        f"Saved {len(urls)} movie(s). Pending: {pending}"
    )
    asyncio.create_task(
        delete_status([acknowledgement], STATUS_DELETE_DELAY)
    )


@client.on(events.NewMessage(pattern=rf"^{GAP}/queue(?:@\w+)?{GAP}$"))
async def on_queue(event) -> None:
    """/queue - show the running job and number of pending jobs."""
    if not is_allowed(event):
        return

    running_id, pending = queue_status()
    running_text = f"#{running_id}" if running_id is not None else "none"

    await event.reply(
        f"Current job: {running_text}\nPending: {pending}"
    )


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
        "/mega <link(s)> [-t <topic_id>] [-newtopic <name>] [-dupes] - "
        "queue MEGA links\n"
        "/yt <url(s)> [-t <topic_id>] [-newtopic <name>] - queue YouTube videos\n"
        "/nk <url(s)> [-t <topic_id>] [-newtopic <name>] - queue Netzkino movies\n"
        "/queue - show the running job and pending count\n"
        "/cancel [job_id] - cancel the running job or remove a pending one\n"
        "/failed - list recent failed jobs\n"
        "/retry [job_id] - re-queue failed jobs\n"
        "/newtopic <name> - create a forum topic\n"
        "/topics - list known forum topics\n"
        "/deltopic [id|all] [purge] - remove topics from the registry\n"
        "/dupes [reset] - duplicate registry and skip history\n"
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


@client.on(events.NewMessage())
async def watch_topic_creation(event) -> None:
    """Record topic-creation service messages to learn new topic IDs."""
    if event.chat_id != CHAT_ID:
        return

    action = getattr(event.message, "action", None)
    if type(action).__name__ != "MessageActionTopicCreate":
        return

    save_topic(event.message.id, action.title)
    print(f"Learned new topic: {action.title} (ID: {event.message.id})", flush=True)


@client.on(events.NewMessage(pattern=rf"^{GAP}/(?:deltopic|topicrooms)(?:@\w+)?{GAP}(?:\s{GAP}(\S+))?(?:\s{GAP}(purge))?{GAP}$"))
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

    if not ALLOWED_USERS:
        raise SystemExit(
            "TG_ALLOWED_USERS is empty. The bot would not accept any users."
        )

    DATA_DIR.mkdir(parents=True, exist_ok=True)

    if WORK_DIR:
        Path(WORK_DIR).mkdir(parents=True, exist_ok=True)

    init_db()

    known_count, _ = seen_stats()
    total_skips, _, _ = skipped_stats()

    log(f"[bot] Chat ID: {CHAT_ID}")
    log(f"[bot] Allowed users: {', '.join(map(str, sorted(ALLOWED_USERS)))}")
    log(f"[bot] Database: {DB_PATH}")
    log(f"[bot] Work dir: {WORK_DIR or 'system temporary directory'}")
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
    asyncio.create_task(worker())

    log("Bot is running.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())