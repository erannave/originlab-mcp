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
PORT = 8000

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


def _watchdog(host_pid):
    while True:
        time.sleep(5)
        if not _pid_alive(host_pid):
            _log("[bootstrap] host Origin gone — shutting down sidecar.")
            _remove_lock()
            os._exit(0)


def main():
    transport = sys.argv[1] if len(sys.argv) > 1 else "sse"
    host_pid, token = _read_handshake()

    import originpro as op
    try:
        op.attach()
        op.set_show(True)
        _verify_instance(op, token)
    except Exception as e:
        _log(f"[bootstrap] op.attach() failed: {e}")

    _write_lock()
    threading.Thread(target=_watchdog, args=(host_pid,), daemon=True).start()

    from server import mcp
    try:
        _log(f"[bootstrap] starting Origin MCP sidecar "
             f"(transport={transport}, host PID={host_pid}).")
        mcp.run(transport=transport)
    finally:
        _remove_lock()


if __name__ == "__main__":
    main()
