"""Stage the shipped OriginMCP files into a clean folder for mkOPX.

Runs in Origin's EMBEDDED Python via `run -pyf` from build.ogs. mkOPX packs the
WHOLE folder that holds package.ini (OPXFile::InitFromIni -> AddFolder), so the
live app folder must never be packaged directly: it also holds vendor\\ (~112 MB,
~7800 files), .git\\, __pycache__\\ and runtime artifacts (server.lock,
host.handshake, *.log, stop.request).

Sets the LabTalk numeric MCP_PKG_OK to 1 and the string MCP_PKG_DIR$ to the
staging folder on success, and leaves MCP_PKG_OK at 0 on failure, so build.ogs
can decide whether to run mkOPX.
"""

import os
import shutil
import tempfile
import traceback

import originpro as op

# Everything the installed app needs, and nothing else. Keep in sync with the
# "Building the OPX" section of CLAUDE.md.
SHIPPED = [
    "launch.ogs",
    "manage.py",
    "mcp_bootstrap.py",
    "origin_mcp_server.py",
    "AfterInstall.ogs",
    "BeforeUninstall.ogs",
    "AppIcon.png",
    "icon.svg",
    "requirements.txt",
    "README.md",
    "manual.html",
]

HERE = os.path.dirname(os.path.abspath(__file__))   # <repo>\packaging
ROOT = os.path.dirname(HERE)                        # <repo>
STAGE = os.path.join(tempfile.gettempdir(), "OriginMCP-pkg", "OriginMCP")


def _msg(text):
    op.lt_exec('type -a "OriginMCP package: ' + text.replace('"', "'") + '"')


def main():
    missing = [n for n in SHIPPED if not os.path.isfile(os.path.join(ROOT, n))]
    ini = os.path.join(HERE, "package.ini")
    if not os.path.isfile(ini):
        missing.append("packaging/package.ini")
    if missing:
        op.lt_exec("MCP_PKG_OK=0;")
        _msg("missing source file(s): " + ", ".join(missing))
        return

    # Fresh staging dir every time: a leftover file from an earlier build would
    # silently end up in the OPX.
    shutil.rmtree(STAGE, ignore_errors=True)
    os.makedirs(STAGE)
    for name in SHIPPED:
        shutil.copy2(os.path.join(ROOT, name), os.path.join(STAGE, name))
    shutil.copy2(ini, os.path.join(STAGE, "package.ini"))

    op.lt_exec('MCP_PKG_DIR$="' + STAGE + '";')
    op.lt_exec("MCP_PKG_OK=1;")
    _msg(f"staged {len(SHIPPED) + 1} files in {STAGE}")


try:
    main()
except Exception:
    try:
        op.lt_exec("MCP_PKG_OK=0;")
        _msg("staging crashed:\n" + traceback.format_exc())
    except Exception:
        pass
    raise
