FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

RUN adduser --disabled-password --gecos "" --uid 10001 appuser

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/
COPY cogs/ ./cogs/
COPY prompts/ ./prompts/
COPY personas/ ./personas/
COPY lore/ ./lore/
COPY config.example.yaml ./config.example.yaml

RUN chown -R appuser:appuser /app
USER appuser

CMD ["python", "-m", "src.bot_app"]
