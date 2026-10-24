"""Settings from config.ini in the repo root. Missing keys fall back to DEFAULTS.

    from hoi4_rp.config import settings
    settings().llm.model

config.ini is personal and git-ignored; if it's missing it is created from config.example.ini.
Set HOI4_RP_CONFIG to load a different file.
"""

from __future__ import annotations

import configparser
import os
import shutil
from dataclasses import dataclass
from functools import lru_cache

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLE_PATH = os.path.join(_ROOT, "config.example.ini")
CONFIG_PATH = os.environ.get("HOI4_RP_CONFIG") or os.path.join(_ROOT, "config.ini")

DEFAULTS = {
    "llm": {
        "base_url": "https://nano-gpt.com/api/v1",
        "api_key_env": "NANOGPT_API_KEY",
        "model": "xiaomi/mimo-v2.6-pro",
        "temperature": "0.9",
        "max_tokens": "1500",
        "timeout": "120",
        "retries": "2",
    },
    "letters": {
        "incoming": "yes",
        "quiet_days": "60",
        "cooldown_days": "30",
        "refusal_days": "180",
        "proposal_gap_days": "30",
    },
}


@dataclass(frozen=True)
class LLMSettings:
    base_url: str
    api_key_env: str
    model: str
    temperature: float
    max_tokens: int
    timeout: float
    retries: int


@dataclass(frozen=True)
class LetterSettings:
    incoming: bool        # countries write to the player on their own
    quiet_days: int       # game days without any incoming letter before someone writes unprompted
    cooldown_days: int    # game days before the same country starts another letter on its own
    refusal_days: int     # game days a declined proposal item can't be proposed again by that country
    proposal_gap_days: int  # game days between formal proposals from anyone


@dataclass(frozen=True)
class Settings:
    llm: LLMSettings
    letters: LetterSettings


@lru_cache(maxsize=None)
def settings() -> Settings:
    if not os.path.exists(CONFIG_PATH) and os.path.exists(EXAMPLE_PATH):
        shutil.copyfile(EXAMPLE_PATH, CONFIG_PATH)
        print(f"created {CONFIG_PATH} from config.example.ini")
    cp = configparser.ConfigParser()
    cp.read_dict(DEFAULTS)
    cp.read(CONFIG_PATH, encoding="utf-8")   # silently skipped if the file is missing
    s, lt = cp["llm"], cp["letters"]
    return Settings(
        llm=LLMSettings(
            base_url=s.get("base_url").rstrip("/"),
            api_key_env=s.get("api_key_env"),
            model=s.get("model"),
            temperature=s.getfloat("temperature"),
            max_tokens=s.getint("max_tokens"),
            timeout=s.getfloat("timeout"),
            retries=s.getint("retries"),
        ),
        letters=LetterSettings(
            incoming=lt.getboolean("incoming"),
            quiet_days=lt.getint("quiet_days"),
            cooldown_days=lt.getint("cooldown_days"),
            refusal_days=lt.getint("refusal_days"),
            proposal_gap_days=lt.getint("proposal_gap_days"),
        ),
    )
