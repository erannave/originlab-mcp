# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A standalone **OriginLab Origin app** that runs an **MCP (Model Context Protocol) server**
as a background **sidecar** while Origin is running. AI clients connect over streamable-HTTP and
drive *this* Origin instance (LabTalk, worksheets, fits, readback) through `originpro` (COM).
It is **not** part of the Batalyse app — separate repo, separate OPX.

The folder is both the dev tree and the Origin install location:
`C:\Users\<user>\AppData\Local\OriginLab\Apps\OriginMCP`.

## Architecture: three processes, not one

```
Origin (Windows, embedded CPython)          External sidecar (Origin's bundled python.exe)
  launch.ogs [main]  --run -pyf-->  manage.py  --Popen-->  mcp_bootstrap.py --> origin_mcp_server.py
  (toggle the app)                  (embedded)             (detached, hidden)   (FastMCP tools)
                                       |                        |
                                  writes host.handshake     op.attach() (COM) binds back to THIS Origin
                                  + MCP_HOST_TOKEN$         verifies token, serves HTTP, self-stops
```

- **`manage.py`** runs inside Origin's **embedded** Python (`run -pyf`). So `os.getpid()` IS the
  host Origin PID. It writes `host.handshake` (host PID + a per-instance token planted in the
  LabTalk var `MCP_HOST_TOKEN$`), pip-installs deps into `vendor/` on first run, then
  `subprocess.Popen`s the sidecar with Origin's bundled `python.exe` and the right env. Action is
  read from `MCP_ACTION$` (start/stop/toggle/status/setup), default **toggle**.
- **`mcp_bootstrap.py`** is the **external** sidecar entry. It fixes up `sys.path`/DLL dirs for the
  vendored deps, `op.attach()`es over COM, verifies via the token that it bound to the *host*
  Origin (best-effort — `originpro` can't re-pick a ROT entry), then serves streamable-HTTP via `_serve_http()`.
  That runs uvicorn's `serve()` as a task and polls the stop conditions IN the event loop: it exits
  when the host Origin PID dies OR a `stop.request` flag appears, releasing COM on the main thread
  on the way out (see COM lifecycle).
- **`origin_mcp_server.py`** holds the FastMCP tools. Named NOT `server.py` (see gotchas). Bind
  host/port via `ORIGIN_MCP_HOST`/`ORIGIN_MCP_PORT`; code default is `127.0.0.1`.
- **`launch.ogs`** is the only entry from Origin: `[main]` toggles; `[start]`/`[stop]`/`[status]`/
  `[setup]`/`[autostart]` set `MCP_ACTION$` then run `manage.py`. The app manifest
  (`LaunchScript=launch.ogs`, the `AfterInstall`/`BeforeUninstall` hooks, AppEnable) is NOT stored
  in a `package.ini` here — it's entered in Code Builder's Generate dialog (see Building the OPX).

### COM lifecycle (critical)

`op.attach()` holds an `OriginExt.ApplicationSI()` COM reference → Origin shows "controlled by
another application" and refuses to close. `originpro` releases it via `op.detach()` →
`Exit(releaseonly=True)` (frees the connection WITHOUT closing Origin).

`op.detach()` is an out-of-process COM call **into** Origin, so it only completes while Origin's
**STA (main) thread is free to service it**. Two rules fall out of this — get either wrong and
the detach deadlocks, Origin gets force-killed, and stays "controlled":

1. **`manage.py stop()` must NOT block.** It runs on Origin's STA thread (via `run -pyf`). If it
   sleep-waits for the sidecar to die, it parks the exact thread that must service the detach.
   So `stop()` just writes `stop.request` and returns immediately; the sidecar detaches and exits
   on its own (~1s). A genuinely stuck sidecar is hard-killed on the *next* stop click via a
   `stop.request` mtime age check (>6s). (The old design sleep-waited 8s then `TerminateProcess`d —
   that was the deadlock.)
2. **The sidecar releases COM on its own main thread, then hard-exits.** `_serve_sse()` detects the
   stop in-loop, stops driving the event loop (it does NOT await uvicorn's stream teardown,
   which hangs), calls `op.detach()` on the main thread (the STA apartment that owns the reference),
   then `os._exit()`. `asyncio.run()` is avoided on purpose — its teardown blocks on the cancelled
   in-flight tasks.

## Commands

```bash
# Lint/compile (no build system; Origin compiles nothing here — it's Python + LabTalk)
python3 -m py_compile manage.py mcp_bootstrap.py origin_mcp_server.py

# Legacy standalone run (client-spawned; bypasses the Origin-managed sidecar)
python origin_mcp_server.py stdio        # or: ... sse
```

Inside Origin (Script Window) — drives the real lifecycle:
```
run -pyf "C:\Users\<user>\AppData\Local\OriginLab\Apps\OriginMCP\manage.py";   # = toggle
```
Or click the **OriginMCP** app icon (toggle). Deps install into `vendor/` on first start, or via
the `[setup]` section. Runtime artifacts (`server.lock`, `host.handshake`, `*.log`,
`stop.request`) and `vendor/` are gitignored.

Reachability check from WSL:
```bash
curl -s -X POST http://host.docker.internal:8000/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'   # expect: a JSON result listing the tools
```

## Building the OPX

Packaging goes through **Code Builder**: right-click the `OriginMCP` app folder in the Workspace
window → **Generate…** → Package Manager → `File ▸ Save` → `OriginMCP.opx`.

- **There is intentionally NO `package.ini` in the folder.** Code Builder's Generate IGNORES
  `package.ini`'s `[Files] SourcePath`, so a `package.ini` present here makes Origin bundle the
  ENTIRE tree — `vendor/` (~112MB) + `.git` (~32MB) — producing a ~93MB OPX and hanging Origin
  (you must force-close). Without it, Generate uses Code Builder's explicit file list
  (`..\CBPackages.ini`).
- **Ship exactly these files** (the Generate file list): `launch.ogs`, `manage.py`,
  `mcp_bootstrap.py`, `origin_mcp_server.py`, `AfterInstall.ogs`, `BeforeUninstall.ogs`,
  `AppIcon.png`, `icon.svg`, `requirements.txt`, `README.md`. NOT `vendor/`, `.git`,
  `__pycache__`, logs or runtime artifacts. `vendor/` is rebuilt on install by `AfterInstall.ogs`.

**Manifest fields to enter in the Generate dialog.** These used to live in `package.ini`; with it
removed, set/verify them in the dialog the first time you package (Code Builder remembers them for
subsequent Generates):

| Field                | Value |
|----------------------|-------|
| Name                 | `OriginMCP` — no space, so installs match the dev folder |
| Description          | `Runs an MCP (Model Context Protocol) server as a background sidecar so AI clients can drive this Origin instance.` |
| Version / Author     | `1` / `Batalyse GmbH` |
| Origin version / Pro | `10.10` / No |
| Icon                 | `AppIcon.png` |
| Launch Script        | `launch.ogs` |
| Enable by Window     | Always + Graph, Workbook, Matrixbook, Image, Excel, Layout |
| **After Install**    | `run.section("%@A%@X\AfterInstall.ogs", main);` — pip-installs `vendor/` deps |
| **Before Uninstall** | `run.section("%@A%@X\BeforeUninstall.ogs", main);` — stops the sidecar |
| Before Install       | (empty) |

⚠️ If the **After Install** / **Before Uninstall** hooks are missing, the installed app won't
install its Python dependencies (the server never starts) and won't release/stop the sidecar on
uninstall. Verify both are present every time you re-package.

> Naming note: keep `Name=OriginMCP` (no space). A `Name` with a space derives a different install
> folder, which previously produced a duplicate "Origin MCP" folder. Also avoid creating a second
> "New App" with the same `Name` — it collides in Origin's `OPXList.xml` registry.

## Gotchas (hard-won; don't relearn these)

- **`originpro` in Origin's `PyPackage` is embedded-only.** Externally `import originpro` needs
  `OriginExt` (COM fallback) + `comtypes`, which aren't shipped. `manage.py setup()` pip-installs
  `OriginExt comtypes mcp fastmcp uvicorn starlette` into `vendor/`.
- **pip `--target vendor` does NOT run pywin32's post-install**, so `import pywintypes` (pulled in
  by `mcp`) fails. `mcp_bootstrap.py` manually adds `vendor/win32`, `win32/lib`, `win32com`,
  `win32comext`, `Pythonwin` to `sys.path` and `os.add_dll_directory(vendor/pywin32_system32)`.
- **pip `--target vendor` needs `--ignore-installed`.** `setup()` runs pip with PYTHONPATH exposing
  the host Origin's `ProgramData\OriginLab\<ver>\PyPackage\Py3`; without the flag, pip treats
  packages found there (e.g. `idna`, `attrs` installed by other Apps) as "already satisfied" and
  skips vendoring them. The sidecar then works under that Origin version but crashes with
  `ModuleNotFoundError` under a freshly installed one (this broke Origin 2026b while 2026 worked —
  its new `103b\PyPackage\Py3` was empty). `DEP_MARKERS` now checks the known leak-prone names.
- **Never name the server module `server.py`** — `vendor/win32com` exposes a top-level `server`
  package that shadows it. Hence `origin_mcp_server.py`.
- **`vendor/` is untracked/gitignored** (platform/version-specific, ~7800 files; auto-installed).
- **Building the OPX** has its own section above. The trap: a `package.ini` in the folder makes
  Code Builder's Generate ignore the file list and bundle `vendor/`+`.git` (~93MB → Origin hangs).
  No `package.ini` here; the manifest (incl. the `AfterInstall`/`BeforeUninstall` hooks) is entered
  in the Generate dialog.
- **`manage.py stop()` must never block Origin's STA thread** (no sleep-wait) — otherwise the
  sidecar's `op.detach()` COM call can't be serviced and deadlocks → force-kill → Origin stuck
  "controlled". See COM lifecycle.
- **WSL2 clients**: mirrored networking breaks Docker Desktop port publishing here — use **NAT**
  (the default). The sidecar binds `0.0.0.0` (set via `ORIGIN_MCP_HOST` in `manage.py`'s child env;
  not LAN-exposed — the standard Windows Firewall blocks physical-NIC inbound by default). The WSL
  client uses `http://host.docker.internal:8000/mcp`, allowed by a firewall rule scoped to the WSL
  range `172.16.0.0/12`.
- **`Python.<func>()` / embedded-python dispatch caches modules** — restart Origin after editing
  `manage.py`/`mcp_bootstrap.py` so changes load. The sidecar reads its `.py` fresh on each spawn,
  so a stop+start (toggle twice) picks up `mcp_bootstrap.py`/`origin_mcp_server.py` edits.
- **The transport MUST stay stateless, or every Origin restart strands the client.** Symptom:
  restart Origin, restart the MCP server from the app icon, and the client still can't reach it —
  tools fail with `MCP error -32602: Received request before initialization was complete` until the
  user manually reconnects (`/mcp` → reconnect in Claude Code). Cause: a session-bearing transport
  (legacy SSE, or streamable-HTTP in session mode) gives each connection a `ServerSession` born
  `NotInitialized` (`mcp/server/session.py:98`), and any request before the `initialize` handshake
  raises there (`session.py:204`). The sidecar dies with its host Origin, and **Claude Code
  transparently re-opens the dropped transport but does NOT replay `initialize`** — so every later
  call lands on a fresh, uninitialized session. Verified empirically against Claude Code 2.1.221
  with a probe server that restarts mid-session: session-bearing transport → `-32602` on the call
  after the restart; `stateless_http=True` → the call after the restart just succeeds against the
  new process, no reconnect. Statelessness is the fix because the SDK stamps a stateless session
  `Initialized` at construction. Do not "simplify" `origin_mcp_server.py` back to a plain
  `FastMCP(...)` or `_serve_http()` back to `mcp.sse_app()`.
