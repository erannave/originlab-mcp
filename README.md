# Origin MCP

A standalone Origin app that runs an **MCP (Model Context Protocol) server** as a
background **sidecar** while Origin is running. AI clients connect to it over
HTTP/SSE and drive *this* Origin instance (run LabTalk, read/write worksheets,
fit, read back results) through `originpro` (COM).

This is a separate app — it is **not** part of Batalyse.

## How it works

```
Origin (this instance)
  │  click the "Origin MCP" app in the Apps Gallery  → launch.ogs [main]
  │  run -pyf manage.py   (embedded Python: toggle start/stop)
  ▼
manage.py  ── subprocess.Popen (hidden, detached) ──►  <exeDir>\64bit\PyDLLs\python.exe  mcp_bootstrap.py sse
  │  writes host.handshake (host PID + token),                 │  op.attach()  → binds back to THIS Origin via COM
  │  plants MCP_HOST_TOKEN$                                    │  verifies token, runs server.py (FastMCP, SSE)
  ▼                                                            ▼
server.lock (PID/port)                          AI client ──► http://127.0.0.1:8000/sse
```

The sidecar runs off Origin's UI thread, so the blocking SSE loop never freezes
Origin. COM marshals each tool call back onto Origin (one in-flight call at a
time — fine for an interactive MCP client). A watchdog in `mcp_bootstrap.py`
monitors the host Origin PID and exits the sidecar if Origin disappears, so no
orphan process is left behind.

## Files

| File | Role |
|------|------|
| `package.ini` | App metadata + install hooks |
| `launch.ogs` | `[main]` = toggle; `[start]`/`[stop]`/`[status]`/`[autostart]` |
| `manage.py` | Embedded-Python lifecycle manager (start/stop/toggle/status) |
| `mcp_bootstrap.py` | Sidecar entry: attach to the right Origin, watchdog, run server |
| `origin_mcp_server.py` | The MCP server (FastMCP tools); `python origin_mcp_server.py stdio` still works standalone. Named to avoid colliding with `win32com.server`. |
| `AfterInstall.ogs` | `pip -chk mcp fastmcp uvicorn starlette` into Origin's PyPackage |
| `BeforeUninstall.ogs` | Stops the sidecar before removal |

## Usage

1. Install the app in Origin (drag the OPX, or develop in-place in this folder).
   On install, `AfterInstall.ogs` pip-installs the deps into Origin's PyPackage.
2. **Click the "Origin MCP" app** in the Apps Gallery to start the server
   (click again to stop). A message box confirms the state. The server listens
   on `http://127.0.0.1:8000/sse`.
3. Point your MCP client at the URL transport (see below) and call
   `connect_origin`.

### Optional: auto-start at Origin launch

App `LaunchScript` only runs when the app is clicked, not at Origin startup. To
auto-start the sidecar, call the `[autostart]` section from an Origin startup
hook — e.g. add to your user startup script (`%Y` Startup) or an `OnStartup`
section:

```
run.section("%@A%@X\launch.ogs", autostart);
```

`[autostart]` is idempotent: `manage.py` no-ops if the server is already up.

## Client configuration

The server is started by Origin, so the client no longer spawns Python — it just
connects to the URL:

```json
{
  "mcpServers": {
    "origin-mcp": { "url": "http://localhost:8000/sse" }
  }
}
```

(The legacy `python origin_mcp_server.py stdio` spawn-mode still works for client-spawned
use, but then the client owns the process and Origin does not.)

### WSL clients (WSL2 NAT mode)

A WSL2 client can't reach the Windows **loopback**, so for WSL the app's
`manage.py` binds the sidecar to `0.0.0.0` (via `ORIGIN_MCP_HOST`). This is still
**not LAN-exposed**: the standard Windows Firewall blocks inbound on the physical
NICs by default — only the WSL virtual adapter needs to be allowed.

1. Client connects to `http://host.docker.internal:8000/sse` (the Windows host as
   seen from WSL2 NAT).
2. Allow the WSL subnet to reach the port — in an **elevated PowerShell**:
   ```powershell
   New-NetFirewallRule -DisplayName "Origin MCP (WSL->host 8000)" -Direction Inbound `
     -Action Allow -Protocol TCP -LocalPort 8000 -RemoteAddress 172.16.0.0/12
   ```
   (`172.16.0.0/12` is the WSL/Hyper-V private range — **not** your LAN/Wi-Fi.)

> Mirrored networking was tried but breaks Docker Desktop's port publishing, so
> NAT + this scoped rule is the supported path here.

(The code default bind stays `127.0.0.1`; `ORIGIN_MCP_HOST`/`ORIGIN_MCP_PORT`
override it. The server runs arbitrary LabTalk — keep the firewall scope tight.)

## Notes / TODO

- **`AppIcon.png` is a placeholder** copied from Batalyse — replace with MCP
  branding before distribution.
- **`package.ini` `ID=9001`** is a placeholder — obtain a real App ID from
  OriginLab for public distribution.
- Single-instance: the OS port `:8000` is the lock. A second Origin instance
  that tries to start a sidecar will fail to bind and self-exit; the first
  instance keeps serving.
