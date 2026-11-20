FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt ./
COPY vendor/hunttech-bot-common /opt/hunttech-bot-common
RUN pip install --no-cache-dir -r requirements.txt "/opt/hunttech-bot-common[database,documents,onec]"
COPY bot.py vault_ai.py ./
COPY prompts.json ./prompts.json
COPY assets ./assets
COPY integrations ./integrations
COPY container-entrypoint.sh ./
RUN chmod 0555 /app/container-entrypoint.sh \
    && mkdir -p /data /.hermes/hunttech_bots \
    && chown -R 10001:10001 /app /data /.hermes

USER 10001:10001
ENTRYPOINT ["/app/container-entrypoint.sh"]
