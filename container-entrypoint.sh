#!/bin/sh
set -eu

read_secret() {
    variable="$1"
    secret_file="$2"
    eval "current=\${$variable:-}"
    if [ -z "$current" ] && [ -r "$secret_file" ]; then
        export "$variable=$(cat "$secret_file")"
    fi
}

read_secret TG_TOKEN /run/secrets/tg_token
if [ -z "${TG_TOKEN:-}" ]; then
    echo "TG_TOKEN is required" >&2
    exit 1
fi

if [ ! -f /data/prompts.json ]; then
    cp /app/prompts.json /data/prompts.json
fi

exec python /app/bot.py
