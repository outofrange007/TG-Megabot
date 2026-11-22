# tg-mega-bot

A Telegram bot that mirrors content into a Telegram supergroup with
forum topics. It downloads from four sources and uploads the results
straight into the group:

- **MEGA** — public file and folder links (`/mega`)
- **Bunkr** — public albums and single files (`/bunkr`, alias `/bk`)
- **YouTube** — videos via yt-dlp, public or cookie-authenticated (`/yt`)
- **Netzkino** — free movies via yt-dlp's built-in extractor (`/nk`)

## Features

- Persistent SQLite job queue with `/queue`, `/cancel`, `/failed` and
  `/retry`; interrupted jobs survive a restart.
- MEGA folders are listed through the public API and downloaded file by
  file, so each file is uploaded as soon as it arrives and disk usage
  stays bounded by the largest single file. Bandwidth-limit (HTTP 509)
  retries and a bulk-download fallback are built in.
- MEGA throttling (API error -16) is absorbed automatically: the
  per-file delay is raised step by step, the share is cooled down,
  and jobs aborted by it are re-queued with a doubled delay.
- Duplicate detection: files are skipped by name and size before the
  download and by SHA-256 hash after it (`/dupes`, `/dupes reset`).
  The `-dupes` flag re-transfers known files for a single job.
- Large-file handling: files above ~1.9 GiB trigger a prompt —
  *Try unsplit*, *Split (N)* or *Keep on server* — with a configurable
  timeout that falls back to splitting.
- Optional video preparation with `-recode` (see *Video preparation*):
  videos that are already H.264/AAC within 720p are only remuxed
  (stream copy, no quality loss); all others are re-encoded to
  H.264/AAC with a 720p limit. Every video gets MP4 faststart and has
  its metadata removed.
- Image cleanup with Pillow: EXIF orientation is applied, then all
  metadata (EXIF/XMP/text chunks) is stripped before upload.
- Bunkr albums are processed file by file; each file is downloaded,
  checked for duplicates and uploaded on its own. An album without a
  topic target gets its own topic named after the album.
- Forum topic support: commands accept `-t <topic_id>` and
  `-newtopic <name>`, and the bot learns topics created in the group.
  A plain `/mega` folder link without a topic target automatically
  gets one topic per top-level folder of the share.
- Command parsing tolerates invisible Unicode characters from pasted
  text, and only users listed in `TG_ALLOWED_USERS` may use the bot.

## Requirements

- Docker with Compose **or** Python 3.12+ with the binaries
  `megatools`, `ffmpeg` and `yt-dlp` on the PATH.
- A Telegram bot token ([@BotFather](https://t.me/BotFather)), an API
  ID/hash ([my.telegram.org](https://my.telegram.org)) and the numeric
  ID of a supergroup where the bot is a member.

## Setup

1. Clone the repository and copy the environment template:

   ```bash
   cp .env.example .env
   ```

2. Fill in `.env` (see the configuration table below).

3. Start the stack:

   ```bash
   docker compose up -d --build
   ```

   Without Docker: install `megatools`, `ffmpeg` and `yt-dlp` with your
   package manager, then `pip install -r requirements.txt` and run
   `python tg-mega-bot.py`.

The script is bind-mounted into the container, so code changes only
need a container restart, not a rebuild.

## Usage

Queue links from any allowed user in the configured supergroup. Every
download runs one at a time in a persistent queue; progress is logged
to the container's stdout and short status messages are posted into
the target topic.

```
/mega https://mega.nz/folder/XXXX#YYYY -newtopic Movies
/mega https://mega.nz/folder/XXXX#YYYY -dupes
/bunkr https://bunkr.si/a/XXXX -newtopic Album
/yt https://www.youtube.com/watch?v=XXXX -t 42
/nk https://www.netzkino.de/#!/filme/some-movie
```

A plain `/mega` folder link — without any topic flag — creates one
forum topic per top-level folder of the share (see below), and
`-dupes` re-transfers files the bot has already uploaded.

## Commands

| Command | Description |
| --- | --- |
| `/mega <link(s)> [-t <topic_id>] [-newtopic <name>] [-dupes]` | Queue MEGA file or folder links |
| `/bunkr <url(s)> [-t <topic_id>] [-newtopic <name>] [-dupes]` | Queue Bunkr albums or files (alias `/bk`) |
| `/yt <url(s)> [-t <topic_id>] [-newtopic <name>]` | Queue YouTube videos |
| `/nk <url(s)> [-t <topic_id>] [-newtopic <name>]` | Queue Netzkino movies |
| `/queue` | Show the running job and pending count |
| `/cancel [job_id]` | Cancel the running job or remove a pending one |
| `/failed` | List recent failed jobs with their errors |
| `/retry [job_id]` | Re-queue failed jobs |
| `/newtopic <name>` | Create a forum topic |
| `/topics` | List known forum topics |
| `/deltopic [id\|all] [purge]` | Forget topics; `purge` also deletes them in Telegram |
| `/dupes [reset]` | Show (or clear) the duplicate registry and skip history |
| `/help` | Show all commands |

## Automatic folder topics

When a `/mega` **folder** link is queued without any topic target —
no `-newtopic`, no `-t`, and not sent as a reply inside a topic — the
bot creates one forum topic per top-level folder of the share, named
after that folder, and uploads each file into its folder's topic.
Files sitting in the share root are posted without a topic. Subfolders
do not get their own topics; everything below a top-level folder lands
in that folder's topic. Topics are created on demand with the bot's
*Manage Topics* right and remembered like manually created ones.

As soon as a target is given (`-newtopic`, `-t`, or a reply inside a
topic), every file goes to that single target instead.

## Re-downloading duplicates (`-dupes`)

By default the bot skips files it has already uploaded: by name and
size before the download, and by SHA-256 hash after it. Appending
`-dupes` to a `/mega` command turns both checks off for that job, so
everything in the link is downloaded and sent again:

```
/mega https://mega.nz/folder/XXXX#YYYY -dupes
```

The registry itself is not touched — it can be combined freely with
`-t` and `-newtopic`, and later jobs without the flag skip as usual.

## Video preparation

By default every video (MP4, MKV, WebM, AVI, MOV, ...) is copied into a
Telegram-optimised MP4 container by stream copy (no re-encoding, faststart, subtitles and
metadata dropped). If the codecs cannot be stored in MP4 (e.g. WMV), the
original file is uploaded unchanged. Adding
`-recode` to `/mega`, `/bunkr`, `/yt` or `/nk` (or setting
`VIDEO_REENCODE=1` for all jobs) enables the preparation below.

With `-recode`, every video is inspected with `ffprobe`. The size check
is orientation-independent: the **long edge must be ≤ 1280** and the
**short edge ≤ 720**, so both 1280×720 (landscape) and 720×1280
(portrait) pass.

A video is only **remuxed** (stream copy, takes seconds, no quality
loss) when all of these hold:

- video codec H.264 with pixel format `yuv420p`
- long edge ≤ 1280 and short edge ≤ 720
- audio codec AAC, or no audio track

Otherwise it is **re-encoded** with libx264 (CRF 26, preset `slow`),
AAC 128k, and scaled down into the 720p box; smaller videos are never
enlarged. If `ffprobe` fails, the bot re-encodes to be safe. In both
cases only the first video and audio stream are kept, metadata and
chapters are removed and MP4 faststart is applied. The log line
`[opt] Remuxing ...` or `[opt] Re-encoding ... (codec=..., WxH, ...)`
shows which path was taken and why.

## Bunkr

`/bunkr` accepts album links (`/a/<id>`) and single file links
(`/f/`, `/v/`, `/i/`) on any Bunkr domain (`bunkr.<tld>` or `bunkrr.<tld>`, e.g. `bunkr.cr`,
`bunkr.si`, `bunkr.su`), so new mirrors work without a code change. Bunkr
links pasted into `/mega` are queued as well. Rate limits (HTTP 429),
server errors and connection drops are retried with 10/30/60 s
back-off; HTTP 403/404 fail the file immediately. A failing file does
not stop the album. The pages are parsed as static HTML — content that
Bunkr only loads via JavaScript cannot be found and produces a clear
error.

## Large files

Telegram limits a single upload to about 2 GiB. When a downloaded file
exceeds the internal threshold (~1.9 GiB), the bot asks what to do:

- **Try unsplit** — one upload attempt as a single file; if Telegram
  rejects it, the bot splits automatically.
- **Split (N)** — cut the file into N byte-exact parts named
  `video.mp4.001`, `video.mp4.002`, ... Reassemble them locally with
  `cat "video.mp4."* > video.mp4` (Windows:
  `copy /b video.mp4.001 + video.mp4.002 video.mp4`).
- **Keep on server** — move the file into `/data/kept` and skip the
  upload.

If nobody answers within `LARGE_FILE_ASK_TIMEOUT` seconds (default
120), the file is split. Set the variable to `0` to disable the prompt
entirely.

## MEGA throttling (API error -16)

MEGA rate-limits heavy folder downloads with API error -16 ("handle
expired or rate limited"). The bot handles this on its own: the delay
between file downloads is raised step by step, the share is paused
for `MEGA_COOLDOWN` seconds after repeated failures and probed
again, and a job that had to be aborted is re-queued automatically
up to `MEGA_AUTO_RETRIES` times — each run starts with double the
`MEGA_FILE_DELAY` of the previous one. Only if all automatic runs
fail does the job end up in `/failed`, ready for a manual `/retry`
later.

## YouTube cookies (optional)

Public videos work without any login. For age-restricted or
members-only videos, export a cookies file from a browser in Netscape
format, place it next to `docker-compose.yml` as `cookies.txt`,
uncomment the cookie mount line in the compose file and set
`YT_COOKIES=/cookies.txt` in `.env`.

Cookies are used for YouTube downloads only — they are never sent to
other services.

## Netzkino note

Netzkino serves its video CDN only to IP addresses from Germany,
Austria and Switzerland. The bot must run on a server in the DACH
region for `/nk` to work; elsewhere downloads fail with HTTP 403.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `TG_API_ID` / `TG_API_HASH` | required | Telegram API credentials from my.telegram.org |
| `TG_BOT_TOKEN` | required | Bot token from @BotFather |
| `TG_CHAT_ID` | required | Numeric ID of the target supergroup |
| `TG_ALLOWED_USERS` | required | Comma-separated user IDs allowed to use the bot |
| `STATUS_DELETE_DELAY` | `60` | Seconds before temporary status messages are deleted (`0` = immediately) |
| `DATA_DIR` | `/data` | Persistent state: SQLite database, session, kept files |
| `WORK_DIR` | `/work` | Scratch space for downloads |
| `YT_COOKIES` | unset | Path to a cookies file for authenticated YouTube downloads |
| `YT_MAX_SIZE` | `1900` | Per-video download cap for YouTube, in MiB |
| `NZ_MAX_SIZE` | `1900` | Per-video download cap for Netzkino, in MiB |
| `LARGE_FILE_ASK_TIMEOUT` | `120` | Seconds to wait for the large-file choice (`0` = always split) |
| `MEGA_FILE_DELAY` | `0.25` | Seconds between consecutive MEGA file downloads; raised automatically while MEGA throttles |
| `MEGA_FAIL_STREAK` | `10` | Consecutive MEGA failures that trigger a share cooldown |
| `MEGA_COOLDOWN` | `300` | Seconds a throttled MEGA share is paused before it is probed again |
| `MEGA_AUTO_RETRIES` | `3` | Automatic re-queue runs for a MEGA job aborted by API error -16; each run doubles `MEGA_FILE_DELAY` |
| `BUNKR_FILE_DELAY` | `1.0` | Seconds between consecutive Bunkr file downloads |
| `SESSION_NAME` | `bot_session` | File name of the Telethon session inside `DATA_DIR` |
| `VIDEO_REENCODE` | `0` | `1` applies 720p preparation to every job, as if `-recode` were always given |

## Troubleshooting

- **"Topics are not enabled"** — enable *Topics* in the group settings.
- **Topic creation fails with an admin error** — make the bot an admin
  with the *Manage Topics* right.
- **`yt-dlp NOT found` in the log** — rebuild the image or install
  `yt-dlp`; `/yt` and `/nk` fail without it.
- **`ERROR: [Errno 21] Is a directory: '/cookies.txt'`** — the mount
  points at a file that does not exist on the host, so Docker created
  a directory instead; create the file first with
  `touch cookies.txt` and redeploy.
- **Cookies ignored** — check that `YT_COOKIES` points to a mounted
  file, not a directory, and that the bot log says the file was found.
- **Duplicate skipped although the file is new** — a file with the same
  name and size was uploaded before; check `/dupes`, re-transfer with
  the `-dupes` flag, or clear the registry with `/dupes reset`.
- **No per-folder topics were created** — automatic folder topics only
  apply when the `/mega` command has no `-newtopic`, no `-t` and is
  not sent as a reply inside a topic.
- **Downloads fail with HTTP 509** — MEGA's bandwidth limit for your
  IP; the bot retries automatically, otherwise try again later.
- **Downloads fail with MEGA API error -16** — MEGA is throttling
  the share. The bot slows down and re-queues aborted jobs
  automatically; if a job still ends up failed, wait a while and
  run `/retry` later.

## License

MIT — see [LICENSE](LICENSE).