FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# ffmpeg: required by yt-dlp (merging) and by NTgCalls (decoding media for the voice chat)
# gcc/libc6-dev: fallback build of TgCrypto if no wheel matches
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*

# Deno: JavaScript runtime used by yt-dlp for current YouTube extraction. Pin a tag for reproducible builds.
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY bot.py generate_session.py ./

# Default download dir inside the image; on Render set DOWNLOAD_DIR=/var/data/downloads (persistent disk).
RUN mkdir -p /app/downloads

CMD ["python", "-u", "bot.py"]
