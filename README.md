# Hermes Google Antigravity Provider Plugin

[![CI](https://github.com/danny2708/Hermes-agy-plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/danny2708/Hermes-agy-plugin/actions/workflows/ci.yml)

A native model-provider plugin (`kind: model-provider`) for **[Hermes Agent](https://github.com/nousresearch/hermes-agent)** and **Hermes Desktop**, enabling local inference routing through **Google Antigravity** (`agy` CLI).

---

## Key Features

- **Dynamic Model Discovery (via official `agy models` CLI):**
  - Directly queries `agy models` to discover and register all active models (Gemini 3.8 Flash, Gemini 3.7 Flash, Gemini 3.1 Pro, Claude Sonnet 4.6, Claude Opus 4.6, GPT-OSS 120B, etc.).
  - Includes a fallback catalog to guarantee uninterrupted service if the CLI query is delayed or offline.
- **Real-Time Chain-of-Thought (CoT) Streaming:**
  - Streams `<think>...</think>` reasoning tokens directly into Hermes under `reasoning_content`.
  - Maps reasoning effort dials (`minimal`, `low`, `medium`, `high`) to corresponding model variants.
- **Hermes-Compatible Streamed Tool Calling:**
  - Translates model `<tool_call>` outputs into standard OpenAI `tool_calls` chunks in real-time.
  - Full compatibility with Hermes' built-in tools (terminal execution, file manipulation, web search, MCP servers, and custom tools).
- **Hardened Security Architecture:**
  - **No dangerous overrides**: Does **NOT** pass `--dangerously-skip-permissions`. In `stream-json` mode, `permission_mode` defaults to `request-review`. Antigravity native tool execution is constrained by AGY's request-review policy and terminal sandbox, while actionable host operations are intended to flow through Hermes tools.
  - **Terminal sandboxing**: Runs `agy` with `--sandbox` and `--disable-slash-commands` to prevent unintended command expansion.
  - **Local Bearer Token Auth**: Protects the HTTP bridge (`127.0.0.1:8765`) using an ephemeral local token (`agy-bridge_token.secret`), requiring exact token matching and rejecting unauthorized local processes.
  - **Strict Origin & Loopback CORS**: Restricts cross-origin requests to local loopback (`localhost`, `127.0.0.1`) and desktop schemes (`app://`, `vscode-webview://`), rejecting arbitrary browser origins with `403 Forbidden` to prevent browser-based CSRF/drive-by attacks.
- **Privacy-Aware Tool Logging (`logs/agy_tools.log`):**
  - **`[TOOL CALL DISPATCHED]`**: Tool name, call ID, formatted payload.
  - **`[TOOL RESULT RECEIVED]`**: Tool name, call ID, execution latency, status (`SUCCESS / OUTPUT` vs `FAILED / ERROR`), character size, and formatted result payload.
  - **Automatic Credential Redaction**: Automatically masks Bearer tokens, OpenAI/Google API keys, passwords, and PEM private keys before writing to disk.
  - **Privacy Dials**: Configure logging behavior via environment variables:
    - `AGY_LOG_TOOL_ARGS=false` (disables logging tool call arguments)
    - `AGY_LOG_TOOL_RESULTS=false` (disables logging tool output content)
    - `AGY_LOG_MAX_PAYLOAD=4000` (caps individual payload size)
  - **Automatic Log Rotation**: Rotates log files automatically at 5MB with 3 backups (`RotatingFileHandler`).
- **Intelligent Lifecycle & Anti-Lock Architecture:**
  - **Isolated Python Runtime**: Automatically discovers an external, system-level Python interpreter outside `hermes-agent\venv`, preventing Windows file-lock errors during Hermes self-updates.
  - **Update Watchdog**: Automatically suspends bridge spawning when an active update is detected, then safely resumes once the update completes.
  - **SSE Heartbeat Keep-Alive**: Prevents idle timeouts during long reasoning or complex multi-turn deliberation.
  - **Alias Backward-Compatibility**: Supports both `--provider antigravity` and `--provider agy`.

---

## Prerequisites

1. **Google Antigravity CLI (`agy`)**:
   - Ensure `agy` is installed and authenticated:
     ```bash
     agy auth login
     ```
   - Verify available models:
     ```bash
     agy models
     ```
2. **Hermes Agent**:
   - Hermes Agent (v0.20+ or v0.21+) or Hermes Desktop installed on your system.

---

## Installation

### Method 1: 1-Click Installer (Recommended for Windows)

Double-click the installer script in the repository root:
```text
install.bat
```
The installer will:
1. Verify `agy` CLI availability.
2. Auto-detect your `$HERMES_HOME` directory (or `%USERPROFILE%\.hermes`).
3. Deploy the plugin files to `$HERMES_HOME/plugins/model-providers/antigravity`.

### Method 2: PowerShell Installation

Run from PowerShell:
```powershell
.\install.ps1
```

If Hermes is installed in a custom location, specify the path:
```powershell
.\install.ps1 -CustomHermesHome "C:\Path\To\Hermes"
```

### Method 3: Manual Installation

Copy this directory directly into your Hermes plugins folder:
```text
$HERMES_HOME/plugins/model-providers/antigravity/
```
*(On Windows, `$HERMES_HOME` typically defaults to `%USERPROFILE%\.hermes` or your project root).*

---

## Usage

### 1. Hermes CLI

List available providers and models:
```bash
hermes model
```

Select `Google Antigravity` as the provider, or launch directly via command line:
```bash
hermes --provider antigravity --model gemini-3.8-flash
```

Using the legacy alias:
```bash
hermes --provider agy --model claude-sonnet-4.6
```

### 2. Hermes Desktop GUI

1. Launch **Hermes Desktop**.
2. Navigate to **Settings** > **Model / Provider**.
3. Select **Google Antigravity** from the provider dropdown.
4. Choose any desired model (e.g., `Gemini 3.8 Flash (High)` or `Claude Sonnet 4.6 (Thinking)`).

---

## Real-Time Log Monitoring

The bridge maintains clean, separated logs inside your `$HERMES_HOME/logs/` directory.

### Monitor Tool Calling Activity:
View real-time tool calls, payloads, latencies, and execution outputs:
```powershell
Get-Content -Path "$env:HERMES_HOME\logs\agy_tools.log" -Wait -Tail 30
```

Sample log output:
```text
================================================================================
[TOOL CALL DISPATCHED]
  Tool Name : terminal
  Call ID   : call_03e9a02a
  Payload / Arguments:
{
  "command": "dir"
}
================================================================================

================================================================================
[TOOL RESULT RECEIVED]
  Tool Name : terminal
  Call ID   : call_03e9a02a (execution time: 0.12s)
  Status    : SUCCESS / OUTPUT
  Size      : 128 chars
  Result Content:
README.md
package.json
src/
node_modules/
================================================================================
```

### Monitor General Bridge Logs:
```powershell
Get-Content -Path "$env:HERMES_HOME\logs\agy_bridge.log" -Wait -Tail 30
```

---

## Testing

Run the included unit test suite:
```bash
python -m unittest discover -s tests -p "test_*.py"
```
The test suite covers:
- `StreamToolCallFilter` state machine and token chunk fragmentation.
- Credential and token redaction (`redact_sensitive_content`).
- Model catalog indexing and effort resolution (`agy models` format).
- Local Bearer token authorization checks.

---

## Repository Structure

```text
Hermes-agy-plugin/
├── plugin.yaml          # Hermes native provider manifest (kind: model-provider)
├── __init__.py          # AntigravityProfile implementation & auto-registration
├── agy_bridge.py        # Local OpenAI-compatible HTTP server & streaming engine
├── bridge_manager.py    # Process supervisor, external Python selector, health check & watchdog
├── install.bat          # 1-click Windows batch launcher
├── install.ps1          # Automated PowerShell installation script
├── tests/               # Automated unit test suite
│   ├── test_stream_filter.py
│   ├── test_redaction.py
│   ├── test_model_catalog.py
│   └── test_auth.py
└── README.md            # Documentation and usage guide
```

---

## How It Works

```
┌────────────────────────────────────────────────────────┐
│                      Hermes Agent                      │
│             (CLI / Desktop / Web UI / MCP)             │
└──────────────────────────┬─────────────────────────────┘
                           │ OpenAI Chat Completions API
                           │ (Bearer Token Authenticated)
                           ▼
┌────────────────────────────────────────────────────────┐
│           Antigravity Local HTTP Bridge                │
│                 (127.0.0.1:8765/v1)                    │
│                                                        │
│  • SSE Stream Filter & Heartbeat Engine                │
│  • <think>...</think> -> reasoning_content extraction  │
│  • <tool_call>...</tool_call> -> OpenAI Tool Chunks    │
│  • Privacy Redactor & Rotating Logger (agy_tools.log)  │
└──────────────────────────┬─────────────────────────────┘
                           │ Sandboxed CLI Invocations
                           │ (--input-format stream-json --sandbox)
                           ▼
┌────────────────────────────────────────────────────────┐
│             Google Antigravity CLI (agy)               │
│         Gemini 3.8 / 3.7 / Claude Sonnet 4.6 / ...     │
└────────────────────────────────────────────────────────┘
```

1. **Lazy Initialization:** When Hermes makes its first inference request, `__init__.py` invokes `bridge_manager.ensure_bridge_running()`.
2. **Independent Spawning:** The bridge is spawned silently in the background using a system-level Python interpreter to keep `hermes-agent\venv` completely unlocked.
3. **Translation Layer:** `agy_bridge.py` converts Hermes' chat history into Antigravity CLI arguments, runs `agy` in sandboxed `stream-json` mode, and streams standard OpenAI Server-Sent Events (SSE) back to Hermes.
4. **Tool Parsing & Logging:** Tool declarations and invocations are transformed transparently between XML markup and OpenAI JSON tool calls, while logging every dispatch and result with automatic credential redaction.

---

## Troubleshooting

- **`agy: command not found`:**
  Ensure the Google Antigravity CLI is installed and added to your system `PATH`. Run `agy --version` to verify.
- **Port Conflict (8765):**
  If port `8765` is occupied by another service, verify with `netstat -ano | findstr 8765` or configure a custom port in `agy_bridge.py` and `__init__.py`.
- **Verify Bridge Health:**
  Open a browser or run:
  ```powershell
  curl http://127.0.0.1:8765/health
  ```
  Expected response: `{"status": "ok", "service": "agy-bridge", "models_count": ...}`.

---

## License

This project is released under the [MIT License](LICENSE).
