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

**Primary route — `packaging/build.ogs`.** From Origin's Script Window:

```
run.section("C:\Users\<user>\AppData\Local\OriginLab\Apps\OriginMCP\packaging\build.ogs", main);
```

It runs `packaging/stage.py` (copies the shipped files + `packaging/package.ini` into a clean temp
folder) and then the `mkOPX` X-Function, which writes `OriginMCP.opx` into the **User Files Folder**
(`%Y`, i.e. `Documents\OriginLab\User Files\`). Deliberately not inside the app folder: installing
an OPX that lives under `Apps\OriginMCP\` asks Origin to overwrite the folder holding the package
file it is reading. No dialog, and the
manifest is version-controlled in `packaging/package.ini` instead of living only in Code Builder's
remembered dialog state — which is why the 1.1 build had nowhere for a fix to live.

- **The `package.ini` lives in `packaging/`, NEVER in the repo root.** `mkOPX` (and Code Builder's
  Generate) IGNORE `[Files] SourcePath` and pack the ENTIRE folder that contains `package.ini`
  (`OPXFile::InitFromIni` → `AddFolder(GetFilePath(ini))`). In the root that means `vendor/`
  (~112MB) + `.git` (~32MB) — a ~93MB OPX that hangs Origin (you must force-close). Hence the
  staging folder: it holds exactly the shipped files and the manifest, nothing else.
- **Ship exactly these files** (`SHIPPED` in `packaging/stage.py`): `launch.ogs`, `manage.py`,
  `mcp_bootstrap.py`, `origin_mcp_server.py`, `AfterInstall.ogs`, `BeforeUninstall.ogs`,
  `AppIcon.png`, `icon.svg`, `requirements.txt`, `README.md`, `manual.html`. NOT `vendor/`, `.git`,
  `__pycache__`, logs or runtime artifacts. `vendor/` is rebuilt on install by `AfterInstall.ogs`.
- **`[Origin] Version=10.10` must stay ≥ the Python floor.** `manage.py setup()` refuses to install
  on Python < 3.10 (`mcp` requires it); 10.10 is Origin 2024, the oldest release verified to bundle
  Python 3.11. Lowering it would let Origin accept an install that the version gate then rejects.
  (10.10 and 10.1 are the same number — Origin renders `OriginVerReq` with two decimals.)

**Fallback route — Code Builder Generate.** Right-click the `OriginMCP` app folder in the Workspace
window → **Generate…** → Package Manager → `File ▸ Save` → `OriginMCP.opx`. It ignores
`packaging/package.ini` and uses its own explicit file list (`..\CBPackages.ini`) plus the fields
remembered in its dialog, so the manifest below must be entered/verified there by hand — keep it in
sync with `packaging/package.ini`:

| Field                | Value |
|----------------------|-------|
| Name                 | `OriginMCP` — no space, so installs match the dev folder |
| Description          | `Runs an MCP (Model Context Protocol) server as a background sidecar so AI clients can drive this Origin instance.` |
| Version / Author     | `1.20` / `Batalyse GmbH` |
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
  `OriginExt comtypes mcp uvicorn starlette` into `vendor/`.
- **The `DEPS` upper bounds are load-bearing.** `mcp` is pinned `<2`: mcp 2.x renamed `FastMCP` to
  `MCPServer` and left `mcp.server.fastmcp` a stub that raises `ModuleNotFoundError`, so an
  unpinned `mcp` resolves 2.x on any machine installing today and the sidecar dies on its first
  import. `starlette<1`/`uvicorn<1` guard the same failure one level down. mcp 1.x is still
  actively released in parallel with 2.x, so this is a stable position, not a stopgap.
- **`DEP_MARKERS` must only name packages the sidecar imports.** A marker for something merely
  transitive can stop being installed when someone else's dependency tree is reorganised, and then
  every clean install is reported as a failure. That is what happened to the old `sniffio` marker
  (a canary for `anyio`, which dropped the dependency in 4.10) — it broke every install on a
  machine other than the dev box with "dependency install failed (rc=0)".
- **`vendor/.deps` is what makes a pin change take effect.** pip `--target --upgrade` layers a new
  version OVER the old one without removing it, and a tree built by an earlier, unpinned `DEPS`
  still contains every marker folder — so on markers alone `setup()` would be skipped forever and
  the sidecar would keep booting against mcp 2.x. The stamp records the exact `DEPS` list that
  built `vendor/`; changing `DEPS` invalidates it and forces a wipe-and-reinstall. `setup()`
  refuses to wipe while the sidecar is running (it holds `vendor/pywin32_system32` DLLs open).
- **pip `--target vendor` does NOT run pywin32's post-install**, so `import pywintypes` (pulled in
  by `mcp`) fails. `mcp_bootstrap.py` manually adds `vendor/win32`, `win32/lib`, `win32com`,
  `win32comext`, `Pythonwin` to `sys.path` and `os.add_dll_directory(vendor/pywin32_system32)`.
- **pip `--target vendor` needs `--ignore-installed`.** `setup()` runs pip with PYTHONPATH exposing
  the host Origin's `ProgramData\OriginLab\<ver>\PyPackage\Py3`; without the flag, pip treats
  packages found there (e.g. `idna`, `attrs` installed by other Apps) as "already satisfied" and
  skips vendoring them. The sidecar then works under that Origin version but crashes with
  `ModuleNotFoundError` under a freshly installed one (this broke Origin 2026b while 2026 worked —
  its new `103b\PyPackage\Py3` was empty). `DEP_MARKERS` now checks the known leak-prone names.
- **The LabTalk traps are duplicated on purpose, but only a curated subset.**
  `origin_mcp_server.py` carries two one-line trap warnings in the `run_labtalk` /
  `get_labtalk_value` docstrings and a fuller five-item `origin://labtalk-traps` resource.
  Docstrings are the only mechanism with guaranteed reach — they ship in the tool schema on every
  request, whereas `instructions=` is injected at the client's discretion and a resource is
  pull-based (nobody fetches what they don't know they need). The source of truth stays
  `ORIGIN-C.md` in the Batalyse repo; do NOT copy the rest of it here — the bulk covers compiling
  Origin C `.cpp` sources, which is unreachable through this server, and a second full copy would
  drift. Keep the subset short: tool descriptions are paid by every client on every request.
- **Never name the server module `server.py`** — `vendor/win32com` exposes a top-level `server`
  package that shadows it. Hence `origin_mcp_server.py`.
- **`vendor/` is untracked/gitignored** (platform/version-specific, ~7800 files; auto-installed).
- **Installing the OPX OVERWRITES the dev tree** — the app folder IS the install location, so any
  uncommitted edit to a shipped file is silently reverted to the packaged version. Commit before
  installing; recover with `git restore <file>`. Symptom: the sidecar keeps serving old behaviour
  after a toggle, and the file's mtime jumps back to the packaged file's date. Corollary: rebuild
  the OPX BEFORE installing, never after — a `build.ogs` run following an install just re-packages
  the version you were trying to replace.
- **Every install drops a `package.ini` into the app root** (it is packed from `packaging/`, and
  `mkOPX` packs the whole staging folder). It is gitignored. Delete it before using the Code
  Builder Generate fallback, or Generate will bundle `vendor/` + `.git`.
- **Building the OPX** has its own section above. The trap: a `package.ini` in the ROOT makes
  `mkOPX`/Generate pack the whole tree — `vendor/`+`.git` (~93MB → Origin hangs). The manifest
  lives in `packaging/package.ini` (incl. the `AfterInstall`/`BeforeUninstall` hooks) and is
  packaged from a clean staging folder by `packaging/build.ogs`.
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
