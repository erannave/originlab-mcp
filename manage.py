"""Origin MCP sidecar lifecycle manager (runs in Origin's EMBEDDED Python).

Invoked from launch.ogs via `run -pyf manage.py`. The requested action is read
from the LabTalk session string MCP_ACTION$ (one of start/stop/toggle/status;
defaults to "toggle").

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

import ctypes
import glob
import os
import random
import subprocess

import originpro as op

HERE = os.path.dirname(os.path.abspath(__file__))
HANDSHAKE = os.path.join(HERE, "host.handshake")
LOCK = os.path.join(HERE, "server.lock")
LOG = os.path.join(HERE, "sidecar.log")
BOOTSTRAP = os.path.join(HERE, "mcp_bootstrap.py")
PORT = 8000
URL = f"http://127.0.0.1:{PORT}/sse"

CREATE_NO_WINDOW = 0x08000000
CREATE_NEW_PROCESS_GROUP = 0x00000200


def _msg(text):
    """Surface a message in the Origin UI (and stash it in the log)."""
    try:
        op.lt_exec('type -b "Origin MCP: ' + text.replace('"', "'") + '"')
    except Exception:
        pass


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
        [pyzip, os.path.join(pyzip, "site-packages"), pydlls, pypkg]
    )
    return env


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
    if not os.path.isfile(pyexe):
        _msg(f"Origin Python not found at {pyexe}")
        return
    if not os.path.isfile(BOOTSTRAP):
        _msg("mcp_bootstrap.py missing")
        return

    # Handshake: host PID (this Origin) + a per-instance token the sidecar
    # verifies after op.attach().
    host_pid = os.getpid()
    token = f"{host_pid}_{random.randint(1, 1 << 30)}"
    try:
        op.lt_exec(f'MCP_HOST_TOKEN$="{token}";')
    except Exception:
        pass
    try:
        with open(HANDSHAKE, "w", encoding="utf-8") as f:
            f.write(f"pid={host_pid}\ntoken={token}\n")
    except Exception as e:
        _msg(f"could not write handshake: {e}")
        return

    env = _child_env(exe_dir)
    try:
        logf = open(LOG, "a", encoding="utf-8")
        subprocess.Popen(
            [pyexe, BOOTSTRAP, "sse"],
            env=env,
            cwd=HERE,
            stdout=logf,
            stderr=logf,
            stdin=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except Exception as e:
        _msg(f"failed to start: {e}")
        return
    _msg(f"server starting — connect at {URL}")


def stop():
    pid = _read_lock_pid()
    if not pid or not _pid_alive(pid):
        _clear_lock()
        _msg("server is not running")
        return
    PROCESS_TERMINATE = 0x0001
    k = ctypes.windll.kernel32
    h = k.OpenProcess(PROCESS_TERMINATE, False, pid)
    if h:
        k.TerminateProcess(h, 1)
        k.CloseHandle(h)
    _clear_lock()
    _msg(f"server stopped (PID {pid})")


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
    if action == "start":
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


if __name__ == "__main__":
    main()
