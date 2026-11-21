FROM python:3.12-slim

# megatools provides megadl for MEGA single-file downloads and bulk fallback.
# ffmpeg provides video conversion, 720p scaling, and MP4 faststart.
RUN apt-get update \
    && apt-get install -y --no-install-recommends megatools ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies, including Pillow for image metadata removal.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tg-mega-bot.py .

CMD ["python", "-u", "tg-mega-bot.py"]