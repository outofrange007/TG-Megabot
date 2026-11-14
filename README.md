# tg-mega-bot

A Telegram bot that mirrors content into a Telegram supergroup with
forum topics. It downloads from three sources and uploads the results
straight into the group:

- **MEGA** — public file and folder links (`/mega`)
- **YouTube** — videos via yt-dlp, public or cookie-authenticated (`/yt`)
- **Netzkino** — free movies via yt-dlp's built-in extractor (`/nk`)

## Features

- Persistent SQLite job queue with `/queue`, `/cancel`, `/failed` and
  `/retry`; interrupted jobs survive a restart.
- MEGA folders are listed through the public API and downloaded file by
  file, so each file is uploaded as soon as it arrives and disk usage
  stays bounded by the largest single file. Bandwidth-limit (HTTP 509)
  retries and a bulk-download fallback are built in.
- Duplicate detection: files are skipped by name and size before the
  download and by SHA-256 hash after it (`/dupes`, `/dupes reset`).
- Large-file handling: files above ~1.9 GiB trigger a prompt —
  *Try unsplit*, *Split (N)* or *Keep on server* — with a configurable
  timeout that falls back to splitting.
- MP4 faststart optimization (stream copy, no re-encoding) so videos
  start playing immediately in Telegram.
- Forum topic support: commands accept `-t <topic_id>` and
  `-newtopic <name>`, and the bot learns topics created in the group.
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
/yt https://www.youtube.com/watch?v=XXXX -t 42
/nk https://www.netzkino.de/#!/filme/some-movie
```

## Commands

| Command | Description |
| --- | --- |
| `/mega <link(s)> [-t <topic_id>] [-newtopic <name>]` | Queue MEGA file or folder links |
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
  name and size was uploaded before; check `/dupes` and clear the
  registry with `/dupes reset` if needed.
- **Downloads fail with HTTP 509** — MEGA's bandwidth limit for your
  IP; the bot retries automatically, otherwise try again later.

## License

MIT — see [LICENSE](LICENSE).
