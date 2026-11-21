FROM python:3.12-slim

# Unbuffered logs; no .pyc files; no pip version nag.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# megatools provides megadl for MEGA single-file downloads and bulk fallback.
# ffmpeg provides 720p H.264/AAC video preparation with MP4 faststart and
# stream merging for yt-dlp. ca-certificates is needed for HTTPS (MEGA API,
# Bunkr CDN). curl is used only for the build-time CDN connectivity hint.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        megatools ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -hide_banner -version | head -n 1 \
    && command -v megadl

WORKDIR /app

# Install Python dependencies first for better layer caching.
# Included packages:
#   telethon       — Telegram client
#   pyaes / cryptography — MEGA decryption
#   Pillow         — image EXIF cleanup and orientation fix
#   yt-dlp         — YouTube / Netzkino downloader (also installs the CLI)
#   requests       — Bunkr album/file page HTTP scraping and CDN download
#   beautifulsoup4 — Bunkr HTML parsing (lxml as fast parser, html.parser fallback)
#   lxml           — fast HTML parser backend for BeautifulSoup
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && yt-dlp --version \
    && python -c "import requests; print('requests', requests.__version__)" \
    && python -c "import bs4; print('beautifulsoup4', bs4.__version__)" \
    && python -c "import lxml; print('lxml', lxml.__version__)"

COPY tg-mega-bot.py .

# Persistent state (SQLite queue/dedup DB, Telethon session
# DATA_DIR/bot_session.session, kept files) and download scratch space.
# The script defaults to /data when that directory exists.
ENV DATA_DIR=/data \
    WORK_DIR=/work
RUN mkdir -p /data /work
VOLUME ["/data", "/work"]

# No EXPOSE: the bot only makes outbound connections (no web server).

# docker stop sends SIGTERM; the bot handles it and shuts down cleanly.
STOPSIGNAL SIGTERM

CMD ["python", "-u", "tg-mega-bot.py"]
