import json
import logging
import os

import requests


logger = logging.getLogger("WIS2_LLM")

DEFAULT_OLLAMA_HOST = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:7b-instruct"
DEFAULT_NUM_CTX = 32768

MAX_HISTORY_MESSAGES = 12
MAX_MESSAGE_CHARS = 2000

PRIMER = (
    "ABOUT THE SYSTEM\n"
    "This dashboard monitors WIS2, the World Meteorological Organization's system for "
    "exchanging weather data between countries. National centres publish data and status "
    "messages to WMO Global Brokers run by Global Information System Centres (GISCs), "
    "such as GISC-Toulouse or GISC-Beijing; each GISC mirrors data worldwide.\n"
    "Severity levels used by events:\n"
    "- CRITICAL: service fully down or a data stream completely stopped.\n"
    "- ERROR: a major function failing, e.g. downloads or broker connections broken.\n"
    "- WARNING: degraded but partially working.\n"
    "- INFO: routine notice; no action needed.\n"
    "ETS checks verify that published messages are complete and correctly formatted. "
    "KPI reports measure ongoing health such as download rates and broker connectivity.\n"
    "Common failure families: connection problems (broker unreachable, timeouts), "
    "validation failures (malformed or incomplete messages), cache/data-flow issues "
    "(\"ghosting\": data looks available but downloads fail), rate limiting, and "
    "scheduled maintenance windows.\n"
    "Incidents are grouped by similarity using a hash; repeated events sharing that hash "
    "mean the same underlying issue keeps recurring."
)

RULES = (
    "You are the built-in assistant of a weather-data monitoring dashboard. "
    "You help non-technical staff understand monitoring events.\n"
    "Rules:\n"
    "- Use plain everyday language. Explain any unavoidable technical term or acronym "
    "in one short sentence.\n"
    "- Answer ONLY using the context below. If the answer is not there, say you do not know "
    "and point to the responsible contacts listed in the context.\n"
    "- Never invent URLs, ticket numbers, people, or facts.\n"
    "- Write in English."
)


def is_configured():
    return bool(
        os.environ.get("OLLAMA_HOST")
        or os.environ.get("OLLAMA_MODEL")
    )


def _host():
    host = os.environ.get("OLLAMA_HOST", DEFAULT_OLLAMA_HOST).rstrip("/")
    if "://" not in host:
        host = "http://" + host
    return host


def ollama_host():
    return _host()


def _model():
    return os.environ.get("OLLAMA_MODEL", DEFAULT_MODEL)


def _num_ctx():
    try:
        return int(os.environ.get("OLLAMA_NUM_CTX", str(DEFAULT_NUM_CTX)))
    except ValueError:
        return DEFAULT_NUM_CTX


def clip_text(value, limit):
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"


def sanitize_history(history):
    cleaned = []
    for entry in history or []:
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        content = entry.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        content = content.strip()
        if not content:
            continue
        cleaned.append({"role": role, "content": content[:MAX_MESSAGE_CHARS]})
    return cleaned[-MAX_HISTORY_MESSAGES:]


def build_system_prompt(facts, knowledge=""):
    prompt = f"{RULES}\n\n=== {PRIMER}"
    if knowledge and knowledge.strip():
        prompt += f"\n\n=== KNOWLEDGE BASE EXCERPTS ===\n{knowledge.strip()}"
    prompt += f"\n\n=== EVENT CONTEXT ===\n{facts}"
    return prompt


def _timeout():
    try:
        return int(os.environ.get("OLLAMA_TIMEOUT", "300"))
    except ValueError:
        return 300


def chat(messages):
    """Send an OpenAI-style message list to the local Ollama server.

    Returns (reply_text, None) on success or (None, error_message).
    """
    payload = {
        "model": _model(),
        "messages": messages,
        "stream": False,
        "keep_alive": "15m",
        "options": {
            "num_predict": 320,
            "num_ctx": _num_ctx(),
        },
    }
    try:
        response = requests.post(
            f"{_host()}/api/chat",
            json=payload,
            timeout=_timeout(),
        )
    except requests.RequestException as exc:
        logger.warning("Ollama request failed: %s", exc)
        return None, str(exc)

    if response.status_code != 200:
        logger.warning(
            "Ollama returned HTTP %s: %s", response.status_code, response.text[:300]
        )
        return None, f"Ollama returned HTTP {response.status_code}"

    try:
        body = response.json()
    except ValueError:
        return None, "Invalid response from Ollama."

    reply = ""
    if isinstance(body, dict):
        message = body.get("message")
        if isinstance(message, dict):
            reply = message.get("content") or ""
    reply = reply.strip()
    if not reply:
        return None, "Empty response from Ollama."
    return reply, None


def raw_json_excerpt(event, limit=None):
    if event.raw_json is None:
        return ""
    try:
        text = json.dumps(event.raw_json, indent=2, default=str)
    except (TypeError, ValueError):
        text = str(event.raw_json)
    if limit is not None and len(text) > limit:
        text = text[:limit] + "\n[truncated]"
    return text
