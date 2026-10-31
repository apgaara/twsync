FROM python:3.10-slim

# Install system dependencies (ffmpeg for yt-dlp)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Create cache directory with write permissions for non-root user (Hugging Face UID 1000)
RUN mkdir -p /app/cache && chmod -R 777 /app/cache

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Ensure cache directory is writable by user 1000
RUN chmod -R 777 /app/cache

ENV PORT=7860
EXPOSE 7860

CMD ["python3", "server.py"]
