r"""Stage the shipped OriginMCP files into a clean folder for mkOPX.

Also chdir()s Origin's process into that folder, because mkOPX only stores
RELATIVE file paths when it is invoked in "current folder" mode. See below.

Runs in Origin's EMBEDDED Python via `run -pyf` from build.ogs. mkOPX packs the
WHOLE folder that holds package.ini (OPXFile::InitFromIni -> AddFolder), so the
live app folder must never be packaged directly: it also holds vendor\\ (~112 MB,
~7800 files), .git\\, __pycache__\\ and runtime artifacts (server.lock,
host.handshake, *.log, stop.request).

Two steps, selected by the LabTalk string MCP_PKG_STEP$:
  "stage"   (default) copy the files, then chdir into the staging folder;
  "restore" chdir back to where Origin started (MCP_PKG_CWD$).

WHY THE CHDIR IS THE WHOLE POINT. mkOPX stores each packed file under the path
it will be extracted to, relative to the Apps root — a correct App OPX contains
`OriginMCP\launch.ogs` (compare `Theme Preview\OlocalC.txt` in any shipped
app). That relative base comes from `AddFolder(m_srcPath, lpcszSrcPath)`, and
mkOPX only supplies `lpcszSrcPath` when it is called with NO `ini:=` and NO
`app:=` — the branch that reads `_getcwd()` and takes the PARENT of it as the
base (mkOPX.XFC). Pass `ini:=` and that base is NULL, so every entry is stored
as its full source path minus the drive letter; the installer then faithfully
recreates `Apps\Users\<user>\AppData\Local\Temp\...` and the app folder
gets nothing. That was a real shipped bug, not a theory.

So: chdir into the staging folder (named OriginMCP, which is what makes the
stored prefix `OriginMCP\`) and let mkOPX find package.ini in the cwd.

Sets the LabTalk numeric MCP_PKG_OK to 1 on success and leaves it 0 on failure,
so build.ogs can decide whether to run mkOPX.
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


def stage():
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

    # Record where Origin was, then move into the staging folder so mkOPX runs
    # in "current folder" mode. build.ogs restores this immediately afterwards.
    op.lt_exec('MCP_PKG_CWD$="' + os.getcwd() + '";')
    os.chdir(STAGE)

    op.lt_exec('MCP_PKG_DIR$="' + STAGE + '";')
    op.lt_exec("MCP_PKG_OK=1;")
    _msg(f"staged {len(SHIPPED) + 1} files in {STAGE}")


def restore():
    """Put Origin's working directory back after mkOPX has run."""
    old = op.get_lt_str("MCP_PKG_CWD$")
    if old and os.path.isdir(old):
        os.chdir(old)


def main():
    step = (op.get_lt_str("MCP_PKG_STEP$") or "stage").strip().lower()
    if step == "restore":
        restore()
    else:
        stage()


try:
    main()
except Exception:
    try:
        op.lt_exec("MCP_PKG_OK=0;")
        _msg("staging crashed:\n" + traceback.format_exc())
    except Exception:
        pass
    raise
