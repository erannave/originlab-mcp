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
import os
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HANDSHAKE_PATH = os.path.join(HERE, "host.handshake")
LOCK_PATH = os.path.join(HERE, "server.lock")
LOG_PATH = os.path.join(HERE, "sidecar.log")
STOP_PATH = os.path.join(HERE, "stop.request")
# Must match the port origin_mcp_server.py actually binds (ORIGIN_MCP_PORT,
# default 8000) so server.lock records the real port, not a hardcoded guess.
PORT = int(os.environ.get("ORIGIN_MCP_PORT", "8000"))

# Set in main() after import so the watchdog thread can release the COM
# attachment on shutdown.
_OP = None

# Defensive sys.path augmentation: PYTHONPATH from the spawn env is primary, but
# if launched by hand for debugging, derive Origin's lib paths from the
# interpreter location so imports still resolve.
_pydlls = os.path.dirname(sys.executable)             # <exeDir>\64bit\PyDLLs
_exe_dir = os.path.dirname(os.path.dirname(_pydlls))  # <exeDir>
for _p in (
    os.path.join(_exe_dir, "python311.zip"),
    os.path.join(_exe_dir, "python311.zip", "site-packages"),
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


def _shutdown(reason, release):
    _log(f"[bootstrap] shutting down: {reason}")
    if release:
        _release_origin()
    _remove_lock()
    try:
        if os.path.exists(STOP_PATH):
            os.remove(STOP_PATH)
    except Exception:
        pass
    # Hard-exit the process from this thread (the main thread is blocked in
    # uvicorn). COM is already released above, so Origin is freed.
    os._exit(0)


def _watchdog(host_pid):
    while True:
        time.sleep(2)
        # Explicit stop requested by manage.py: release Origin, then exit.
        if os.path.exists(STOP_PATH):
            _shutdown("stop requested", release=True)
        # Host Origin gone: nothing to release (COM host is dead anyway).
        if not _pid_alive(host_pid):
            _shutdown("host Origin gone", release=False)


def main():
    global _OP
    transport = sys.argv[1] if len(sys.argv) > 1 else "sse"
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
    threading.Thread(target=_watchdog, args=(host_pid,), daemon=True).start()
    try:
        _log(f"[bootstrap] starting Origin MCP sidecar "
             f"(transport={transport}, host PID={host_pid}).")
        mcp.run(transport=transport)
    finally:
        # Normal exit path: release Origin and clean up the lock.
        _release_origin()
        _remove_lock()


if __name__ == "__main__":
    main()
