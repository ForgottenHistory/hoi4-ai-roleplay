"""LLM client for any OpenAI-compatible chat API (NanoGPT by default), standard library only.

    from hoi4_rp import llm
    text = llm.chat([{"role": "user", "content": "Write a one-line headline."}])

Endpoint, model and limits come from config.ini [llm]. The API key is read from the environment
variable named by api_key_env (NANOGPT_API_KEY by default).

Every call is logged to data/logs/llm.jsonl: what it was for (kind: reply, letter, policy, promises, judge),
which country, tokens in and out (the API's own count, else an estimate), seconds and model. With
log_prompts = yes the full prompt and reply are kept too, in data/logs/prompts/. `python -m hoi4_rp usage`
sums the log up.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
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


LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "logs")
LOG_PATH = os.path.join(LOG_DIR, "llm.jsonl")
SUMMARY_EVERY = 20             # calls between the running totals printed in the console
_lock = threading.Lock()
_session: Dict[str, List[int]] = defaultdict(lambda: [0, 0, 0])     # kind -> [calls, tokens in, tokens out]


def _estimate(text: str) -> int:
    return len(text) // 4


def _log(kind: str, about: str, messages: List[Dict[str, str]], reply: str, usage: Dict, seconds: float,
         model_name: str, error: str = "") -> None:
    prompt_text = "".join(m.get("content") or "" for m in messages)
    tin = int(usage.get("prompt_tokens") or 0) or _estimate(prompt_text)
    tout = int(usage.get("completion_tokens") or 0) or _estimate(reply)
    now = datetime.datetime.now()
    row = {"time": now.isoformat(timespec="seconds"), "kind": kind or "other", "about": about,
           "in": tin, "out": tout, "estimated": not usage.get("prompt_tokens"), "seconds": round(seconds, 1),
           "model": model_name}
    if error:
        row["error"] = error[:200]
    with _lock:
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            with open(LOG_PATH, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            if settings().llm.log_prompts:
                folder = os.path.join(LOG_DIR, "prompts")
                os.makedirs(folder, exist_ok=True)
                name = f"{now:%Y%m%d-%H%M%S}-{kind or 'other'}-{about or 'x'}.txt"
                with open(os.path.join(folder, re.sub(r"[^\w.-]", "_", name)), "w", encoding="utf-8") as f:
                    for m in messages:
                        f.write(f"===== {m.get('role')}\n{m.get('content')}\n\n")
                    f.write(f"===== REPLY ({tin} in, {tout} out)\n{reply or error}\n")
        except OSError as e:
            print(f"  could not write the LLM log: {e}")
        s = _session[row["kind"]]
        s[0], s[1], s[2] = s[0] + 1, s[1] + tin, s[2] + tout
        calls = sum(v[0] for v in _session.values())
    if calls % SUMMARY_EVERY == 0:
        print(f"  [LLM this session] {session_text()}")


def session_text() -> str:
    """'64 calls, 1.9M tokens in, 31k out (policy 48: 1.5M in; reply 9: 0.2M in; ...)'"""
    with _lock:
        items = sorted(_session.items(), key=lambda kv: -kv[1][1])
        calls, tin, tout = (sum(v[i] for v in _session.values()) for i in range(3))
    parts = "; ".join(f"{k} {v[0]}: {_short(v[1])} in" for k, v in items)
    return f"{calls} calls, {_short(tin)} tokens in, {_short(tout)} out ({parts})"


def _short(n: int) -> str:
    return f"{n / 1e6:.2f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)


def usage_report(days: float = 1.0) -> str:
    """The log of the last `days` days, summed up by kind of call and by country."""
    if not os.path.exists(LOG_PATH):
        return "no LLM calls logged yet"
    since = (datetime.datetime.now() - datetime.timedelta(days=days)).isoformat(timespec="seconds")
    rows = []
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("time", "") >= since:
                rows.append(r)
    if not rows:
        return f"no LLM calls in the last {days:g} day(s)"
    total_in, total_out = sum(r["in"] for r in rows), sum(r["out"] for r in rows)
    lines = [f"Last {days:g} day(s): {len(rows)} calls, {_short(total_in)} tokens in, {_short(total_out)} out, "
             f"{sum(r['seconds'] for r in rows) / 60:.0f} minutes of waiting"
             + (f", {sum(1 for r in rows if r.get('error'))} failed" if any(r.get("error") for r in rows) else "")]
    for title, key in (("By kind", "kind"), ("By country (top 15)", "about")):
        groups: Dict[str, List[int]] = defaultdict(lambda: [0, 0, 0])
        for r in rows:
            g = groups[r.get(key) or "-"]
            g[0], g[1], g[2] = g[0] + 1, g[1] + r["in"], g[2] + r["out"]
        lines.append(f"\n{title}:")
        for k, (n, tin, tout) in sorted(groups.items(), key=lambda kv: -kv[1][1])[:15]:
            lines.append(f"  {k:<12} {n:>5} calls  {_short(tin):>7} in ({tin * 100 // max(total_in, 1):>2}%)  "
                         f"{_short(tout):>6} out  avg {_short(tin // n)} in per call")
    return "\n".join(lines)


def _strip_thinking(text: str) -> str:
    # Some models inline their reasoning as <think>...</think> before the answer.
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def chat(messages: List[Dict[str, str]], model_name: Optional[str] = None,
         temperature: Optional[float] = None, max_tokens: Optional[int] = None,
         kind: str = "", about: str = "") -> str:
    """Send a chat and return the reply text. Arguments left as None use config.ini.
    Retries server errors (5xx) and timeouts. `kind` and `about` (a country tag) label the call in the log."""
    started = time.monotonic()
    try:
        reply, usage = _chat(messages, model_name, temperature, max_tokens)
    except LLMError as e:
        _log(kind, about, messages, "", {}, time.monotonic() - started, model_name or model(), str(e))
        raise
    _log(kind, about, messages, reply, usage, time.monotonic() - started, model_name or model())
    return reply


def _chat(messages: List[Dict[str, str]], model_name: Optional[str], temperature: Optional[float],
          max_tokens: Optional[int]):
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
    return _strip_thinking(content), (data.get("usage") or {}) if isinstance(data, dict) else {}
