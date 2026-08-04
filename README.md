# Origin MCP

An OriginLab Origin app that lets **AI clients drive your running Origin instance**.

Install the app, click it once, and Origin starts a small background server (an
[MCP](https://modelcontextprotocol.io) server). Point any MCP-capable AI client
at it and the AI can now work inside *this* Origin: run LabTalk, read and write
worksheets, run fits, create projects, and read results back.

> This is a standalone app — it is **not** part of Batalyse.

## What the AI can do

Once connected, the client has tools to:

- **Run LabTalk** — any script, with or without reading values back (`run_labtalk`, `run_labtalk_with_readback`)
- **Read/write worksheet data** (`get_worksheet_data`, `set_worksheet_data`, `add_worksheet`)
- **Read/write LabTalk variables and the project tree** (`get_labtalk_value`, `set_labtalk_var`, `get_labtalk_tree`)
- **Manage projects** (`create_project`, `save_project`)

## Quick start

1. **Install** the `OriginMCP.opx` (double-click it, or drag it onto Origin). On
   install, Origin downloads the server's Python dependencies in the background —
   the first install takes a minute.
2. **Click the "Origin MCP" app** in the Apps Gallery to start the server. A
   message box confirms it's running. Click again to stop. The server listens on
   `http://127.0.0.1:8000/mcp`.
3. **Point your MCP client at the URL** (see below) and have it call
   `connect_origin`.

That's it — the server runs in the background while Origin is open and shuts
itself down when Origin closes.

## Connect your AI client

The app starts the server for you, so the client just connects to the URL:

```json
{
  "mcpServers": {
    "origin-mcp": { "type": "http", "url": "http://localhost:8000/mcp" }
  }
}
```

### Auto-start at Origin launch (optional)

Clicking the app starts the server. To start it automatically every time Origin
launches, add this to your Origin startup script:

```
run.section("%@A%@X\launch.ogs", autostart);
```

It's safe to call repeatedly — it does nothing if the server is already running.

### Connecting from WSL

A WSL2 client can't reach Windows `localhost`, so the app binds the server in a
way WSL can reach (it is **not** exposed to your LAN — Windows Firewall still
blocks inbound on your physical network adapters).

1. Connect to `http://host.docker.internal:8000/mcp` (transport: `http`).
2. Allow the WSL subnet to reach the port. In an **elevated PowerShell**:
   ```powershell
   New-NetFirewallRule -DisplayName "Origin MCP (WSL->host 8000)" -Direction Inbound `
     -Action Allow -Protocol TCP -LocalPort 8000 -RemoteAddress 172.16.0.0/12
   ```
   (`172.16.0.0/12` is the private WSL/Hyper-V range — **not** your Wi-Fi/LAN.)

> ⚠️ The server runs arbitrary LabTalk, so keep the firewall scope tight.

## How it works

Clicking the app runs a small lifecycle manager inside Origin's embedded Python,
which launches a separate, hidden **sidecar** process for the actual server. The
sidecar attaches back to *this* Origin over COM and serves the MCP tools:

```
Origin (this instance)
  │  click "Origin MCP"  →  launch.ogs  →  manage.py   (start / stop / toggle)
  ▼
manage.py  ── launches (hidden) ──►  sidecar: mcp_bootstrap.py → origin_mcp_server.py
  │  records host PID + a token             │  attaches back to THIS Origin (COM)
  ▼                                         ▼
                              AI client ──► http://127.0.0.1:8000/mcp
```

The sidecar runs off Origin's UI thread, so the server never freezes Origin, and
it exits on its own when Origin closes — no orphaned processes left behind.

Implementation details (the three-process design, COM lifecycle, and packaging)
live in [`CLAUDE.md`](./CLAUDE.md).

## Files

| File | Role |
|------|------|
| `launch.ogs` | App entry: click = toggle; `[start]`/`[stop]`/`[status]`/`[setup]`/`[autostart]` |
| `manage.py` | Lifecycle manager (runs in Origin's embedded Python) |
| `mcp_bootstrap.py` | Sidecar entry: attaches to the right Origin, then runs the server |
| `origin_mcp_server.py` | The MCP server and its tools |
| `AfterInstall.ogs` | Installs the server's Python dependencies on install |
| `BeforeUninstall.ogs` | Stops the sidecar before the app is removed |

## Notes

- **`AppIcon.png` is a placeholder** — replace with Origin MCP branding before
  public distribution.
- **Single instance:** the OS port `:8000` acts as the lock. A second Origin
  instance that tries to start a server will fail to bind and exit quietly; the
  first instance keeps serving.
