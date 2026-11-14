FROM python:3.12-slim

# megatools provides `megadl`, used for single-file links and as the
# bulk-download fallback when a folder cannot be listed via the API.
# ffmpeg repacks videos for Telegram before uploading (MP4 faststart).
RUN apt-get update \
    && apt-get install -y --no-install-recommends megatools ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# cryptography enables the fast OpenSSL AES backend and MAC verification.
# yt-dlp provides the downloader used by the /yt (YouTube) and /nk
# (Netzkino) commands.
RUN pip install --no-cache-dir telethon pyaes cryptography yt-dlp

WORKDIR /app
COPY tg-mega-bot.py .

CMD ["python", "-u", "tg-mega-bot.py"]
