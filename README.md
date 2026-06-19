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

### WSL clients

The server binds to **127.0.0.1** on purpose — it runs arbitrary LabTalk, so it
must not be exposed to the LAN. A client inside **WSL2** cannot reach the Windows
loopback under default NAT networking. Use **mirrored networking**, which shares
the Windows loopback with WSL:

1. In `C:\Users\<you>\.wslconfig`, under `[wsl2]`, add: `networkingMode=mirrored`
2. From a **Windows** terminal (not inside WSL): `wsl --shutdown`, then reopen WSL.
3. The client connects to `http://localhost:8000/sse` — no LAN exposure, no
   firewall rule.

(`ORIGIN_MCP_HOST` / `ORIGIN_MCP_PORT` can override the bind, but only do so with
a firewall rule scoped to a trusted subnet — the server is effectively RCE.)

## Notes / TODO

- **`AppIcon.png` is a placeholder** copied from Batalyse — replace with MCP
  branding before distribution.
- **`package.ini` `ID=9001`** is a placeholder — obtain a real App ID from
  OriginLab for public distribution.
- Single-instance: the OS port `:8000` is the lock. A second Origin instance
  that tries to start a sidecar will fail to bind and self-exit; the first
  instance keeps serving.
