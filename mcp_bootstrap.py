"""Origin MCP sidecar entry point (runs as a SEPARATE external process).

Spawned by manage.py (inside Origin's embedded Python) as:

    <exeDir>\\64bit\\PyDLLs\\python.exe  <app>\\mcp_bootstrap.py  sse

PYTHONHOME / PYTHONPATH are set by manage.py so `import originpro` / `import mcp`
resolve against Origin's PyPackage.

Responsibilities beyond a bare server.py:
  1. Bind to the correct Origin instance. manage.py wrote host.handshake (the
     host Origin PID + a per-instance token planted in MCP_HOST_TOKEN$). We
     op.attach(), then verify the token matches.
  2. Watchdog: exit when the host Origin disappears, so a crash/close of Origin
     never leaves an orphaned server. The watchdog checks the host PID via the
     OS (NOT via op) to avoid touching Origin COM from a background thread.
  3. Write/remove server.lock (PID + port) so manage.py's start/stop/status and
     a second Origin instance can tell the sidecar is already up.
"""

import ctypes
import glob
import os
import sys
import threading
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
HANDSHAKE_PATH = os.path.join(HERE, "host.handshake")
LOCK_PATH = os.path.join(HERE, "server.lock")
LOG_PATH = os.path.join(HERE, "sidecar.log")
STOP_PATH = os.path.join(HERE, "stop.request")
# Must match the port origin_mcp_server.py actually binds (ORIGIN_MCP_PORT,
# default 8000) so server.lock records the real port, not a hardcoded guess.
PORT = int(os.environ.get("ORIGIN_MCP_PORT", "8000"))

# Set in main() after import so the shutdown path can release the COM
# attachment. _OP.detach() MUST be called from the same (main) thread that
# called op.attach() — see _serve_http below.
_OP = None

# Defensive sys.path augmentation: PYTHONPATH from the spawn env is primary, but
# if launched by hand for debugging, derive Origin's lib paths from the
# interpreter location so imports still resolve.
_pydlls = os.path.dirname(sys.executable)             # <exeDir>\64bit\PyDLLs
_exe_dir = os.path.dirname(os.path.dirname(_pydlls))  # <exeDir>
# Glob the stdlib zip rather than hardcoding python311.zip: the name tracks the
# Python version Origin bundles and WILL change (Origin 2024..2026b all ship
# 3.11, an earlier one shipped 3.8). manage.py:_child_env does the same, so a
# hardcoded name here would silently diverge from the spawn env on a future
# Origin and only break the hand-launched debug path. Fall back to the current
# name if the glob finds nothing, so behaviour never gets worse than before.
_zips = glob.glob(os.path.join(_exe_dir, "python*.zip"))
_pyzip = _zips[0] if _zips else os.path.join(_exe_dir, "python311.zip")
for _p in (
    _pyzip,
    os.path.join(_pyzip, "site-packages"),
    _pydlls,
):
    if _p not in sys.path:
        sys.path.append(_p)

# pywin32 fix-up. pip `--target` drops pywin32 into vendor/ but does NOT run the
# usual .pth/post-install, so `import pywintypes` (pulled in transitively by mcp)
# fails: pywintypes.py lives in vendor/win32/lib and pywintypes311.dll in
# vendor/pywin32_system32 — neither is on the path. Wire them up by hand before
# any import that needs them.
_VENDOR = os.path.join(HERE, "vendor")
_sys32 = os.path.join(_VENDOR, "pywin32_system32")
if os.path.isdir(_sys32):
    try:
        os.add_dll_directory(_sys32)
    except Exception:
        pass
    os.environ["PATH"] = _sys32 + os.pathsep + os.environ.get("PATH", "")
for _sub in ("win32", os.path.join("win32", "lib"), "win32com", "win32comext",
             "Pythonwin"):
    _wp = os.path.join(_VENDOR, _sub)
    if os.path.isdir(_wp) and _wp not in sys.path:
        sys.path.insert(0, _wp)


def _log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass
    print(msg, file=sys.stderr, flush=True)


def _read_handshake():
    """Return (host_pid, token) from the handshake file, or (None, None)."""
    host_pid, token = None, None
    try:
        with open(HANDSHAKE_PATH, "r", encoding="utf-8") as f:
            for line in f:
                if "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip().lower(), val.strip()
                if key == "pid":
                    host_pid = int(val)
                elif key == "token":
                    token = val
    except Exception as e:
        _log(f"[bootstrap] could not read handshake: {e}")
    return host_pid, token


def _pid_alive(pid):
    """True if `pid` is a live process (Win32 directly, no COM)."""
    if not pid:
        return True  # nothing to monitor -> never self-terminate
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k = ctypes.windll.kernel32
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if not k.GetExitCodeProcess(h, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        k.CloseHandle(h)


def _write_lock():
    try:
        with open(LOCK_PATH, "w", encoding="utf-8") as f:
            f.write(f"pid={os.getpid()}\nport={PORT}\n")
    except Exception as e:
        _log(f"[bootstrap] could not write lock: {e}")


def _remove_lock():
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except Exception:
        pass


def _verify_instance(op, token):
    """Confirm op.attach() bound to the Origin that spawned us by matching the
    token planted in MCP_HOST_TOKEN$. Best-effort: warns on mismatch but does
    not abort (originpro has no reliable way to re-pick a specific ROT entry)."""
    if not token:
        return
    try:
        bound = op.get_lt_str("MCP_HOST_TOKEN$")
    except Exception as e:
        _log(f"[bootstrap] token read failed: {e}")
        return
    if bound != token:
        _log(f"[bootstrap] WARNING: attached to a different Origin instance "
             f"(expected {token!r}, got {bound!r}). Tool calls may target the "
             f"wrong Origin.")
    else:
        _log("[bootstrap] verified bound to host Origin instance.")


def _release_origin():
    """Release the COM attachment so Origin is no longer 'controlled by another
    application' and can be closed normally. op.detach() -> Exit(releaseonly)
    drops the connection WITHOUT closing Origin."""
    if _OP is None:
        return
    try:
        _OP.detach()
        _log("[bootstrap] released Origin (op.detach).")
    except Exception as e:
        _log(f"[bootstrap] op.detach() failed: {e}")


def _serve_http(mcp, host_pid):
    """Serve streamable-HTTP and own the entire shutdown sequence on the MAIN
    thread. NEVER returns — it os._exit()s.

    The transport is STATELESS (see origin_mcp_server.py) so that a client keeps
    working across an Origin restart without a manual reconnect. Do not swap this
    back to mcp.sse_app(): SSE reintroduces per-connection sessions, and Claude
    Code does not replay `initialize` when it re-opens a dropped transport, so
    every post-restart tool call fails with -32602.

    Why not just flip uvicorn's should_exit and let serve() return so a finally
    can detach? Because uvicorn's graceful shutdown HANGS tearing down long-lived
    streams (even with force_exit), and asyncio.run()'s own teardown then
    blocks awaiting those cancelled tasks. serve() routinely failed to return
    inside manage.py's 8s window, so we were hard-killed with COM still attached
    → Origin stuck 'controlled by another application'.

    Instead: run serve() as a task and poll the stop conditions INSIDE the loop.
    Once asked to stop, stop running the loop (without awaiting the transport
    teardown), release COM HERE — this is the only thread whose STA apartment
    owns the Origin reference, so op.detach() actually succeeds — and os._exit().
    The OS reclaims the still-open sockets; we never wait on them."""
    import asyncio

    import uvicorn

    # Starlette app whose lifespan runs the StreamableHTTPSessionManager; uvicorn's
    # default lifespan="auto" drives it, so no extra wiring is needed here.
    app = mcp.streamable_http_app()
    config = uvicorn.Config(
        app,
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    # Detached, no-console process: don't let uvicorn grab SIGINT/SIGTERM.
    server.install_signal_handlers = lambda: None

    # Detach COM on the way out, UNLESS the host Origin is already dead (its COM
    # host is gone — detaching would just fail). Mutable so _drive can set it.
    release = [True]

    async def _drive():
        serve_task = asyncio.create_task(server.serve())
        while True:
            await asyncio.sleep(0.5)
            if serve_task.done():
                # Server stopped on its own (e.g. failed to bind the port).
                # Re-await to surface the exception into the log, then exit.
                try:
                    await serve_task
                except Exception:
                    _log("[bootstrap] uvicorn serve() exited:\n"
                         + traceback.format_exc())
                return
            if os.path.exists(STOP_PATH):
                _log("[bootstrap] shutting down: stop requested")
                release[0] = True
                return
            if not _pid_alive(host_pid):
                _log("[bootstrap] shutting down: host Origin gone")
                release[0] = False
                return

    # Own loop (not asyncio.run) so we control teardown: when _drive returns we
    # simply stop driving the loop and hard-exit, rather than awaiting the
    # cancellation of the SSE stream tasks (which is exactly what used to hang).
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_drive())
    except Exception:
        _log("[bootstrap] serve loop crashed:\n" + traceback.format_exc())

    if release[0]:
        _release_origin()
    _remove_lock()
    try:
        if os.path.exists(STOP_PATH):
            os.remove(STOP_PATH)
    except Exception:
        pass
    os._exit(0)


def _legacy_watchdog(host_pid):
    """Watchdog for the legacy stdio transport (client-spawned, not Origin-
    managed). Best-effort hard exit; the stdio path has no STA-safe place to
    op.detach() from a background thread, so it just exits."""
    while True:
        time.sleep(2)
        if os.path.exists(STOP_PATH) or not _pid_alive(host_pid):
            _remove_lock()
            os._exit(0)


def main():
    global _OP
    transport = sys.argv[1] if len(sys.argv) > 1 else "http"
    host_pid, token = _read_handshake()

    # Clear any stale stop request from a previous run.
    try:
        if os.path.exists(STOP_PATH):
            os.remove(STOP_PATH)
    except Exception:
        pass

    import originpro as op
    _OP = op
    try:
        op.attach()
        op.set_show(True)
        _verify_instance(op, token)
    except Exception as e:
        _log(f"[bootstrap] op.attach() failed: {e}")

    # Import the server (and its transitive deps) BEFORE writing the lock, so a
    # failed import never leaves a stale lock behind. The module is named
    # origin_mcp_server (NOT "server") to avoid colliding with win32com.server,
    # which is exposed as a top-level "server" once vendor/win32com is on path.
    from origin_mcp_server import mcp

    _write_lock()
    _log(f"[bootstrap] starting Origin MCP sidecar "
         f"(transport={transport}, host PID={host_pid}).")
    # "sse" is accepted as a legacy alias so an older cached manage.py (Origin's
    # embedded Python caches it until restart) still spawns the HTTP sidecar.
    if transport in ("http", "sse"):
        # Owns shutdown + COM release + process exit on the main thread.
        _serve_http(mcp, host_pid)
        return  # not reached: _serve_http os._exit()s
    # Legacy stdio path (client-spawned, not Origin-managed).
    threading.Thread(target=_legacy_watchdog, args=(host_pid,), daemon=True).start()
    try:
        mcp.run(transport=transport)
    finally:
        _release_origin()
        _remove_lock()


if __name__ == "__main__":
    main()
