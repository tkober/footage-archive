FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

RUN mkdir -p /footage-archive
WORKDIR /footage-archive

RUN apt-get update && apt-get install -y \
    build-essential \
    ffmpeg \
    libimage-exiftool-perl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies before copying app code for better layer caching
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY ./ ./

RUN useradd -m appuser
RUN chown -R appuser:appuser /footage-archive
USER appuser

ENV TZ="Europe/Berlin"
ENV PATH="/footage-archive/.venv/bin:$PATH"

CMD ["python", "app.py"]
