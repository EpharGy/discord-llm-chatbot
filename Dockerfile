FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

RUN adduser --disabled-password --gecos "" --uid 10001 appuser

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
RUN python -m playwright install --with-deps chromium

COPY src/ ./src/
COPY cogs/ ./cogs/
COPY prompts/ ./prompts/
COPY personas/ ./personas/
COPY lore/ ./lore/
COPY config.example.yaml ./config.example.yaml

RUN chown -R appuser:appuser /app
RUN chown -R appuser:appuser /ms-playwright
USER appuser

CMD ["python", "-m", "src.bot_app"]
