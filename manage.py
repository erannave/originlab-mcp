"""Origin MCP sidecar lifecycle manager (runs in Origin's EMBEDDED Python).

Invoked from launch.ogs via `run -pyf manage.py`. The requested action is read
from the LabTalk session string MCP_ACTION$ (one of
start/stop/toggle/status/setup; defaults to "toggle").

Because this runs inside Origin's process, os.getpid() IS the host Origin PID.
We:
  • plant a per-instance token in MCP_HOST_TOKEN$ and write it + the host PID to
    host.handshake, so the spawned (external) sidecar can confirm via op.attach()
    that it bound to THIS Origin instance;
  • launch the sidecar as a detached, windowless child process using Origin's
    bundled python.exe, with PYTHONHOME/PYTHONPATH pointed at Origin's PyPackage
    (mirroring OriginC Autocomplete's OCLSP_GetOriginPythonLibPaths);
  • track its PID in server.lock for start/stop/status.

The sidecar (mcp_bootstrap.py) self-terminates when this Origin PID dies, so a
crash/close of Origin never leaves an orphan.
"""

import os
import sys
import traceback

# ── Bulletproof startup logging ────────────────────────────────────────────
# Written with stdlib only, BEFORE importing originpro, so that even an import
# failure or a wrong invocation leaves a trace on disk. This is the first thing
# to check when "clicking the app does nothing".
try:
    HERE = os.path.dirname(os.path.abspath(__file__))
except NameError:  # __file__ not set by some run -pyf paths
    HERE = os.getcwd()

STARTLOG = os.path.join(HERE, "startup.log")


def _slog(msg):
    try:
        with open(STARTLOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


_slog(f"--- manage.py invoked (cwd={os.getcwd()}, exe={sys.executable}) ---")

HANDSHAKE = os.path.join(HERE, "host.handshake")
LOCK = os.path.join(HERE, "server.lock")
LOG = os.path.join(HERE, "sidecar.log")
STOP = os.path.join(HERE, "stop.request")
BOOTSTRAP = os.path.join(HERE, "mcp_bootstrap.py")
VENDOR = os.path.join(HERE, "vendor")
# Keep in sync with origin_mcp_server.py / mcp_bootstrap.py: the actual bound
# port is ORIGIN_MCP_PORT (default 8000). Read it here so status/URL and the
# lock file reflect the real port, and propagate it to the child env below.
PORT = int(os.environ.get("ORIGIN_MCP_PORT", "8000"))
URL = f"http://127.0.0.1:{PORT}/sse"

# Packages the EXTERNAL sidecar needs that Origin's bundled PyPackage lacks.
# OriginExt is the COM module originpro falls back to for external op.attach()
# (the embedded _PyOrigin.pyd only loads inside Origin); comtypes is its COM
# backend. The rest are the MCP server stack. Installed into VENDOR/ via pip and
# put first on the sidecar's PYTHONPATH.
DEPS = ["OriginExt", "comtypes", "mcp", "uvicorn", "starlette"]
# Folder names that must exist under VENDOR for deps to be considered present.
DEP_MARKERS = ["OriginExt", "comtypes", "mcp"]

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200

try:
    import ctypes
    import glob
    import random
    import subprocess
    import time

    import originpro as op
    _slog("originpro imported OK")
except Exception:
    _slog("IMPORT FAILED:\n" + traceback.format_exc())
    raise


def _msg(text):
    """Surface a message in the Origin UI (and stash it in the log)."""
    _slog("MSG: " + text)
    try:
        op.lt_exec('type -b "Origin MCP: ' + text.replace('"', "'") + '"')
    except Exception:
        _slog("lt_exec(type -b) failed:\n" + traceback.format_exc())


def _origin_python():
    exe_dir = op.path('e')  # <exeDir>\
    pyexe = os.path.join(exe_dir, "64bit", "PyDLLs", "python.exe")
    return exe_dir, pyexe


def _child_env(exe_dir):
    """Build the environment for the sidecar so the bundled python.exe finds its
    stdlib (PYTHONHOME) and Origin's packages incl. originpro (PYTHONPATH)."""
    zips = glob.glob(os.path.join(exe_dir, "python*.zip"))
    pyzip = zips[0] if zips else os.path.join(exe_dir, "python311.zip")
    pydlls = os.path.join(exe_dir, "64bit", "PyDLLs")
    alluser = op.get_lt_str('%@R')  # C:\ProgramData\OriginLab\<ver>\
    pypkg = os.path.join(alluser, "PyPackage", "Py3")
    env = dict(os.environ)
    env["PYTHONHOME"] = pydlls
    env["PYTHONPATH"] = os.pathsep.join(
        [VENDOR, pyzip, os.path.join(pyzip, "site-packages"), pydlls, pypkg]
    )
    # Bind on all interfaces so a WSL2 (NAT) client can reach the server via the
    # Windows host IP (host.docker.internal). NOT LAN-exposed: the standard
    # Windows Firewall blocks inbound on the physical NICs by default; only a
    # WSL-subnet-scoped firewall rule opens it to the local WSL VM. The server's
    # own default stays 127.0.0.1 — this env var is the app's explicit opt-in.
    env["ORIGIN_MCP_HOST"] = "0.0.0.0"
    # Pin the port the sidecar binds so it matches PORT/URL/server.lock here.
    env["ORIGIN_MCP_PORT"] = str(PORT)
    return env


def _deps_present():
    return all(os.path.isdir(os.path.join(VENDOR, m)) for m in DEP_MARKERS)


def setup():
    """Install the sidecar's Python dependencies into VENDOR/ using Origin's
    bundled python.exe + pip. Blocking (runs on the embedded/UI thread), so it
    briefly freezes Origin — acceptable for a one-time install."""
    exe_dir, pyexe = _origin_python()
    if not os.path.isfile(pyexe):
        _msg(f"Origin Python not found at {pyexe}")
        return False
    os.makedirs(VENDOR, exist_ok=True)
    env = _child_env(exe_dir)
    cmd = [pyexe, "-m", "pip", "install", "--upgrade", "--target", VENDOR] + DEPS
    _slog("pip cmd: " + " ".join(cmd))
    try:
        proc = subprocess.run(
            cmd, env=env, cwd=HERE, capture_output=True, text=True,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception:
        _slog("pip run crashed:\n" + traceback.format_exc())
        _msg("dependency install crashed; see startup.log")
        return False
    _slog(f"pip rc={proc.returncode}")
    _slog("pip stdout:\n" + (proc.stdout or ""))
    _slog("pip stderr:\n" + (proc.stderr or ""))
    if proc.returncode == 0 and _deps_present():
        _msg("dependencies installed")
        return True
    _msg(f"dependency install failed (rc={proc.returncode}); see startup.log")
    return False


def _read_lock_pid():
    try:
        with open(LOCK, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip().lower().startswith("pid="):
                    return int(line.split("=", 1)[1].strip())
    except Exception:
        return None
    return None


def _pid_alive(pid):
    if not pid:
        return False
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


def _clear_lock():
    try:
        if os.path.exists(LOCK):
            os.remove(LOCK)
    except Exception:
        pass


def is_running():
    """Return the live sidecar PID, or None. Clears a stale lock file."""
    pid = _read_lock_pid()
    if pid and _pid_alive(pid):
        return pid
    if os.path.exists(LOCK):
        _clear_lock()
    return None


def start():
    running = is_running()
    if running:
        _msg(f"already running (PID {running}) — {URL}")
        return

    exe_dir, pyexe = _origin_python()
    _slog(f"exe_dir={exe_dir}  pyexe={pyexe}  exists={os.path.isfile(pyexe)}")
    if not os.path.isfile(pyexe):
        _msg(f"Origin Python not found at {pyexe}")
        return
    if not os.path.isfile(BOOTSTRAP):
        _msg(f"mcp_bootstrap.py missing at {BOOTSTRAP}")
        return

    # First run: install the sidecar's dependencies (blocks briefly).
    if not _deps_present():
        _msg("installing dependencies (first run, may take a minute)…")
        if not setup():
            return

    # Handshake: host PID (this Origin) + a per-instance token the sidecar
    # verifies after op.attach().
    host_pid = os.getpid()
    token = f"{host_pid}_{random.randint(1, 1 << 30)}"
    try:
        op.lt_exec(f'MCP_HOST_TOKEN$="{token}";')
    except Exception:
        _slog("planting MCP_HOST_TOKEN$ failed:\n" + traceback.format_exc())
    try:
        with open(HANDSHAKE, "w", encoding="utf-8") as f:
            f.write(f"pid={host_pid}\ntoken={token}\n")
    except Exception as e:
        _msg(f"could not write handshake: {e}")
        return

    env = _child_env(exe_dir)
    _slog("child PYTHONHOME=" + env["PYTHONHOME"])
    _slog("child PYTHONPATH=" + env["PYTHONPATH"])
    logf = None
    try:
        logf = open(LOG, "a", encoding="utf-8")
        proc = subprocess.Popen(
            [pyexe, BOOTSTRAP, "sse"],
            env=env,
            cwd=HERE,
            stdout=logf,
            stderr=logf,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
        _slog(f"spawned sidecar child PID={proc.pid}")
    except Exception as e:
        _msg(f"failed to start: {e}")
        _slog("Popen failed:\n" + traceback.format_exc())
        return
    finally:
        # The child inherited its own duplicate of the log handle during spawn,
        # so the parent's copy is no longer needed. Closing it avoids leaking a
        # file handle on every start() inside Origin's long-lived embedded Python.
        if logf is not None:
            try:
                logf.close()
            except Exception:
                pass
    _msg(f"server starting — connect at {URL}")


def stop():
    pid = _read_lock_pid()
    if not pid or not _pid_alive(pid):
        _clear_lock()
        try:
            if os.path.exists(STOP):
                os.remove(STOP)
        except Exception:
            pass
        _msg("server is not running")
        return

    # If we ALREADY asked it to stop a while ago and it's still alive, the sidecar
    # is genuinely stuck — hard-kill as a last resort. (This may briefly leave
    # Origin "controlled" until COM/RPC notices the dead client.) The sidecar
    # removes STOP on a clean exit, so a lingering, old STOP means it never left.
    if os.path.exists(STOP):
        try:
            age = time.time() - os.path.getmtime(STOP)
        except OSError:
            age = 0.0
        if age > 6.0:
            _slog(f"sidecar still alive {age:.0f}s after stop request; terminating")
            PROCESS_TERMINATE = 0x0001
            k = ctypes.windll.kernel32
            h = k.OpenProcess(PROCESS_TERMINATE, False, pid)
            if h:
                k.TerminateProcess(h, 1)
                k.CloseHandle(h)
            _clear_lock()
            try:
                os.remove(STOP)
            except Exception:
                pass
            _msg(f"server force-stopped (PID {pid}); if Origin still says "
                 f"'controlled', wait a few seconds for COM to release")
            return

    # Graceful stop: ask the sidecar to release its COM attachment (op.detach) and
    # exit, then RETURN IMMEDIATELY. We must NOT sleep-wait here: stop() runs on
    # Origin's STA thread (via `run -pyf`), which is the SAME thread that has to
    # SERVICE the sidecar's op.detach() COM call. Blocking here parks that thread,
    # the detach can't be serviced, and it deadlocks until a hard kill — leaving
    # Origin "controlled". By returning at once we free Origin's thread; the
    # sidecar detaches (Origin services it) and exits on its own within ~1s,
    # dropping the lock. A genuinely stuck sidecar is caught by the age check above
    # on the next stop click.
    try:
        with open(STOP, "w", encoding="utf-8") as f:
            f.write("stop\n")
    except Exception as e:
        _slog(f"could not write stop request: {e}")
        _msg("could not write stop request; see startup.log")
        return
    _msg(f"stopping (PID {pid}) — Origin will be released in a moment")


def status():
    pid = is_running()
    if pid:
        _msg(f"running (PID {pid}) — {URL}")
    else:
        _msg("not running")


def main():
    try:
        action = (op.get_lt_str("MCP_ACTION$") or "toggle").strip().lower()
    except Exception:
        action = "toggle"
    _slog("action=" + action)
    if action == "setup":
        setup()
    elif action == "start":
        start()
    elif action == "stop":
        stop()
    elif action == "status":
        status()
    else:  # toggle (default)
        if is_running():
            stop()
        else:
            start()


try:
    main()
except Exception:
    _slog("main() crashed:\n" + traceback.format_exc())
    raise
