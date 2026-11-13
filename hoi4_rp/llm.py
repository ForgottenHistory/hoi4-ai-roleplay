"""LLM client, standard library only: any OpenAI-compatible chat API (NanoGPT by default, OpenRouter, OpenAI,
Gemini, DeepSeek, Mistral, Groq, xAI, Together, LM Studio...), Anthropic's Messages API and Ollama's /api/chat.

    from hoi4_rp import llm
    text = llm.chat([{"role": "user", "content": "Write a one-line headline."}])

Provider, endpoint, model and limits come from config.ini [llm] (config.PROVIDERS has the presets). The API key is
read from the environment variable named by api_key_env (the provider's own, e.g. NANOGPT_API_KEY, by default).
Parameters a model refuses (max_tokens on OpenAI's reasoning models, temperature on some) are changed or left out
and the call sent again; that is remembered for the session.

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
    cfg = settings().llm
    key = os.environ.get(cfg.api_key_env, "").strip()
    if not key and cfg.key_required:
        raise LLMError(f"{cfg.api_key_env} is not set (provider {cfg.provider}); set it with "
                       f'setx {cfg.api_key_env} "your-key" and open a new terminal')
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
    # Some models inline their reasoning as <think>...</think> before the answer; one cut off mid-thought
    # has no closing tag and no answer.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S)
    return re.sub(r"<think>.*", "", text, flags=re.S).strip()


def chat(messages: List[Dict[str, str]], model_name: Optional[str] = None,
         temperature: Optional[float] = None, max_tokens: Optional[int] = None,
         kind: str = "", about: str = "") -> str:
    """Send a chat and return the reply text. Arguments left as None use config.ini.
    Retries server errors (5xx) and timeouts. `kind` and `about` (a country tag) label the call in the log.
    A reply left empty because the model's reasoning used up the token budget is asked again once, with
    twice the budget."""
    budget = max_tokens or settings().llm.max_tokens
    for attempt in range(2):
        started = time.monotonic()
        try:
            reply, usage, finish = _chat(messages, model_name, temperature, budget)
        except LLMError as e:
            _log(kind, about, messages, "", {}, time.monotonic() - started, model_name or model(), str(e))
            raise
        out_of_budget = not reply and finish == "length"
        _log(kind, about, messages, reply, usage, time.monotonic() - started, model_name or model(),
             "empty: reasoning used the token budget" if out_of_budget else "")
        if not out_of_budget or attempt:
            return reply
        print(f"  [LLM] {kind or 'call'} {about}: reasoning used all {budget} tokens, asking again with {budget * 2}")
        budget *= 2
    return ""


# Request changes a model needed, kept for the session: model -> {"max_completion_tokens", "no_temperature"}
_quirks: Dict[str, set] = defaultdict(set)


def _quirk(model_name: str, status: int, detail: str) -> bool:
    """Learn from a 400 which parameter the model refuses; True when there is something new to try."""
    if status != 400:
        return False
    text, q = detail.lower(), _quirks[model_name]
    if "max_tokens" in text and "max_completion_tokens" in text and "max_completion_tokens" not in q:
        q.add("max_completion_tokens")
        return True
    if "temperature" in text and any(w in text for w in ("unsupported", "not support", "only the default",
                                                         "deprecated", "does not support")) \
            and "no_temperature" not in q:
        q.add("no_temperature")
        return True
    return False


def _openai_request(cfg, key: str, model_name: str, messages, temperature: float, max_tokens: int):
    q = _quirks[model_name]
    body = {"model": model_name, "messages": messages}
    if "no_temperature" not in q:
        body["temperature"] = temperature
    if cfg.provider == "openai" or "max_completion_tokens" in q:
        body["max_completion_tokens"] = max_tokens      # OpenAI's reasoning models refuse max_tokens
    else:
        body["max_tokens"] = max_tokens
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return f"{cfg.base_url}/chat/completions", body, headers


def _openai_reply(data):
    choice = data["choices"][0]
    content = choice["message"].get("content") or ""
    if isinstance(content, list):                       # some servers send content parts
        content = "".join(p.get("text") or "" for p in content if isinstance(p, dict))
    return content, (data.get("usage") or {}), choice.get("finish_reason") or ""


def _anthropic_request(cfg, key: str, model_name: str, messages, temperature: float, max_tokens: int):
    system = "\n\n".join(m.get("content") or "" for m in messages if m.get("role") == "system")
    turns: List[Dict[str, str]] = []
    for m in messages:
        if m.get("role") == "system":
            continue
        role = "assistant" if m.get("role") == "assistant" else "user"
        if turns and turns[-1]["role"] == role:          # user and assistant take turns
            turns[-1]["content"] += "\n\n" + (m.get("content") or "")
        else:
            turns.append({"role": role, "content": m.get("content") or ""})
    if not turns or turns[0]["role"] != "user":
        turns.insert(0, {"role": "user", "content": "(begin)"})
    body = {"model": model_name, "messages": turns, "max_tokens": max_tokens}
    if system:
        body["system"] = system
    if "no_temperature" not in _quirks[model_name]:
        body["temperature"] = min(temperature, 1.0)     # Anthropic takes 0-1
    headers = {"Content-Type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"}
    return f"{cfg.base_url}/messages", body, headers


def _anthropic_reply(data):
    content = "".join(b.get("text") or "" for b in data["content"] if b.get("type") == "text")
    u = data.get("usage") or {}
    usage = {"prompt_tokens": (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0)
             + (u.get("cache_creation_input_tokens") or 0), "completion_tokens": u.get("output_tokens") or 0}
    finish = "length" if data.get("stop_reason") == "max_tokens" else data.get("stop_reason") or ""
    return content, usage, finish


_warned_context: set = set()


def _ollama_request(cfg, key: str, model_name: str, messages, temperature: float, max_tokens: int):
    need = sum(len(m.get("content") or "") for m in messages) // 4 + max_tokens
    if need > cfg.context and model_name not in _warned_context:
        _warned_context.add(model_name)
        print(f"  [LLM] a prompt of ~{need} tokens (with the reply) is over [llm] context = {cfg.context}; Ollama cuts "
              f"the start of it. Raise context if your memory allows.")
    body = {"model": model_name, "messages": messages, "stream": False, "keep_alive": "30m",
            "options": {"num_ctx": cfg.context, "num_predict": max_tokens}}
    if "no_temperature" not in _quirks[model_name]:
        body["options"]["temperature"] = temperature
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return f"{cfg.base_url}/api/chat", body, headers


def _ollama_reply(data):
    usage = {"prompt_tokens": data.get("prompt_eval_count") or 0, "completion_tokens": data.get("eval_count") or 0}
    return data["message"].get("content") or "", usage, data.get("done_reason") or ""


def _wait(attempt: int, e: Optional[urllib.error.HTTPError] = None) -> float:
    """Seconds before the next try: the server's Retry-After when it gives one (rate limits), else 2, 4, 6..."""
    try:
        return min(60.0, float(e.headers.get("Retry-After")))
    except (AttributeError, TypeError, ValueError):
        return 2.0 * (attempt + 1)


def _chat(messages: List[Dict[str, str]], model_name: Optional[str], temperature: Optional[float],
          max_tokens: Optional[int]):
    cfg = settings().llm
    name = model_name or cfg.model
    build, read = {"anthropic": (_anthropic_request, _anthropic_reply),
                   "ollama": (_ollama_request, _ollama_reply)}.get(cfg.provider, (_openai_request, _openai_reply))
    key = _api_key()
    temp = cfg.temperature if temperature is None else temperature
    attempt, adjusted = 0, 0
    while True:
        url, body, headers = build(cfg, key, name, messages, temp, max_tokens or cfg.max_tokens)
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:500]
            if adjusted < 3 and _quirk(name, e.code, detail):
                adjusted += 1                            # sent again without what the model refused
                continue
            if (e.code >= 500 or e.code == 429) and attempt < cfg.retries:
                time.sleep(_wait(attempt, e))
                attempt += 1
                continue
            raise LLMError(f"LLM HTTP {e.code} ({cfg.provider}): {detail}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt < cfg.retries:
                time.sleep(_wait(attempt))
                attempt += 1
                continue
            raise LLMError(f"LLM request to {url} failed: {e}") from None
        except ValueError as e:
            raise LLMError(f"LLM reply was not JSON: {e}") from None
    try:
        content, usage, finish = read(data)
    except (KeyError, IndexError, TypeError, AttributeError):
        raise LLMError(f"unexpected LLM reply: {str(data)[:500]}") from None
    return _strip_thinking(content), usage, finish
