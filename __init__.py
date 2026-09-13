"""Google Antigravity (AGY) provider plugin for Hermes Agent.

Exposes the local Antigravity bridge (http://127.0.0.1:8765/v1) as a native
model provider in Hermes Agent. Dynamically discovers all models supported by
Google Antigravity CLI without hardcoding.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any, List, Optional, Tuple

from .bridge_manager import ensure_bridge_running, is_bridge_healthy, is_update_in_progress

logger = logging.getLogger("hermes.plugins.antigravity")

# Hermes' generic OpenAI transport requires an API-key-shaped value even for
# a loopback, keyless provider. The bridge ignores the token; set a local-only
# placeholder so explicit resolution works without external secrets.
os.environ.setdefault("AGY_API_KEY", "local-agy")

try:
    from providers import register_provider
    from providers.base import ProviderProfile
except ImportError:
    register_provider = None

    class ProviderProfile:  # type: ignore[no-redef]
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

        def fetch_models(self, **kwargs: Any) -> Optional[List[str]]:
            return None


class AntigravityProfile(ProviderProfile):
    """Google Antigravity provider profile backed by local agy CLI bridge."""

    def prepare_messages(self, messages: List[dict[str, Any]]) -> List[dict[str, Any]]:
        """Ensure bridge is running before messages are sent to the local endpoint."""
        try:
            ensure_bridge_running(wait=True)
        except Exception as exc:
            logger.debug("Failed to auto-start bridge in prepare_messages: %s", exc)
        return super().prepare_messages(messages) if hasattr(super(), "prepare_messages") else messages

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        **context: Any,
    ) -> Tuple[dict[str, Any], dict[str, Any]]:
        """Forward reasoning effort configuration to the bridge."""
        try:
            ensure_bridge_running(wait=True)
        except Exception as exc:
            logger.debug("Failed to auto-start bridge in build_api_kwargs_extras: %s", exc)

        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}
        if hasattr(super(), "build_api_kwargs_extras"):
            extra_body, top_level = super().build_api_kwargs_extras(
                reasoning_config=reasoning_config, **context
            )

        # Forward Hermes' Low/Medium/High selection to the bridge
        if isinstance(reasoning_config, dict) and reasoning_config.get("enabled") is not False:
            requested = str(reasoning_config.get("effort") or "").strip().lower()
            effort_map = {
                "minimal": "low",
                "low": "low",
                "medium": "medium",
                "med": "medium",
                "high": "high",
                "xhigh": "high",
                "max": "high",
            }
            if requested in effort_map:
                top_level["reasoning_effort"] = effort_map[requested]
        return extra_body, top_level

    def supported_reasoning_efforts(self, model: str | None) -> Tuple[str, ...] | None:
        return ("low", "medium", "high")

    def fetch_models(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = 8.0,
    ) -> Optional[List[str]]:
        """Ensure bridge is running and fetch dynamic models from local bridge."""
        try:
            ensure_bridge_running(wait=True)
        except Exception as exc:
            logger.debug("Failed to auto-start bridge in fetch_models: %s", exc)

        if hasattr(super(), "fetch_models"):
            models = super().fetch_models(
                api_key=api_key or "local-agy",
                base_url=base_url or getattr(self, "base_url", "http://127.0.0.1:8765/v1"),
                timeout=timeout,
            )
            if models:
                return models

        # Direct query fallback
        import json
        import urllib.request
        target_url = (base_url or getattr(self, "base_url", "http://127.0.0.1:8765/v1")).rstrip("/") + "/models"
        try:
            req = urllib.request.Request(target_url, headers={"User-Agent": "hermes-antigravity-plugin"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    items = data.get("data", [])
                    return [item["id"] for item in items if "id" in item]
        except Exception as exc:
            logger.warning("Could not fetch models from %s: %s", target_url, exc)

        return [
            "gemini-3.8-flash",
            "gemini-3.7-flash",
            "gemini-3.1-pro",
            "claude-sonnet-4.6",
        ]


antigravity = AntigravityProfile(
    name="antigravity",
    aliases=("agy", "google-antigravity", "google-agy"),
    display_name="Google Antigravity",
    description="Local Google Antigravity bridge — Dynamic models directly from Google Antigravity",
    signup_url="https://antigravity.google",
    env_vars=("AGY_API_KEY",),
    base_url="http://127.0.0.1:8765/v1",
    api_mode="chat_completions",
    default_headers={"Authorization": "Bearer local-agy"},
    default_aux_model="gemini-3.8-flash",
    fallback_models=(
        "gemini-3.8-flash",
        "gemini-3.7-flash",
        "gemini-3.1-pro",
        "claude-sonnet-4.6",
    ),
)

if register_provider is not None:
    register_provider(antigravity)


# Intelligent deferred auto-start:
# When Hermes boots or restarts after an update, ensure the bridge wakes up
# cleanly once any in-flight updates are complete.
def _deferred_start_when_clear() -> None:
    import threading
    import time

    def _worker() -> None:
        time.sleep(2.0)
        for _ in range(120):
            if not is_update_in_progress():
                ensure_bridge_running(wait=False)
                break
            time.sleep(1.0)

    t = threading.Thread(target=_worker, daemon=True, name="agy-update-clearance-watcher")
    t.start()


try:
    _deferred_start_when_clear()
except Exception:
    pass
