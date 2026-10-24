"""LLM client for any OpenAI-compatible chat API (NanoGPT by default), standard library only.

    from hoi4_rp import llm
    text = llm.chat([{"role": "user", "content": "Write a one-line headline."}])

Endpoint, model and limits come from config.ini [llm]. The API key is read from the environment
variable named by api_key_env (NANOGPT_API_KEY by default).
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional

from .config import settings


class LLMError(RuntimeError):
    pass


def _api_key() -> str:
    env = settings().llm.api_key_env
    key = os.environ.get(env, "").strip()
    if not key:
        raise LLMError(f"{env} is not set")
    return key


def model() -> str:
    return settings().llm.model


def _strip_thinking(text: str) -> str:
    # Some models inline their reasoning as <think>...</think> before the answer.
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def chat(messages: List[Dict[str, str]], model_name: Optional[str] = None,
         temperature: Optional[float] = None, max_tokens: Optional[int] = None) -> str:
    """Send a chat and return the reply text. Arguments left as None use config.ini.
    Retries server errors (5xx) and timeouts."""
    cfg = settings().llm
    payload = json.dumps({
        "model": model_name or cfg.model,
        "messages": messages,
        "temperature": cfg.temperature if temperature is None else temperature,
        "max_tokens": max_tokens or cfg.max_tokens,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{cfg.base_url}/chat/completions", data=payload, method="POST",
        headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
    )
    for attempt in range(cfg.retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if e.code >= 500 and attempt < cfg.retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise LLMError(f"LLM HTTP {e.code}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < cfg.retries:
                time.sleep(2 * (attempt + 1))
                continue
            raise LLMError(f"LLM request failed: {e}") from None
    try:
        content = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"unexpected LLM reply: {str(data)[:500]}") from None
    return _strip_thinking(content)
