# Hermes Google Antigravity Provider Plugin

A native model-provider plugin (`kind: model-provider`) for **[Hermes Agent](https://github.com/nousresearch/hermes-agent)** and **Hermes Desktop**, enabling seamless local inference routing through **Google Antigravity** (`agy` CLI).

---

## Key Features

- **100% Dynamic Model Discovery:**
  - Queries `agy models` at runtime to automatically discover and register all available models (Gemini 3.8 Flash, Gemini 3.7 Flash, Gemini 3.1 Pro, Claude Sonnet 4.6, Claude Opus 4.6, GPT-OSS 120B, etc.).
  - Zero code modifications required when Google releases or updates models.
- **Real-Time Chain-of-Thought (CoT) Streaming:**
  - Intercepts `<think>...</think>` reasoning tokens on the fly and streams them directly into Hermes under `reasoning_content`.
  - Supports reasoning effort mapping (`minimal`, `low`, `medium`, `high`).
- **Native Tool Calling:**
  - Translates model `<tool_call>` outputs into standard OpenAI `tool_calls` chunks in real-time.
  - Full compatibility with Hermes' built-in tools (terminal execution, file manipulation, web search, MCP servers, and custom tools).
- **Dedicated Tool Execution Logging (`logs/agy_tools.log`):**
  - **`[TOOL CALL DISPATCHED]`**: Captures tool name, call ID, and pretty-printed JSON parameters.
  - **`[TOOL RESULT RECEIVED]`**: Captures tool name, matching call ID, execution latency, status (`SUCCESS / OUTPUT` vs `FAILED / ERROR`), character size, and formatted result payload.
  - Multi-turn fingerprint deduplication prevents spamming redundant conversation history.
- **Intelligent Lifecycle & Anti-Lock Architecture:**
  - **Isolated Python Runtime**: Automatically locates an external, system-level Python interpreter outside the `hermes-agent\venv` environment, preventing Windows file-lock errors during Hermes self-updates.
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

### Monitor Tool Calling Activity (Recommended):
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

## Repository Structure

```text
Hermes-agy-plugin/
├── plugin.yaml          # Hermes native provider manifest (kind: model-provider)
├── __init__.py          # AntigravityProfile implementation & auto-registration
├── agy_bridge.py        # Local OpenAI-compatible HTTP server & streaming engine
├── bridge_manager.py    # Process supervisor, external Python selector, health check & watchdog
├── install.bat          # 1-click Windows batch launcher
├── install.ps1          # Automated PowerShell installation script
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
                           ▼
┌────────────────────────────────────────────────────────┐
│           Antigravity Local HTTP Bridge                │
│                 (127.0.0.1:8765/v1)                    │
│                                                        │
│  • SSE Stream Filter & Heartbeat Engine                │
│  • <think>...</think> -> reasoning_content extraction  │
│  • <tool_call>...</tool_call> -> OpenAI Tool Chunks    │
│  • Dedicated Tool Logger (logs/agy_tools.log)          │
└──────────────────────────┬─────────────────────────────┘
                           │ CLI Subprocess Invocation
                           ▼
┌────────────────────────────────────────────────────────┐
│             Google Antigravity CLI (agy)               │
│         Gemini 3.8 / 3.7 / Claude Sonnet 4.6 / ...     │
└────────────────────────────────────────────────────────┘
```

1. **Lazy Initialization:** When Hermes makes its first inference request, `__init__.py` invokes `bridge_manager.ensure_bridge_running()`.
2. **Independent Spawning:** The bridge is spawned silently in the background using a system-level Python interpreter to keep `hermes-agent\venv` completely unlocked.
3. **Translation Layer:** `agy_bridge.py` converts Hermes' chat history into Antigravity CLI arguments, runs `agy`, and streams standard OpenAI Server-Sent Events (SSE) back to Hermes.
4. **Tool Parsing & Logging:** Tool declarations and invocations are transformed transparently between XML markup and OpenAI JSON tool calls, while logging every dispatch and result for easy debugging.

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
  Expected response: `{"status": "ok", "service": "agy_bridge"}`.

---

## License

This project is released under the [MIT License](LICENSE).
