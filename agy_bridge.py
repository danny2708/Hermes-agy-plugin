"""OpenAI-compatible local HTTP bridge for Google Antigravity (agy).

Translates incoming OpenAI chat completions requests into `agy` CLI invocations,
enabling Hermes Agent to route model inference through local Google Antigravity.

Features:
- 100% Dynamic Model Discovery from `agy` CLI without hardcoding.
- Auto-maps slugs (e.g. gemini-3.8-flash, gemini-3.1-pro, claude-sonnet-4.6) to exact CLI targets.
- Real-time Chain-of-Thought (CoT) extraction from `<think>` tags to `reasoning_content`.
- Seamless OpenAI `<tool_call>` generation & parameter streaming.
- Comprehensive logging of tool calls dispatched and tool execution results received.
- Safe update watchdog pausing the bridge during Hermes updates to prevent venv locks.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from urllib.parse import unquote, urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from typing import Any, Dict, List, Optional, Tuple

_AUTH_TOKEN_FILE = os.path.join(tempfile.gettempdir(), "agy_bridge_token.secret")


def get_or_create_auth_token() -> str:
    """Retrieve or generate a secure local ephemeral auth token."""
    env_token = os.environ.get("AGY_BRIDGE_TOKEN", "").strip()
    if env_token:
        return env_token

    try:
        if os.path.isfile(_AUTH_TOKEN_FILE):
            with open(_AUTH_TOKEN_FILE, "r", encoding="utf-8") as f:
                token = f.read().strip()
                if len(token) >= 16:
                    return token
    except Exception:
        pass

    import secrets
    token = f"agy-{secrets.token_hex(16)}"
    try:
        with open(_AUTH_TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(token)
        if sys.platform != "win32":
            os.chmod(_AUTH_TOKEN_FILE, 0o600)
    except Exception:
        pass
    return token


def get_hermes_home_dir() -> str:
    """Return the active HERMES_HOME directory dynamically."""
    if os.environ.get("HERMES_HOME"):
        return os.path.normpath(os.environ["HERMES_HOME"])
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        return os.path.normpath(os.path.expanduser("~/.hermes"))


hermes_home = get_hermes_home_dir()
log_dir = os.path.join(hermes_home, "logs")
os.makedirs(log_dir, exist_ok=True)
_log_file = os.path.join(log_dir, "agy_bridge.log")

_handlers = [
    RotatingFileHandler(_log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
]
if sys.stderr is not None:
    _handlers.append(logging.StreamHandler(sys.stderr))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=_handlers,
)
logger = logging.getLogger("agy_bridge")

# Dedicated tool logger with 5MB rotation
_tool_log_file = os.path.join(log_dir, "agy_tools.log")
tool_logger = logging.getLogger("agy_bridge.tools")
_tool_handler = RotatingFileHandler(
    _tool_log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
)
_tool_handler.setFormatter(
    logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
tool_logger.addHandler(_tool_handler)
tool_logger.setLevel(logging.INFO)
tool_logger.propagate = True

# Privacy and redaction configuration
AGY_LOG_TOOL_ARGS = os.getenv("AGY_LOG_TOOL_ARGS", "true").lower() in ("true", "1", "yes")
AGY_LOG_TOOL_RESULTS = os.getenv("AGY_LOG_TOOL_RESULTS", "true").lower() in ("true", "1", "yes")
AGY_LOG_MAX_PAYLOAD = int(os.getenv("AGY_LOG_MAX_PAYLOAD", "4000"))


def redact_sensitive_content(text: str) -> str:
    """Mask credentials, tokens, and private keys in logs."""
    if not text:
        return text
    # Bearer tokens
    text = re.sub(r"(?i)\b(bearer\s+)[a-zA-Z0-9_\-\.]{10,}", r"\1[REDACTED_TOKEN]", text)
    # Common API keys (OpenAI, Google, generic sk-)
    text = re.sub(r"\b(sk-[a-zA-Z0-9_\-]{20,})", r"[REDACTED_API_KEY]", text)
    text = re.sub(r"\b(AIza[0-9A-Za-z-_]{30,})", r"[REDACTED_GOOGLE_KEY]", text)
    # Password/secret key-value fields in JSON or text
    text = re.sub(
        r"""(?i)(["']?(?:password|passwd|secret|api[_-]?key|access[_-]?token|private[_-]?key)["']?\s*[:=]\s*["'])([^"'\r\n]{4,})(["'])""",
        r"\1[REDACTED]\3",
        text,
    )
    # PEM Private Keys
    text = re.sub(
        r"-----BEGIN [A-Z ]+ PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+ PRIVATE KEY-----",
        r"[REDACTED_PRIVATE_KEY]",
        text,
    )
    return text


# Thread-safe tracker for tool calls and result logging
_active_tool_calls_lock = threading.Lock()
_active_tool_calls: Dict[str, Dict[str, Any]] = {}
_logged_result_ids: set = set()


def _format_content_payload(val: Any, max_len: Optional[int] = None) -> str:
    """Format payload (JSON or text) cleanly, with indentation, redaction, and safe truncation."""
    if max_len is None:
        max_len = AGY_LOG_MAX_PAYLOAD

    if val is None:
        return "<none>"
    if isinstance(val, (dict, list)):
        try:
            text = json.dumps(val, indent=2, ensure_ascii=False)
        except Exception:
            text = str(val)
    elif isinstance(val, str):
        val_s = val.strip()
        if (val_s.startswith("{") and val_s.endswith("}")) or (val_s.startswith("[") and val_s.endswith("]")):
            try:
                parsed = json.loads(val_s)
                text = json.dumps(parsed, indent=2, ensure_ascii=False)
            except Exception:
                text = val
        else:
            text = val
    else:
        text = str(val)

    text = redact_sensitive_content(text)

    if len(text) > max_len:
        omitted = len(text) - max_len
        text = text[:max_len] + f"\n... [truncated {omitted} characters; total length: {len(text)} chars]"
    return text


def log_tool_call_dispatched(name: str, call_id: str, arguments: str) -> None:
    """Log outgoing tool call emitted by the model to Hermes."""
    global _active_tool_calls
    with _active_tool_calls_lock:
        _active_tool_calls[call_id] = {
            "name": name,
            "dispatched_at": time.time(),
            "arguments": arguments,
        }
        if len(_active_tool_calls) > 500:
            oldest_key = next(iter(_active_tool_calls))
            _active_tool_calls.pop(oldest_key, None)

    if not AGY_LOG_TOOL_ARGS:
        formatted_args = "<arguments logging disabled via AGY_LOG_TOOL_ARGS=false>"
    else:
        formatted_args = _format_content_payload(arguments)

    sep = "=" * 80
    msg = (
        f"\n{sep}\n"
        f"[TOOL CALL DISPATCHED]\n"
        f"  Tool Name : {name or 'unknown'}\n"
        f"  Call ID   : {call_id}\n"
        f"  Payload / Arguments:\n"
        f"{formatted_args}\n"
        f"{sep}"
    )
    tool_logger.info(msg)


def log_incoming_tool_results(messages: List[Dict[str, Any]]) -> None:
    """Scan incoming messages for tool results executed by Hermes and log them."""
    global _active_tool_calls, _logged_result_ids
    if not messages:
        return

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "tool":
            continue

        call_id = str(msg.get("tool_call_id") or "")
        name = str(msg.get("name") or "")
        content = msg.get("content", "")

        content_preview = str(content)[:200]
        fingerprint = f"{call_id}:{name}:{hash(content_preview)}"

        with _active_tool_calls_lock:
            if fingerprint in _logged_result_ids:
                continue
            _logged_result_ids.add(fingerprint)
            if len(_logged_result_ids) > 2000:
                _logged_result_ids.clear()

            call_info = _active_tool_calls.pop(call_id, None)

        if not name and call_info:
            name = call_info.get("name", "")

        duration_str = ""
        if call_info and "dispatched_at" in call_info:
            dur = time.time() - call_info["dispatched_at"]
            duration_str = f" (execution time: {dur:.2f}s)"

        content_str = str(content) if content is not None else ""
        content_lower = content_str[:150].lower()
        if any(err_kw in content_lower for err_kw in ("error:", "traceback", "failed:", "exception:")):
            status_str = "FAILED / ERROR"
        else:
            status_str = "SUCCESS / OUTPUT"

        if not AGY_LOG_TOOL_RESULTS:
            formatted_res = "<result logging disabled via AGY_LOG_TOOL_RESULTS=false>"
        else:
            formatted_res = _format_content_payload(content)

        sep = "=" * 80
        msg_log = (
            f"\n{sep}\n"
            f"[TOOL RESULT RECEIVED]\n"
            f"  Tool Name : {name or 'unknown'}\n"
            f"  Call ID   : {call_id}{duration_str}\n"
            f"  Status    : {status_str}\n"
            f"  Size      : {len(content_str)} chars\n"
            f"  Result Content:\n"
            f"{formatted_res}\n"
            f"{sep}"
        )
        tool_logger.info(msg_log)


HOST = "127.0.0.1"
PORT = 8765

MAX_AGY_PROMPT_CHARS = int(os.getenv("AGY_BRIDGE_MAX_PROMPT_CHARS", "350000"))
AGY_SYSTEM_BUDGET_CHARS = int(os.getenv("AGY_BRIDGE_SYSTEM_BUDGET_CHARS", "80000"))
AGY_CURRENT_REQUEST_BUDGET_CHARS = int(
    os.getenv("AGY_BRIDGE_CURRENT_REQUEST_BUDGET_CHARS", "40000")
)
AGY_CURRENT_WORK_BUDGET_CHARS = int(
    os.getenv("AGY_BRIDGE_CURRENT_WORK_BUDGET_CHARS", "180000")
)

BRIDGE_SCRATCH_DIR = os.path.join(tempfile.gettempdir(), "agy_bridge_work")
os.makedirs(BRIDGE_SCRATCH_DIR, exist_ok=True)

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

# Ensure scratch directory has strict permissions blocking native tools
dot_gemini_scratch = os.path.join(BRIDGE_SCRATCH_DIR, ".gemini")
os.makedirs(dot_gemini_scratch, exist_ok=True)
try:
    with open(os.path.join(dot_gemini_scratch, "settings.json"), "w", encoding="utf-8") as f:
        json.dump({
            "allowNonWorkspaceAccess": False,
            "permissions": {
                "deny": [
                    "command(*)",
                    "read_file(*)",
                    "write_file(*)",
                    "read_url(*)",
                    "execute_url(*)",
                    "mcp(*)"
                ]
            }
        }, f, indent=2)
except Exception:
    pass


def find_agy_executable() -> str:
    """Locate the agy CLI binary dynamically across operating systems."""
    # 1. Check PATH
    found = shutil.which("agy")
    if found:
        return found

    # 2. Check Windows common paths
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            cand = os.path.join(local_app_data, "agy", "bin", "agy.exe")
            if os.path.isfile(cand):
                return cand
        app_data = os.environ.get("APPDATA", "")
        if app_data:
            cand = os.path.join(app_data, "agy", "bin", "agy.exe")
            if os.path.isfile(cand):
                return cand

    # 3. Check Unix common paths
    home = os.path.expanduser("~")
    for cand in [
        os.path.join(home, ".local", "bin", "agy"),
        os.path.join(home, "bin", "agy"),
        "/usr/local/bin/agy",
        "/usr/bin/agy",
    ]:
        if os.path.isfile(cand):
            return cand

    return "agy"


AGY_PATH = find_agy_executable()

# ── Dynamic Model Discovery & Caching ────────────────────────────────────────

_models_lock = threading.Lock()
try:
    _AGY_MAX_CONCURRENT = max(1, int(os.getenv("AGY_MAX_CONCURRENT", "2")))
except ValueError:
    _AGY_MAX_CONCURRENT = 2

_agy_process_slots = threading.BoundedSemaphore(_AGY_MAX_CONCURRENT)
_cached_catalog: List[Dict[str, Any]] = []
_cached_slug_map: Dict[str, str] = {}
_cached_base_efforts: Dict[str, Dict[str, str]] = {}
_cached_base_defaults: Dict[str, str] = {}
_last_fetch_time: float = 0.0
_CACHE_TTL: float = 60.0  # Refresh every 60 seconds from agy CLI


def fetch_raw_agy_model_entries() -> List[Tuple[str, str]]:
    """Query agy CLI using the official 'agy models' command to extract available models."""
    if not os.path.isfile(AGY_PATH) and shutil.which(AGY_PATH) is None:
        return []

    cmd = [AGY_PATH, "models"]

    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10.0,
            creationflags=CREATE_NO_WINDOW,
        )
        if res.returncode == 0 and res.stdout:
            raw_out = res.stdout.strip()
            # Forward-compatible: check if CLI returns structured JSON
            if raw_out.startswith(("{", "[")):
                try:
                    parsed = json.loads(raw_out)
                    items = parsed if isinstance(parsed, list) else parsed.get("models", parsed.get("data", []))
                    json_entries: List[Tuple[str, str]] = []
                    for item in items:
                        if isinstance(item, dict):
                            slug = item.get("id") or item.get("name") or item.get("model") or ""
                            dname = item.get("display_name") or item.get("description") or slug
                            if slug:
                                json_entries.append((slug, dname))
                        elif isinstance(item, str) and item:
                            json_entries.append((item, item))
                    if json_entries:
                        return json_entries
                except Exception:
                    pass

            lines = raw_out.splitlines()
            entries: List[Tuple[str, str]] = []
            for line in lines:
                line_s = line.strip()
                if not line_s or line_s.lower().startswith("fetching"):
                    continue
                parts = re.split(r"\t+|\s{2,}", line_s)
                if len(parts) >= 2:
                    slug = parts[0].strip()
                    dname = parts[1].strip()
                    entries.append((slug, dname))
                elif len(parts) == 1 and parts[0]:
                    val = parts[0].strip()
                    entries.append((val, val))
            if entries:
                return entries
    except Exception as exc:
        logger.warning("Failed to dynamically fetch models via 'agy models': %s", exc)

    return []


def fetch_raw_agy_model_names() -> List[str]:
    """Compatibility wrapper returning model display strings."""
    entries = fetch_raw_agy_model_entries()
    return [e[1] for e in entries] if entries else []


def build_dynamic_model_index(
    raw_models: List[Any],
) -> Tuple[
    List[Dict[str, Any]],
    Dict[str, str],
    Dict[str, Dict[str, str]],
    Dict[str, str],
]:
    """Build OpenAI /v1/models catalog (clean base models) and effort resolution mappings."""
    slug_map: Dict[str, str] = {}
    catalog: List[Dict[str, Any]] = []
    base_efforts: Dict[str, Dict[str, str]] = {}
    base_defaults: Dict[str, str] = {}
    seen_base: set = set()

    for item in raw_models:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            exact_slug = str(item[0]).strip()
            display_str = str(item[1]).strip()
        else:
            exact_slug = ""
            display_str = str(item).strip()

        if not display_str:
            continue

        match = re.match(r"^(.*?)\s*\((High|Thinking|Medium|Low)\)$", display_str, re.IGNORECASE)
        if match:
            base_name = match.group(1).strip()
            variant = match.group(2).lower()
        else:
            base_name = display_str
            variant = "default"

        s_base = re.sub(r"[\s_]+", "-", base_name).lower()
        cli_target = exact_slug if exact_slug else display_str

        if s_base not in base_efforts:
            base_efforts[s_base] = {}
        base_efforts[s_base][variant] = cli_target

        if exact_slug:
            slug_map[exact_slug.lower()] = cli_target
        slug_map[display_str.lower()] = cli_target
        s_full = re.sub(r"[\s_]+", "-", re.sub(r"[()]", "", display_str)).lower()
        slug_map[s_full] = cli_target

        if s_base not in base_defaults or variant in ("high", "thinking"):
            base_defaults[s_base] = cli_target
            slug_map[s_base] = cli_target

        if s_base not in seen_base:
            seen_base.add(s_base)
            catalog.append({
                "id": s_base,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "google-antigravity",
                "display_name": base_name,
            })

    if not catalog:
        fallback_entries = [
            ("gemini-3.8-flash-high", "Gemini 3.8 Flash (High)"),
            ("gemini-3.7-flash-high", "Gemini 3.7 Flash (High)"),
            ("gemini-3.1-pro-high", "Gemini 3.1 Pro (High)"),
            ("gemini-3.6-flash-high", "Gemini 3.6 Flash (High)"),
            ("claude-sonnet-4-6", "Claude Sonnet 4.6 (Thinking)"),
            ("claude-opus-4-6-thinking", "Claude Opus 4.6 (Thinking)"),
            ("gpt-oss-120b-medium", "GPT-OSS 120B (Medium)"),
        ]
        return build_dynamic_model_index(fallback_entries)

    return catalog, slug_map, base_efforts, base_defaults


def refresh_dynamic_models() -> None:
    """Fetch available models from agy CLI in background and update cache."""
    global _cached_catalog, _cached_slug_map, _cached_base_efforts, _cached_base_defaults, _last_fetch_time
    entries = fetch_raw_agy_model_entries()
    if entries:
        cat, smap, beff, bdef = build_dynamic_model_index(entries)
        with _models_lock:
            _cached_catalog = cat
            _cached_slug_map = smap
            _cached_base_efforts = beff
            _cached_base_defaults = bdef
            _last_fetch_time = time.time()
        logger.info("Dynamically refreshed %d base models from Antigravity CLI", len(cat))


def get_models_catalog() -> Tuple[List[Dict[str, Any]], Dict[str, str], Dict[str, Dict[str, str]], Dict[str, str]]:
    """Return the cached dynamic model catalog, slug map, base efforts, and base defaults."""
    global _cached_catalog, _cached_slug_map, _cached_base_efforts, _cached_base_defaults, _last_fetch_time

    now = time.time()
    with _models_lock:
        if not _cached_catalog:
            cat, smap, beff, bdef = build_dynamic_model_index([
                "Gemini 3.8 Flash (High)",
                "Gemini 3.7 Flash (High)",
                "Gemini 3.1 Pro (High)",
                "Gemini 3.6 Flash (High)",
                "Claude Sonnet 4.6 (Thinking)",
                "Claude Opus 4.6 (Thinking)",
                "GPT-OSS 120B (Medium)",
            ])
            _cached_catalog = cat
            _cached_slug_map = smap
            _cached_base_efforts = beff
            _cached_base_defaults = bdef

        if now - _last_fetch_time >= _CACHE_TTL:
            _last_fetch_time = now
            threading.Thread(target=refresh_dynamic_models, daemon=True).start()

        return _cached_catalog, _cached_slug_map, _cached_base_efforts, _cached_base_defaults


def resolve_model_arg(model_name: Optional[str], effort: Optional[str] = None) -> Optional[str]:
    """Resolve an incoming model identifier and reasoning effort to an agy target name."""
    if not model_name:
        return None

    clean = model_name.strip().lower()
    _, slug_map, base_efforts, base_defaults = get_models_catalog()

    if effort and clean in base_efforts:
        eff = str(effort).strip().lower()
        norm_effort = "medium"
        if eff in ("low", "minimal", "lo"):
            norm_effort = "low"
        elif eff in ("high", "xhigh", "max", "ultra", "hi", "thinking"):
            norm_effort = "high"
        elif eff in ("medium", "med", "normal"):
            norm_effort = "medium"

        efforts_dict = base_efforts[clean]
        if norm_effort in efforts_dict:
            return efforts_dict[norm_effort]
        if "thinking" in efforts_dict:
            return efforts_dict["thinking"]
        if norm_effort == "medium" and "high" in efforts_dict:
            return efforts_dict["high"]
        if "high" in efforts_dict:
            return efforts_dict["high"]

    if clean in base_defaults:
        return base_defaults[clean]

    if clean in slug_map:
        return slug_map[clean]

    return model_name.strip()


def format_tool_specs(tools: Optional[List[Dict[str, Any]]]) -> str:
    """Format OpenAI tools array into clear JSON schema and invocation instructions for agy."""
    if not tools:
        return ""
    specs = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") or {}
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not name:
            continue
        specs.append({
            "name": name.strip(),
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters", {}),
        })
    if not specs:
        return ""

    spec_json = json.dumps(specs, ensure_ascii=False, indent=2)
    return (
        "## Available Tools\n"
        "You have access to the following tools:\n"
        f"```json\n{spec_json}\n```\n\n"
        "## Tool Calling Instructions\n"
        "- When you need to take action (e.g. read files, write files, execute shell commands, web search, edit code), call a tool.\n"
        "- If you need to think or plan before taking action, enclose your internal reasoning inside `<think>...</think>` tags.\n"
        "- To invoke a tool, output a `<tool_call>` XML block like this:\n"
        "  <tool_call>\n"
        "  <name>tool_name</name>\n"
        "  <arguments>{\"param1\": \"val1\"}</arguments>\n"
        "  </tool_call>\n"
        "- You may write natural explanations or thoughts before or after the `<tool_call>`.\n"
        "- Do not run the tools yourself. Output the `<tool_call>` block for the caller to execute.\n"
        "- The caller will execute your tool call on the system and provide the result in the next turn."
    )


class StreamToolCallFilter:
    """Split streamed AGY text into content, reasoning, and OpenAI tool deltas."""

    def __init__(self, allow_tool_calls: bool = True) -> None:
        self.buffer = ""
        self.state = "TEXT"
        self.allow_tool_calls = allow_tool_calls
        self.current_tc_id = ""
        self.current_tc_index = 0
        self.tool_calls_count = 0
        self.current_tc_name = ""
        self.current_tc_args = ""
        self.logged_tool_call_ids: set = set()

    def feed(self, text: str) -> Tuple[str, str, List[Dict[str, Any]], List[int]]:
        self.buffer += text
        emitted_text = ""
        emitted_reasoning = ""
        emitted_tool_chunks: List[Dict[str, Any]] = []
        emitted_tools_done: List[int] = []

        while self.buffer:
            if self.state == "TEXT":
                think_idx = self.buffer.find("<think>")
                tool_idx = self.buffer.find("<tool_call>") if self.allow_tool_calls else -1

                idxs = [(think_idx, "THINK"), (tool_idx, "TOOL")]
                idxs = [(idx, tag) for idx, tag in idxs if idx != -1]
                if idxs:
                    min_idx, tag = min(idxs, key=lambda x: x[0])
                    emitted_text += self.buffer[:min_idx]
                    
                    if tag == "THINK":
                        self.buffer = self.buffer[min_idx + len("<think>"):]
                        self.state = "THINK"
                    else:
                        self.buffer = self.buffer[min_idx + len("<tool_call>"):]
                        self.state = "TOOL_WAIT_NAME"
                        self.current_tc_id = f"call_{uuid.uuid4().hex[:8]}"
                        self.current_tc_index = self.tool_calls_count
                        self.current_tc_name = ""
                        self.current_tc_args = ""
                        self.tool_calls_count += 1
                else:
                    prefix_match = False
                    possible_tags = ["<think>"]
                    if self.allow_tool_calls:
                        possible_tags.append("<tool_call>")
                    for tag in possible_tags:
                        for i in range(1, len(tag)):
                            if self.buffer.endswith(tag[:i]):
                                emitted_text += self.buffer[:-i]
                                self.buffer = self.buffer[-i:]
                                prefix_match = True
                                break
                        if prefix_match:
                            break
                    if not prefix_match:
                        emitted_text += self.buffer
                        self.buffer = ""
                    break

            elif self.state == "THINK":
                end_idx = self.buffer.find("</think>")
                if end_idx != -1:
                    emitted_reasoning += self.buffer[:end_idx]
                    self.buffer = self.buffer[end_idx + len("</think>"):]
                    self.state = "TEXT"
                else:
                    prefix_match = False
                    tag = "</think>"
                    for i in range(1, len(tag)):
                        if self.buffer.endswith(tag[:i]):
                            emitted_reasoning += self.buffer[:-i]
                            self.buffer = self.buffer[-i:]
                            prefix_match = True
                            break
                    if not prefix_match:
                        emitted_reasoning += self.buffer
                        self.buffer = ""
                    break

            elif self.state == "TOOL_WAIT_NAME":
                name_match = re.search(r"<name>\s*(.*?)\s*</name>", self.buffer)
                if name_match:
                    name = name_match.group(1).strip()
                    self.current_tc_name = name
                    self.current_tc_args = ""
                    emitted_tool_chunks.append({
                        "index": self.current_tc_index,
                        "id": self.current_tc_id,
                        "type": "function",
                        "function": {"name": name, "arguments": ""}
                    })
                    self.buffer = self.buffer[name_match.end():]
                    self.state = "TOOL_WAIT_ARGS_OPEN"
                else:
                    brace_idx = self.buffer.find("{")
                    name_open_idx = self.buffer.find("<name>")
                    if brace_idx != -1 and (name_open_idx == -1 or brace_idx < name_open_idx):
                        self.state = "TOOL_JSON_FALLBACK"
                    elif "</tool_call>" in self.buffer:
                        self.state = "TEXT"
                        self.buffer = self.buffer[self.buffer.find("</tool_call>") + len("</tool_call>"):]
                    else:
                        break

            elif self.state == "TOOL_WAIT_ARGS_OPEN":
                tag = "<arguments>"
                start_idx = self.buffer.find(tag)
                if start_idx != -1:
                    self.buffer = self.buffer[start_idx + len(tag):]
                    self.state = "TOOL_ARGS"
                elif "</tool_call>" in self.buffer:
                    tc_end = self.buffer.find("</tool_call>")
                    self.buffer = self.buffer[tc_end + len("</tool_call>"):]
                    self.state = "TEXT"
                else:
                    held = ""
                    for i in range(min(len(self.buffer), len(tag) - 1), 0, -1):
                        if self.buffer.endswith(tag[:i]):
                            held = self.buffer[-i:]
                            break
                    self.buffer = held
                    break

            elif self.state == "TOOL_ARGS":
                tag = "</arguments>"
                end_idx = self.buffer.find(tag)
                if end_idx != -1:
                    args = self.buffer[:end_idx]
                    if args:
                        self.current_tc_args += args
                        emitted_tool_chunks.append({
                            "index": self.current_tc_index,
                            "function": {"arguments": args}
                        })
                    emitted_tools_done.append(self.current_tc_index)
                    if self.current_tc_id and self.current_tc_id not in self.logged_tool_call_ids:
                        self.logged_tool_call_ids.add(self.current_tc_id)
                        log_tool_call_dispatched(self.current_tc_name, self.current_tc_id, self.current_tc_args)
                    self.buffer = self.buffer[end_idx + len(tag):]
                    self.state = "TOOL_ARGS_END_WAIT"
                else:
                    held = ""
                    chunk = self.buffer
                    for i in range(min(len(self.buffer), len(tag) - 1), 0, -1):
                        if self.buffer.endswith(tag[:i]):
                            chunk = self.buffer[:-i]
                            held = self.buffer[-i:]
                            break
                    if chunk:
                        self.current_tc_args += chunk
                        emitted_tool_chunks.append({
                            "index": self.current_tc_index,
                            "function": {"arguments": chunk}
                        })
                    self.buffer = held
                    break

            elif self.state == "TOOL_ARGS_END_WAIT":
                tc_end = self.buffer.find("</tool_call>")
                if tc_end != -1:
                    self.buffer = self.buffer[tc_end + len("</tool_call>"):]
                    self.state = "TEXT"
                else:
                    break

            elif self.state == "TOOL_JSON_FALLBACK":
                end_idx = self.buffer.find("</tool_call>")
                if end_idx != -1:
                    raw_json = self.buffer[:end_idx].strip()
                    self.buffer = self.buffer[end_idx + len("</tool_call>"):]
                    self.state = "TEXT"
                    
                    cleaned = raw_json
                    if cleaned.startswith("```"):
                        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
                        cleaned = re.sub(r"\n?```$", "", cleaned).strip()
                    
                    try:
                        data = json.loads(cleaned)
                        name = data.get("name") or (data.get("function") or {}).get("name")
                        args = data.get("arguments") or (data.get("function") or {}).get("arguments") or {}
                        args_str = json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args)
                        if name:
                            self.current_tc_name = str(name).strip()
                            self.current_tc_args = args_str
                            emitted_tool_chunks.append({
                                "index": self.current_tc_index,
                                "id": self.current_tc_id,
                                "type": "function",
                                "function": {"name": self.current_tc_name, "arguments": ""}
                            })
                            emitted_tool_chunks.append({
                                "index": self.current_tc_index,
                                "function": {"arguments": args_str}
                            })
                            emitted_tools_done.append(self.current_tc_index)
                            if self.current_tc_id and self.current_tc_id not in self.logged_tool_call_ids:
                                self.logged_tool_call_ids.add(self.current_tc_id)
                                log_tool_call_dispatched(self.current_tc_name, self.current_tc_id, self.current_tc_args)
                    except Exception:
                        pass
                else:
                    break

        return emitted_text, emitted_reasoning, emitted_tool_chunks, emitted_tools_done

    def flush(self) -> Tuple[str, str, List[Dict[str, Any]], List[int]]:
        """Release any safe trailing data when the upstream stream ends."""
        emitted_text = ""
        emitted_reasoning = ""
        emitted_tool_chunks: List[Dict[str, Any]] = []
        if self.state == "TEXT":
            emitted_text = self.buffer
        elif self.state == "THINK":
            emitted_reasoning = self.buffer
        elif self.state == "TOOL_ARGS" and self.buffer:
            self.current_tc_args += self.buffer
            emitted_tool_chunks.append({
                "index": self.current_tc_index,
                "function": {"arguments": self.buffer},
            })
            if self.current_tc_id and self.current_tc_id not in self.logged_tool_call_ids:
                self.logged_tool_call_ids.add(self.current_tc_id)
                log_tool_call_dispatched(self.current_tc_name, self.current_tc_id, self.current_tc_args)
        self.buffer = ""
        return emitted_text, emitted_reasoning, emitted_tool_chunks, []


def extract_tool_calls_from_text(text: str) -> Tuple[List[Dict[str, Any]], str]:
    """Parse <tool_call>...</tool_call> blocks from text for non-streaming completions."""
    if not text:
        return [], ""
    f = StreamToolCallFilter()
    txt, _, tcs, _ = f.feed(text)
    txt_flush, _, tcs_flush, _ = f.flush()
    
    final_tcs = []
    current_tc = None
    for chunk in tcs + tcs_flush:
        if "id" in chunk:
            current_tc = chunk
            final_tcs.append(current_tc)
        elif current_tc:
            current_tc["function"]["arguments"] += chunk["function"]["arguments"]
            
    return final_tcs, (txt + txt_flush).strip()


def _clip_prompt_section(
    text: str, limit: int, *, keep_tail: bool = False, keep_both: bool = False
) -> str:
    """Bound a prompt section while making every omission explicit."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    marker = "\n\n[... older intermediate tool output omitted by AGY bridge safety limit ...]\n\n"
    available = max(0, limit - len(marker))
    if keep_both:
        head = available // 4
        tail = available - head
        return text[:head] + marker + (text[-tail:] if tail else "")
    if keep_tail:
        return marker + text[-available:]
    head = available * 2 // 3
    tail = available - head
    return text[:head] + marker + (text[-tail:] if tail else "")


def _message_text(msg: Dict[str, Any]) -> str:
    content = msg.get("content", "")
    if isinstance(content, list):
        parts: List[str] = []
        for p in content:
            if not isinstance(p, dict):
                if p:
                    parts.append(str(p))
                continue
            ptype = p.get("type", "")
            if ptype == "text" or ("text" in p and ptype != "image_url"):
                txt = p.get("text", "")
                if txt:
                    parts.append(str(txt))
            elif ptype == "image_url" or "image_url" in p:
                img_info = p.get("image_url", {})
                url = (
                    img_info.get("url", "")
                    if isinstance(img_info, dict)
                    else (img_info if isinstance(img_info, str) else "")
                )
                if not url and "url" in p:
                    url = str(p["url"])

                if not url:
                    continue

                if url.startswith("data:image/"):
                    try:
                        header, b64_data = url.split(",", 1)
                        mime = header.split(";")[0].replace("data:", "").strip().lower()
                        ext = "png"
                        if "jpeg" in mime or "jpg" in mime:
                            ext = "jpg"
                        elif "webp" in mime:
                            ext = "webp"
                        elif "gif" in mime:
                            ext = "gif"

                        img_hash = hashlib.md5(b64_data[:200].encode("utf-8")).hexdigest()[:12]
                        temp_img_path = os.path.join(
                            BRIDGE_SCRATCH_DIR, f"vision_input_{img_hash}.{ext}"
                        )
                        if not os.path.exists(temp_img_path):
                            with open(temp_img_path, "wb") as img_f:
                                img_f.write(base64.b64decode(b64_data))
                        parts.append(
                            f"\n[Attached Image File: {temp_img_path}]\n"
                            f"Please inspect and analyze the image at: {temp_img_path}\n"
                        )
                    except Exception as ex:
                        logger.warning("Failed to decode base64 image_url: %s", ex)
                elif url.startswith("file://"):
                    parsed = urlparse(url)
                    file_path = unquote(parsed.path)
                    if (
                        sys.platform == "win32"
                        and file_path.startswith("/")
                        and len(file_path) > 2
                        and file_path[2] == ":"
                    ):
                        file_path = file_path[1:]
                    parts.append(
                        f"\n[Attached Image File: {file_path}]\n"
                        f"Please inspect and analyze the image at: {file_path}\n"
                    )
                elif os.path.isfile(url):
                    parts.append(
                        f"\n[Attached Image File: {url}]\n"
                        f"Please inspect and analyze the image at: {url}\n"
                    )
                elif url.startswith(("http://", "https://")):
                    parts.append(
                        f"\n[Attached Image URL: {url}]\n"
                        f"Please inspect and analyze the image at: {url}\n"
                    )
                else:
                    parts.append(f"\n[Attached Image: {url}]\n")
        return "\n".join(parts)
    return str(content or "")


def _render_agy_message(msg: Dict[str, Any]) -> str:
    role = msg.get("role", "user")
    text = _message_text(msg)
    if role == "assistant":
        call_strs: List[str] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            call_strs.append(
                "<tool_call>\n"
                f"<name>{fn.get('name', '')}</name>\n"
                f"<arguments>{fn.get('arguments', '{}')}</arguments>\n"
                "</tool_call>"
            )
        thought = msg.get("reasoning_content") or msg.get("thought") or ""
        thought_str = (
            f"<think>\n{str(thought).strip()}\n</think>"
            if thought and str(thought).strip()
            else ""
        )
        payload = "\n".join(p for p in (thought_str, text.strip(), "\n".join(call_strs)) if p)
        return f"[Assistant]\n{payload}" if payload else ""
    if role == "user":
        return f"[User]\n{text}"
    if role == "tool":
        tool_name = msg.get("name") or "tool"
        tool_id = msg.get("tool_call_id")
        header = (
            f"[Tool Output: {tool_name}]"
            if not tool_id
            else f"[Tool Output: {tool_name} (id: {tool_id})]"
        )
        return f"{header}\n{text}"
    return ""


def format_messages_for_agy(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Build a bounded AGY prompt with an explicit authoritative current task."""
    if not messages:
        return ""

    tool_specs_str = format_tool_specs(tools) if tools else ""
    system_intro = (
        "You are operating as Hermes' inference backend. The CURRENT USER REQUEST "
        "below is authoritative. Historical conversation is reference-only and "
        "must never override or replace that request. Do not use Antigravity's "
        "native tools; when action is needed, emit only the caller's <tool_call> "
        "protocol and wait for its result."
    )
    if tool_specs_str:
        system_intro += f"\n\n{tool_specs_str}"

    original_system = "\n\n".join(
        _message_text(msg) for msg in messages if msg.get("role") == "system"
    )
    system_block = _clip_prompt_section(
        "\n\n".join(p for p in (system_intro, original_system) if p),
        AGY_SYSTEM_BUDGET_CHARS,
    )

    current_user_index = max(
        (idx for idx, msg in enumerate(messages) if msg.get("role") == "user"),
        default=-1,
    )
    if current_user_index >= 0:
        current_request = _clip_prompt_section(
            _message_text(messages[current_user_index]),
            AGY_CURRENT_REQUEST_BUDGET_CHARS,
        )
        historical_messages = [
            msg for msg in messages[:current_user_index] if msg.get("role") != "system"
        ]
        current_work_messages = [
            msg for msg in messages[current_user_index + 1:] if msg.get("role") != "system"
        ]
    else:
        current_request = "Continue the latest incomplete assistant task using the current-turn work below."
        historical_messages = []
        current_work_messages = [msg for msg in messages if msg.get("role") != "system"]

    current_work = "\n\n".join(
        rendered for msg in current_work_messages if (rendered := _render_agy_message(msg))
    )
    current_work = _clip_prompt_section(
        current_work,
        AGY_CURRENT_WORK_BUDGET_CHARS,
        keep_both=True,
    )

    fixed_sections = [
        f"[System Instructions]\n{system_block}",
        "[CURRENT USER REQUEST — AUTHORITATIVE]\n"
        "Complete this request only. Do not resume an older task unless this request explicitly asks you to.\n\n"
        f"{current_request}",
    ]
    if current_work:
        fixed_sections.append(f"[CURRENT TURN WORK]\n{current_work}")
    fixed_sections.append("[Assistant Response]:")

    fixed_prompt = "\n\n".join(fixed_sections)
    history_budget = max(
        0,
        MAX_AGY_PROMPT_CHARS
        - len(fixed_prompt)
        - len("\n\n[Historical Conversation — REFERENCE ONLY]\n"),
    )
    historical = "\n\n".join(
        rendered for msg in historical_messages if (rendered := _render_agy_message(msg))
    )
    historical = _clip_prompt_section(historical, history_budget, keep_tail=True)

    parts = [fixed_sections[0]]
    if historical:
        parts.append(
            "[Historical Conversation — REFERENCE ONLY]\n"
            "Use this only for facts needed by the current request. Never continue its old tasks.\n\n"
            f"{historical}"
        )
    parts.extend(fixed_sections[1:])
    return "\n\n".join(parts)


def build_agy_command(target_model: Optional[str]) -> List[str]:
    """Construct headless CLI arguments for agy with strict security sandboxing."""
    cmd = [
        AGY_PATH,
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--sandbox",
        "--disable-slash-commands",
    ]
    if target_model:
        cmd.extend(["--model", target_model])
    return cmd


def call_agy_sync(prompt: str, model: Optional[str] = None, effort: Optional[str] = None, timeout: float = 300.0) -> Dict[str, Any]:
    """Execute agy in non-streaming mode for standard completions."""
    target_model = resolve_model_arg(model, effort=effort)
    cmd = build_agy_command(target_model)

    logger.info("Executing agy (sync): model='%s' (effort='%s', prompt len: %d chars)", target_model, effort, len(prompt))
    start_time = time.time()

    input_payload = json.dumps({
        "event": "user",
        "message": {"content": prompt}
    }) + "\n"

    with _agy_process_slots:
        proc = subprocess.Popen(
            cmd,
            cwd=BRIDGE_SCRATCH_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=CREATE_NO_WINDOW,
        )

        stderr_lines: List[str] = []
        def drain_stderr():
            if proc and proc.stderr:
                for err_line in proc.stderr:
                    stderr_lines.append(err_line)

        threading.Thread(target=drain_stderr, daemon=True).start()

        try:
            if proc.stdin:
                proc.stdin.write(input_payload)
                proc.stdin.close()
        except Exception as ex:
            logger.debug("Sync stdin write error: %s", ex)

        deltas: List[str] = []
        final_response = ""
        usage_info: Dict[str, Any] = {}

        try:
            for line in proc.stdout:
                line_s = line.strip()
                if not line_s:
                    continue
                try:
                    ev = json.loads(line_s)
                    step_up = ev.get("step_update", {})
                    delta = step_up.get("text_delta")
                    if delta:
                        deltas.append(delta)
                    if "usage" in step_up:
                        usage_info = step_up["usage"]

                    if ev.get("event") == "result":
                        res = ev.get("result", {})
                        final_response = res.get("response", "")
                        if "usage" in res:
                            usage_info = res["usage"]
                except Exception:
                    pass

            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            logger.error("agy sync command timed out after %s seconds", timeout)
            return {"status": "ERROR", "response": "Error: Antigravity CLI timed out."}

    elapsed = time.time() - start_time
    logger.info("agy sync finished in %.2fs (exit code %d)", elapsed, proc.returncode)

    if not final_response and deltas:
        final_response = "".join(deltas)

    if proc.returncode != 0 and not final_response:
        err_text = "".join(stderr_lines).strip()
        logger.error("agy sync stderr: %s", err_text)
        return {"status": "ERROR", "response": f"Error from agy CLI:\n{err_text}"}

    return {"status": "SUCCESS", "response": final_response, "usage": usage_info}


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class OpenAIBaseHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - - [%s] %s", self.client_address[0], self.log_date_time_string(), format % args)

    def _get_trusted_origin(self) -> Optional[str]:
        """Validate incoming browser Origin. Allow only local loopback or desktop webviews."""
        origin = self.headers.get("Origin", "").strip()
        if not origin:
            return None
        if re.match(r"^https?://(?:localhost|127\.0\.0\.1)(?::\d+)?$", origin, re.IGNORECASE):
            return origin
        if origin.startswith(("vscode-webview://", "app://")):
            return origin
        return None

    def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        trusted_origin = self._get_trusted_origin()
        if trusted_origin:
            self.send_header("Access-Control-Allow-Origin", trusted_origin)
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-API-Key, X-Reasoning-Effort")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(data)

    def _is_authorized(self) -> bool:
        expected = get_or_create_auth_token()
        if not expected:
            return True
        auth_hdr = self.headers.get("Authorization", "").strip()
        if not auth_hdr:
            auth_hdr = self.headers.get("X-API-Key", "").strip()
        if auth_hdr.lower().startswith("bearer "):
            token = auth_hdr[7:].strip()
        else:
            token = auth_hdr
        return token == expected

    def do_OPTIONS(self) -> None:
        trusted_origin = self._get_trusted_origin()
        if trusted_origin:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", trusted_origin)
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type, X-API-Key, X-Reasoning-Effort")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.end_headers()
        else:
            self.send_response(403)
            self.end_headers()

    def do_GET(self) -> None:
        if self.headers.get("Origin") and not self._get_trusted_origin():
            self._send_json(403, {"error": {"message": "Forbidden: Untrusted cross-origin browser request", "type": "access_denied"}})
            return

        path = self.path.split("?")[0].rstrip("/")
        if path in ("", "/health"):
            catalog, _, _, _ = get_models_catalog()
            self._send_json(200, {
                "status": "ok",
                "service": "agy-bridge",
                "agy_path": AGY_PATH,
                "models_count": len(catalog),
            })
            return

        if not self._is_authorized():
            self._send_json(401, {
                "error": {
                    "message": "Unauthorized: Missing or invalid local bridge Bearer token",
                    "type": "authentication_error",
                }
            })
            return

        if path in ("/models", "/v1/models"):
            catalog, _, _, _ = get_models_catalog()
            self._send_json(200, {"object": "list", "data": catalog})
        else:
            self._send_json(404, {"error": {"message": f"Not found: {path}", "type": "invalid_request_error"}})

    def do_POST(self) -> None:
        if self.headers.get("Origin") and not self._get_trusted_origin():
            self._send_json(403, {"error": {"message": "Forbidden: Untrusted cross-origin browser request", "type": "access_denied"}})
            return

        path = self.path.split("?")[0].rstrip("/")
        if path not in ("/chat/completions", "/v1/chat/completions"):
            self._send_json(404, {"error": {"message": f"Not found: {path}", "type": "invalid_request_error"}})
            return

        if not self._is_authorized():
            self._send_json(401, {
                "error": {
                    "message": "Unauthorized: Missing or invalid local bridge Bearer token",
                    "type": "authentication_error",
                }
            })
            return

        content_len = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_len).decode("utf-8")

        try:
            req = json.loads(post_body)
        except json.JSONDecodeError as exc:
            self._send_json(400, {"error": {"message": f"Invalid JSON: {exc}", "type": "invalid_request_error"}})
            return

        messages = req.get("messages", [])
        tools = req.get("tools")
        model = req.get("model", "gemini-3.8-flash")
        stream = bool(req.get("stream", False))

        # Scan and log any incoming tool execution results from Hermes
        log_incoming_tool_results(messages)

        if tools and isinstance(tools, list):
            tool_names = []
            for t in tools:
                if isinstance(t, dict):
                    fn = t.get("function", {})
                    tname = fn.get("name") if isinstance(fn, dict) else t.get("name")
                    if tname:
                        tool_names.append(str(tname))
            if tool_names:
                logger.info("Request provides %d tool definitions: %s", len(tool_names), ", ".join(tool_names))

        effort = (
            req.get("reasoning_effort")
            or req.get("effort")
            or (req.get("extra_body") or {}).get("reasoning_effort")
            or (req.get("thinking") or {}).get("effort")
            or self.headers.get("X-Reasoning-Effort")
        )

        prompt = format_messages_for_agy(messages, tools=tools)
        if not prompt.strip():
            prompt = "Hello"

        call_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        created_ts = int(time.time())

        if stream:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            trusted_origin = self._get_trusted_origin()
            if trusted_origin:
                self.send_header("Access-Control-Allow-Origin", trusted_origin)
            self.end_headers()

            write_lock = threading.Lock()
            stop_heartbeat = threading.Event()
            client_disconnected = threading.Event()

            def safe_write(raw_bytes: bytes) -> bool:
                if client_disconnected.is_set():
                    return False
                with write_lock:
                    try:
                        self.wfile.write(raw_bytes)
                        self.wfile.flush()
                        return True
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                        client_disconnected.set()
                        return False

            def send_heartbeats():
                tick = 0
                while not stop_heartbeat.wait(2.0):
                    if client_disconnected.is_set():
                        break
                    safe_write(b": heartbeat\n\n")
                    tick += 1
                    status = "Antigravity is working…\n" if tick == 1 else ""
                    chunk_hb = {
                        "id": call_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": model,
                        "choices": [{"index": 0, "delta": {"reasoning_content": status}, "finish_reason": None}]
                    }
                    safe_write(f"data: {json.dumps(chunk_hb, ensure_ascii=False)}\n\n".encode("utf-8"))

            chunk_role = {
                "id": call_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": ""},
                        "finish_reason": None,
                    }
                ],
            }
            safe_write(f"data: {json.dumps(chunk_role, ensure_ascii=False)}\n\n".encode("utf-8"))

            hb_thread = threading.Thread(target=send_heartbeats, daemon=True)
            hb_thread.start()

            target_model = resolve_model_arg(model, effort=effort)
            cmd = build_agy_command(target_model)

            logger.info(
                "Executing agy (stream): model='%s' (effort='%s', prompt len: %d chars, tools: %s)",
                target_model,
                effort,
                len(prompt),
                bool(tools),
            )
            start_time = time.time()

            proc: Optional[subprocess.Popen] = None
            tc_filter = StreamToolCallFilter(allow_tool_calls=bool(tools))

            try:
                with _agy_process_slots:
                    if client_disconnected.is_set():
                        logger.info("Client disconnected before process started")
                        return

                    proc = subprocess.Popen(
                        cmd,
                        cwd=BRIDGE_SCRATCH_DIR,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        bufsize=1,
                        creationflags=CREATE_NO_WINDOW,
                    )

                    input_payload = json.dumps({
                        "event": "user",
                        "message": {"content": prompt},
                    }) + "\n"

                    def drain_proc_stderr():
                        if proc and proc.stderr:
                            for _ in proc.stderr:
                                pass

                    err_t = threading.Thread(target=drain_proc_stderr, daemon=True)
                    err_t.start()

                    def write_proc_stdin():
                        try:
                            if proc and proc.stdin:
                                proc.stdin.write(input_payload)
                                proc.stdin.close()
                        except Exception as ex:
                            logger.debug("Stream stdin write error: %s", ex)

                    in_t = threading.Thread(target=write_proc_stdin, daemon=True)
                    in_t.start()

                    has_emitted_delta = False
                    for line in proc.stdout:
                        if client_disconnected.is_set():
                            logger.info("Client disconnected during stream, terminating agy")
                            proc.kill()
                            break

                        line_s = line.strip()
                        if not line_s:
                            continue

                        try:
                            ev = json.loads(line_s)
                            step_up = ev.get("step_update", {})
                            if not isinstance(step_up, dict):
                                step_up = {}

                            delta = (
                                step_up.get("text_delta")
                                if step_up.get("step_type") == "agent_response"
                                else None
                            )
                            if delta:
                                has_emitted_delta = True
                                if tc_filter:
                                    txt, rsn, tcs, _ = tc_filter.feed(delta)
                                    if rsn:
                                        chunk_rsn = {
                                            "id": call_id,
                                            "object": "chat.completion.chunk",
                                            "created": created_ts,
                                            "model": model,
                                            "choices": [{"index": 0, "delta": {"reasoning_content": rsn}, "finish_reason": None}]
                                        }
                                        if not safe_write(f"data: {json.dumps(chunk_rsn, ensure_ascii=False)}\n\n".encode("utf-8")):
                                            proc.kill()
                                            break
                                    if txt:
                                        chunk_content = {
                                            "id": call_id,
                                            "object": "chat.completion.chunk",
                                            "created": created_ts,
                                            "model": model,
                                            "choices": [{"index": 0, "delta": {"content": txt}, "finish_reason": None}]
                                        }
                                        if not safe_write(f"data: {json.dumps(chunk_content, ensure_ascii=False)}\n\n".encode("utf-8")):
                                            proc.kill()
                                            break
                                    for tc in tcs:
                                        chunk_tool = {
                                            "id": call_id,
                                            "object": "chat.completion.chunk",
                                            "created": created_ts,
                                            "model": model,
                                            "choices": [{"index": 0, "delta": {"tool_calls": [tc]}, "finish_reason": None}]
                                        }
                                        if not safe_write(f"data: {json.dumps(chunk_tool, ensure_ascii=False)}\n\n".encode("utf-8")):
                                            proc.kill()
                                            break
                                else:
                                    chunk_content = {
                                        "id": call_id,
                                        "object": "chat.completion.chunk",
                                        "created": created_ts,
                                        "model": model,
                                        "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}]
                                    }
                                    if not safe_write(f"data: {json.dumps(chunk_content, ensure_ascii=False)}\n\n".encode("utf-8")):
                                        proc.kill()
                                        break
                            elif ev.get("event") == "result":
                                res = ev.get("result", {})
                                full_resp = res.get("response", "")
                                if not has_emitted_delta and full_resp:
                                    if tc_filter:
                                        txt, rsn, tcs, _ = tc_filter.feed(full_resp)
                                        if rsn:
                                            chunk_rsn = {
                                                "id": call_id,
                                                "object": "chat.completion.chunk",
                                                "created": created_ts,
                                                "model": model,
                                                "choices": [{"index": 0, "delta": {"reasoning_content": rsn}, "finish_reason": None}]
                                            }
                                            safe_write(f"data: {json.dumps(chunk_rsn, ensure_ascii=False)}\n\n".encode("utf-8"))
                                        if txt:
                                            chunk_content = {
                                                "id": call_id,
                                                "object": "chat.completion.chunk",
                                                "created": created_ts,
                                                "model": model,
                                                "choices": [{"index": 0, "delta": {"content": txt}, "finish_reason": None}]
                                            }
                                            safe_write(f"data: {json.dumps(chunk_content, ensure_ascii=False)}\n\n".encode("utf-8"))
                                        for tc in tcs:
                                            chunk_tool = {
                                                "id": call_id,
                                                "object": "chat.completion.chunk",
                                                "created": created_ts,
                                                "model": model,
                                                "choices": [{"index": 0, "delta": {"tool_calls": [tc]}, "finish_reason": None}]
                                            }
                                            safe_write(f"data: {json.dumps(chunk_tool, ensure_ascii=False)}\n\n".encode("utf-8"))
                                    else:
                                        chunk_content = {
                                            "id": call_id,
                                            "object": "chat.completion.chunk",
                                            "created": created_ts,
                                            "model": model,
                                            "choices": [
                                                {
                                                    "index": 0,
                                                    "delta": {"content": full_resp},
                                                    "finish_reason": None,
                                                }
                                            ],
                                        }
                                        safe_write(f"data: {json.dumps(chunk_content, ensure_ascii=False)}\n\n".encode("utf-8"))
                                stop_heartbeat.set()
                                break
                        except Exception as exc:
                            logger.warning("Ignoring malformed AGY stream event: %s", exc)

                    if tc_filter:
                        flush_txt, flush_rsn, flush_tcs, _ = tc_filter.flush()
                        if flush_rsn:
                            chunk_rsn = {
                                "id": call_id,
                                "object": "chat.completion.chunk",
                                "created": created_ts,
                                "model": model,
                                "choices": [{"index": 0, "delta": {"reasoning_content": flush_rsn}, "finish_reason": None}]
                            }
                            safe_write(f"data: {json.dumps(chunk_rsn, ensure_ascii=False)}\n\n".encode("utf-8"))
                        if flush_txt:
                            chunk_content = {
                                "id": call_id,
                                "object": "chat.completion.chunk",
                                "created": created_ts,
                                "model": model,
                                "choices": [{"index": 0, "delta": {"content": flush_txt}, "finish_reason": None}]
                            }
                            safe_write(f"data: {json.dumps(chunk_content, ensure_ascii=False)}\n\n".encode("utf-8"))
                        for tc in flush_tcs:
                            chunk_tool = {
                                "id": call_id,
                                "object": "chat.completion.chunk",
                                "created": created_ts,
                                "model": model,
                                "choices": [{"index": 0, "delta": {"tool_calls": [tc]}, "finish_reason": None}]
                            }
                            safe_write(f"data: {json.dumps(chunk_tool, ensure_ascii=False)}\n\n".encode("utf-8"))

                    stop_heartbeat.set()
                    if not client_disconnected.is_set():
                        finish_reason = "tool_calls" if (tc_filter and tc_filter.tool_calls_count > 0) else "stop"
                        chunk_finish = {
                            "id": call_id,
                            "object": "chat.completion.chunk",
                            "created": created_ts,
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {},
                                    "finish_reason": finish_reason,
                                }
                            ],
                        }
                        safe_write(f"data: {json.dumps(chunk_finish, ensure_ascii=False)}\n\n".encode("utf-8"))
                        safe_write(b"data: [DONE]\n\n")
                        self.close_connection = True

                    proc.wait(timeout=10)
                    elapsed = time.time() - start_time
                    logger.info("agy stream finished in %.2fs (exit code %s)", elapsed, proc.returncode)

            except Exception as exc:
                logger.error("Error during streaming execution: %s", exc, exc_info=True)
                if not client_disconnected.is_set():
                    err_chunk = {
                        "id": call_id,
                        "object": "chat.completion.chunk",
                        "created": created_ts,
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": f"\n\n[Bridge Error: {exc}]"},
                                "finish_reason": None,
                            }
                        ],
                    }
                    safe_write(f"data: {json.dumps(err_chunk, ensure_ascii=False)}\n\n".encode("utf-8"))
                    safe_write(b"data: [DONE]\n\n")
                    self.close_connection = True
            finally:
                stop_heartbeat.set()
                hb_thread.join(timeout=1.0)
                if proc and proc.poll() is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass

        else:
            agy_result = call_agy_sync(prompt, model=model, effort=effort)
            answer = agy_result.get("response", "")
            usage = agy_result.get("usage", {})

            tool_calls = []
            finish_reason = "stop"
            if tools:
                extracted_tcs, cleaned_content = extract_tool_calls_from_text(answer)
                if extracted_tcs:
                    tool_calls = extracted_tcs
                    answer = cleaned_content
                    finish_reason = "tool_calls"

            message_payload: Dict[str, Any] = {
                "role": "assistant",
                "content": answer if (answer or not tool_calls) else None,
            }
            if tool_calls:
                message_payload["tool_calls"] = tool_calls

            resp = {
                "id": call_id,
                "object": "chat.completion",
                "created": created_ts,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": message_payload,
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": {
                    "prompt_tokens": usage.get("input_tokens", len(prompt) // 4),
                    "completion_tokens": usage.get("output_tokens", len(answer or "") // 4),
                    "total_tokens": usage.get("total_tokens", (len(prompt) + len(answer or "")) // 4),
                },
            }
            self._send_json(200, resp)


def _start_update_watchdog() -> None:
    """Watchdog thread that monitors for Hermes updates.

    If an update is initiated (detected via marker or flag), this watchdog
    gracefully shuts down the bridge process so Hermes can update without
    file lock or port conflicts.
    """
    def watchdog_loop() -> None:
        marker_main = os.path.join(hermes_home, ".hermes-update-in-progress")
        marker_temp = os.path.join(tempfile.gettempdir(), ".hermes-update-in-progress")
        flag_disabled = os.path.join(tempfile.gettempdir(), "agy_bridge_disabled.flag")

        while True:
            time.sleep(1.0)
            if os.path.isfile(marker_main) or os.path.isfile(marker_temp) or os.path.isfile(flag_disabled):
                logger.info(
                    "Hermes update marker detected (%s). Shutting down agy_bridge gracefully to allow update...",
                    marker_main if os.path.isfile(marker_main) else marker_temp,
                )
                os._exit(0)

    t = threading.Thread(target=watchdog_loop, daemon=True, name="hermes-update-watchdog")
    t.start()


def run_server() -> None:
    _start_update_watchdog()

    if not os.path.isfile(AGY_PATH) and shutil.which(AGY_PATH) is None:
        logger.warning("Antigravity CLI (agy) not found. Please check installation.")
    else:
        logger.info("Found Antigravity CLI at %s", AGY_PATH)

    cat, _, _, _ = get_models_catalog()
    logger.info("Initialized %d dynamic base models from Antigravity", len(cat))

    try:
        server = ThreadingHTTPServer((HOST, PORT), OpenAIBaseHandler)
    except OSError as exc:
        logger.warning("Could not bind %s:%d (already running?): %s", HOST, PORT, exc)
        return

    logger.info("AGY OpenAI Bridge running on http://%s:%d/v1", HOST, PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down AGY Bridge...")
    except Exception as exc:
        logger.error("Server loop crashed: %s", exc, exc_info=True)
    finally:
        try:
            server.server_close()
        except Exception:
            pass


if __name__ == "__main__":
    run_server()
