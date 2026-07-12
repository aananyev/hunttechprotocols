"""
Configuration adapter for hunttech-bot-common.

Replaces direct load_dotenv() + os.getenv() with AppSettings.
Maintains backward compatibility: existing os.getenv() calls still work.
"""
import os
from pathlib import Path
from hunttech_bot_common.config import AppSettings
from dotenv import load_dotenv

# Load .env once (compatible with existing os.getenv calls)
load_dotenv()

# Create typed settings (for new code)
settings = AppSettings.from_env()
