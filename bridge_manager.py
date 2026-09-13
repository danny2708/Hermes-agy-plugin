"""Manager for the local Google Antigravity bridge.

Provides fail-safe auto-start, health-checks, and lifecycle control so Hermes
is never frozen by Connection Errors when calling Antigravity models.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hermes.plugins.antigravity.manager")

BRIDGE_HEALTH_URL = "http://127.0.0.1:8765/health"
BRIDGE_PORT = 8765

_SPAWN_LOCK_FILE = os.path.join(tempfile.gettempdir(), "agy_bridge_spawn.lock")


def get_hermes_home_dir() -> str:
    """Return the active HERMES_HOME directory dynamically."""
    if os.environ.get("HERMES_HOME"):
        return os.path.normpath(os.environ["HERMES_HOME"])
    try:
        from hermes_constants import get_hermes_home
        return str(get_hermes_home())
    except Exception:
        # Fallback to ~/.hermes
        return os.path.normpath(os.path.expanduser("~/.hermes"))


def get_log_dir() -> str:
    """Return the directory where bridge logs should be written."""
    d = os.path.join(get_hermes_home_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    return d


def is_update_in_progress() -> bool:
    """Check if a Hermes Desktop / Agent update is currently in progress.

    Returns True if an update marker exists or an update flag is set.
    While True, bridge auto-spawning is completely suspended so Hermes can
    safely replace its binaries and dependencies without file locks.
    """
    hermes_home = get_hermes_home_dir()
    marker_main = os.path.join(hermes_home, ".hermes-update-in-progress")
    marker_temp = os.path.join(tempfile.gettempdir(), ".hermes-update-in-progress")
    flag_disabled = os.path.join(tempfile.gettempdir(), "agy_bridge_disabled.flag")

    return (
        os.path.isfile(marker_main)
        or os.path.isfile(marker_temp)
        or os.path.isfile(flag_disabled)
    )


def find_safe_python_bin() -> str:
    """Find a Python binary OUTSIDE hermes-agent venv.

    Running the bridge under an external/system Python ensures:
    1. The project venv is NEVER locked on Windows.
    2. hermes_cli._scan_venv_blockers will never classify agy_bridge as a venv blocker.
    3. hermes update can rebuild the venv without file lock errors.
    """
    hermes_home = get_hermes_home_dir()
    venv_dir = os.path.normpath(os.path.join(hermes_home, "hermes-agent", "venv")).lower()

    if sys.platform == "win32":
        # Check standard user Python installations
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        if local_app_data:
            programs_py = os.path.join(local_app_data, "Programs", "Python")
            if os.path.isdir(programs_py):
                try:
                    for entry in sorted(os.listdir(programs_py), reverse=True):
                        p_dir = os.path.join(programs_py, entry)
                        pw = os.path.join(p_dir, "pythonw.exe")
                        if os.path.isfile(pw):
                            return pw
                        p = os.path.join(p_dir, "python.exe")
                        if os.path.isfile(p):
                            return p
                except Exception:
                    pass

        # Check system PATH for pythonw or python outside venv
        for entry in os.environ.get("PATH", "").split(os.pathsep):
            if not entry.strip():
                continue
            entry_norm = os.path.normpath(entry).lower()
            if venv_dir in entry_norm:
                continue
            cand_pw = os.path.join(entry, "pythonw.exe")
            if os.path.isfile(cand_pw):
                return cand_pw
            cand_p = os.path.join(entry, "python.exe")
            if os.path.isfile(cand_p):
                return cand_p

    # Fallback to sys.executable
    return sys.executable


def is_bridge_healthy(timeout: float = 0.4) -> bool:
    """Return True if the local agy_bridge is responding on port 8765."""
    try:
        req = urllib.request.Request(
            BRIDGE_HEALTH_URL,
            headers={"User-Agent": "hermes-bridge-probe"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def ensure_bridge_running(wait: bool = True, max_wait: float = 12.0) -> bool:
    """Ensure the agy_bridge is running; if not, spawn it silently in background.

    Intelligently parks and refuses to spawn if an update is in progress,
    and automatically resumes once Hermes has finished updating and running.
    """
    if is_update_in_progress():
        logger.info("Hermes update is in progress. Bridge auto-start is paused.")
        return False

    if is_bridge_healthy(timeout=0.3):
        return True

    bridge_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agy_bridge.py")
    if not os.path.isfile(bridge_script):
        logger.warning("agy_bridge.py not found at %s", bridge_script)
        return False

    python_bin = find_safe_python_bin()
    log_path = os.path.join(get_log_dir(), "agy_bridge.log")

    flags = 0
    flags_fallback = 0
    if sys.platform == "win32":
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        CREATE_BREAKAWAY_FROM_JOB = 0x01000000
        DETACHED_PROCESS = 0x00000008
        flags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | CREATE_BREAKAWAY_FROM_JOB
        flags_fallback = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW | DETACHED_PROCESS

    # Guard against duplicate concurrent spawns
    recent_spawn = False
    try:
        if os.path.isfile(_SPAWN_LOCK_FILE):
            mtime = os.path.getmtime(_SPAWN_LOCK_FILE)
            if time.time() - mtime < 6.0:
                recent_spawn = True
    except Exception:
        pass

    if not recent_spawn:
        try:
            with open(_SPAWN_LOCK_FILE, "w", encoding="utf-8") as f:
                f.write(str(os.getpid()))
        except Exception:
            pass

        try:
            log_file = open(log_path, "a", encoding="utf-8")
            env = os.environ.copy()
            env["HERMES_HOME"] = get_hermes_home_dir()

            try:
                subprocess.Popen(
                    [python_bin, bridge_script],
                    creationflags=flags,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=env,
                )
            except OSError:
                subprocess.Popen(
                    [python_bin, bridge_script],
                    creationflags=flags_fallback,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    env=env,
                )
            logger.info("Auto-started Antigravity Bridge using %s", python_bin)
        except Exception as exc:
            logger.error("Failed to auto-start Antigravity Bridge: %s", exc)
            return False

    if not wait:
        return True

    start = time.time()
    while time.time() - start < max_wait:
        if is_bridge_healthy(timeout=0.3):
            logger.info("Antigravity Bridge is healthy and ready")
            return True
        time.sleep(0.3)

    logger.warning("Antigravity Bridge did not become healthy within %s seconds", max_wait)
    return False
