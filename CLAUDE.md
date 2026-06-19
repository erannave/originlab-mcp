# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A standalone **OriginLab Origin app** that runs an **MCP (Model Context Protocol) server**
as a background **sidecar** while Origin is running. AI clients connect over HTTP/SSE and
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
                                  + MCP_HOST_TOKEN$         verifies token, runs SSE server, watchdog
```

- **`manage.py`** runs inside Origin's **embedded** Python (`run -pyf`). So `os.getpid()` IS the
  host Origin PID. It writes `host.handshake` (host PID + a per-instance token planted in the
  LabTalk var `MCP_HOST_TOKEN$`), pip-installs deps into `vendor/` on first run, then
  `subprocess.Popen`s the sidecar with Origin's bundled `python.exe` and the right env. Action is
  read from `MCP_ACTION$` (start/stop/toggle/status/setup), default **toggle**.
- **`mcp_bootstrap.py`** is the **external** sidecar entry. It fixes up `sys.path`/DLL dirs for the
  vendored deps, `op.attach()`es over COM, verifies via the token that it bound to the *host*
  Origin (best-effort — `originpro` can't re-pick a ROT entry), runs a **watchdog** thread, then
  serves. The watchdog exits the sidecar when the host Origin PID dies OR a `stop.request` flag
  appears.
- **`origin_mcp_server.py`** holds the FastMCP tools. Named NOT `server.py` (see gotchas). Bind
  host/port via `ORIGIN_MCP_HOST`/`ORIGIN_MCP_PORT`; code default is `127.0.0.1`.
- **`launch.ogs`** is the only entry from Origin: `[main]` toggles; `[start]`/`[stop]`/`[status]`/
  `[setup]`/`[autostart]` set `MCP_ACTION$` then run `manage.py`. `package.ini` wires
  `LaunchScript`, `AfterInstall` (deps) and `BeforeUninstall` (stop).

### COM lifecycle (critical)

`op.attach()` holds an `OriginExt.ApplicationSI()` COM reference → Origin shows "controlled by
another application" and refuses to close. `originpro` releases it via `op.detach()` →
`Exit(releaseonly=True)` (frees the connection WITHOUT closing Origin), but only on a **graceful**
exit. Therefore `stop()` must NEVER just hard-kill: it writes `stop.request`, the watchdog calls
`op.detach()`, then exits. Hard `TerminateProcess` is an 8s fallback only and leaves Origin
controlled until COM/RPC times out.

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
curl -s -N -H "Accept: text/event-stream" http://host.docker.internal:8000/sse | head -3   # expect: event: endpoint
```

## Gotchas (hard-won; don't relearn these)

- **`originpro` in Origin's `PyPackage` is embedded-only.** Externally `import originpro` needs
  `OriginExt` (COM fallback) + `comtypes`, which aren't shipped. `manage.py setup()` pip-installs
  `OriginExt comtypes mcp fastmcp uvicorn starlette` into `vendor/`.
- **pip `--target vendor` does NOT run pywin32's post-install**, so `import pywintypes` (pulled in
  by `mcp`) fails. `mcp_bootstrap.py` manually adds `vendor/win32`, `win32/lib`, `win32com`,
  `win32comext`, `Pythonwin` to `sys.path` and `os.add_dll_directory(vendor/pywin32_system32)`.
- **Never name the server module `server.py`** — `vendor/win32com` exposes a top-level `server`
  package that shadows it. Hence `origin_mcp_server.py`.
- **`vendor/` is untracked/gitignored** (platform/version-specific, ~7800 files; auto-installed).
- **Building the OPX: exclude `.git`, `vendor/`, `__pycache__`, `*.log`, `host.handshake`.** Origin's
  packager flattens `.git` internals into the install folder and derives the folder name from
  package `Name` — that previously produced a duplicate "Origin MCP" folder and a corrupted repo.
  `package.ini` `Name=OriginMCP` (no space) so installs match the dev folder.
- **WSL2 clients**: mirrored networking breaks Docker Desktop port publishing here — use **NAT**
  (the default). The sidecar binds `0.0.0.0` (set via `ORIGIN_MCP_HOST` in `manage.py`'s child env;
  not LAN-exposed — the standard Windows Firewall blocks physical-NIC inbound by default). The WSL
  client uses `http://host.docker.internal:8000/sse`, allowed by a firewall rule scoped to the WSL
  range `172.16.0.0/12`.
- **`Python.<func>()` / embedded-python dispatch caches modules** — restart Origin after editing
  `manage.py`/`mcp_bootstrap.py` so changes load. The sidecar reads its `.py` fresh on each spawn,
  so a stop+start (toggle twice) picks up `mcp_bootstrap.py`/`origin_mcp_server.py` edits.
