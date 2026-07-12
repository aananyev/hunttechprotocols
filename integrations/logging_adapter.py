"""
Logging adapter for hunttech-bot-common.

Replaces logging.basicConfig() with setup_logging().
Adds secrets masking filter to protect API keys in logs.
"""
import logging
from hunttech_bot_common.logging import setup_logging, get_logger

# Use common logging setup (keeps same format and level)
setup_logging(
    level="INFO",
    format_str="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    add_secrets_filter=True,
)

# Create the same logger name as before for compatibility
logger = get_logger("bot")
