from mcp.server.fastmcp import FastMCP
import originpro as op
import functools
import os
import sys

# Bind address for the HTTP transport. Default 127.0.0.1 (loopback only) — the
# server executes arbitrary LabTalk, so it must NOT be exposed to the LAN. A WSL
# client reaches it via WSL2 *mirrored* networking, which shares the Windows
# loopback (client connects to localhost:8000). Only override ORIGIN_MCP_HOST if
# you deliberately need another interface and have firewalled it accordingly.
_HOST = os.environ.get("ORIGIN_MCP_HOST", "127.0.0.1")
_PORT = int(os.environ.get("ORIGIN_MCP_PORT", "8000"))

# stateless_http=True is what lets a client survive an Origin restart WITHOUT a
# manual /mcp reconnect, and it is the whole reason we serve streamable-HTTP
# instead of the legacy SSE transport.
#
# Under SSE (or session-mode streamable-HTTP) every connection owns a
# ServerSession that starts NotInitialized and only becomes usable after the
# client's `initialize` handshake (mcp/server/session.py:204 raises "Received
# request before initialization was complete" otherwise). When Origin restarts,
# the sidecar dies with it; Claude Code transparently re-opens the transport but
# does NOT replay `initialize`, so every later tool call hits a fresh,
# uninitialized session and fails with MCP error -32602 until the user manually
# reconnects. Verified against Claude Code 2.1.221.
#
# In stateless mode the SDK builds a brand-new transport + session per request
# and stamps it Initialized at construction (mcp/server/session.py:98), so there
# is no session to lose and nothing to re-handshake. json_response=True returns
# a plain JSON body per POST rather than a single-event SSE stream.
# Sent to the client in the `initialize` result. Whether it reaches the model is
# up to the client — some inject it into the system prompt, some ignore it — so
# it is a bonus, not the delivery mechanism. The traps that MUST be seen live in
# the docstrings of run_labtalk / get_labtalk_value, which ship in the tool
# schema on every request.
_INSTRUCTIONS = """This server drives a live OriginLab Origin instance over COM.

LabTalk has a family of traps that return a wrong answer silently, fast, with no
error: aggregates over qualified column references, cumulative sum(), and
Set-Column-Values row subsets. Read the origin://labtalk-traps resource before
writing non-trivial LabTalk, and never accept an aggregate without checking the
value came back non-missing."""

mcp = FastMCP(
    "Origin-MCP",
    host=_HOST,
    port=_PORT,
    stateless_http=True,
    json_response=True,
    instructions=_INSTRUCTIONS,
)


def _safe(fn):
    """Wrap a tool so Python exceptions become readable strings instead of
    crashing the MCP call. Preserves the wrapped function's signature and
    docstring (via functools.wraps) so FastMCP still builds the correct schema.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            return f"Error in {fn.__name__}: {e}"
    return wrapper


@mcp.tool()
@_safe
def connect_origin() -> str:
    """Ensure Origin is connected, running, and visible.

    Binds to a running Origin instance via op.attach(), makes it visible, then
    confirms the connection by reading Origin's version number (@V) through
    LabTalk. Fails loudly with a clear message if Origin is not actually
    reachable. Returns the version and visibility state.
    """
    op.attach()
    op.set_show(True)
    version = op.lt_float('@V')
    visible = op.get_show()
    return f"Origin connected (version {version}, visible={visible})."


@mcp.tool()
@_safe
def create_project() -> str:
    """Create a new Origin project."""
    op.new()
    return "New project created."


@mcp.tool()
@_safe
def add_worksheet(name: str = "Sheet1") -> str:
    """Add a new worksheet to the current project."""
    ws = op.new_sheet('w', name)
    return f"Worksheet '{ws.name}' created."


@mcp.tool()
@_safe
def set_worksheet_data(worksheet_name: str, col_index: int, data: list[float]) -> str:
    """Set column data in the specified worksheet.

    Args:
        worksheet_name: The exact name of the worksheet to modify.
        col_index: The 0-based column index to write to.
        data: A list of numbers to put into the column.
    """
    ws = op.find_sheet('w', worksheet_name)
    if ws is None:
        return f"Error: Worksheet '{worksheet_name}' not found."
    ws.from_list(col_index, data)
    return f"Data set successfully in '{worksheet_name}' column {col_index}."


@mcp.tool()
@_safe
def save_project(path: str) -> str:
    """Save the project to a file.

    Args:
        path: The absolute path to save the .opju file to.
    """
    op.save(path)
    return f"Project saved to {path}."


@mcp.tool()
@_safe
def run_labtalk(script: str) -> str:
    """Execute an arbitrary LabTalk script in Origin.

    LabTalk is Origin's native scripting language — use it to drive fits, stats,
    X-Functions, import/export, and any operation not covered by a dedicated
    tool. Statements are separated by ';' or newlines; multi-line scripts are
    allowed.

    Example:
        run_labtalk("newbook; col(A) = {1,2,3,4}; col(B) = 2*col(A)")

    Returns a success/failure message. op.lt_exec returns False when the script
    errors; detailed LabTalk error text is not reliably exposed, so on failure
    only the boolean result is reported. To capture a textual result, write it
    into a LabTalk string variable inside the script and read it back with
    get_labtalk_value(..., as_string=True).

    TRAPS (silent wrong answers, no error raised — read origin://labtalk-traps):
      • An aggregate over a QUALIFIED column ref returns NANUM without reading
        the data: bind a range first — `range r = [Book]Sheet!col(2); total(r)`.
      • `sum()` is a CUMULATIVE sum; the grand total is `total()`.
    """
    ok = op.lt_exec(script)
    if ok:
        return "LabTalk script executed successfully (ok=True)."
    return "LabTalk script failed (ok=False, lt_exec returned False)."


@mcp.tool()
@_safe
def get_labtalk_value(expression: str, as_string: bool = False) -> str:
    """Evaluate a LabTalk expression or read a LabTalk variable, returned as text.

    Use this to read results back out of Origin: a fit coefficient (e.g.
    'fitlr.b'), an aggregate (e.g. 'total(col(A))'), or a worksheet cell
    (e.g. 'col(A)[3]').

    String convention: LabTalk string variable names end with '$'. If
    `as_string` is True OR the expression ends with '$', the value is read with
    op.get_lt_str; otherwise it is evaluated numerically with op.lt_float.

    Examples:
        get_labtalk_value("fitlr.b")           -> slope of a linear fit
        get_labtalk_value("total(col(A))")     -> column sum
        get_labtalk_value("mystr$")            -> contents of string var mystr$
        get_labtalk_value("mystr", as_string=True)

    TRAP: `total([Book]Sheet!col(2))` returns NANUM in ~0s with NO error — an
    aggregate over a qualified column ref never scans the data. Bind a range
    inside run_labtalk first, then read it back. See origin://labtalk-traps.
    """
    if as_string or expression.rstrip().endswith('$'):
        return op.get_lt_str(expression)
    return str(op.lt_float(expression))


@mcp.tool()
@_safe
def get_labtalk_tree(tree_name: str):
    """Read a structured LabTalk tree (e.g. a fit-result tree) back as a JSON dict.

    Many Origin operations populate a named tree variable with their full
    results. For example, after a linear fit the 'fitlr' tree holds slope,
    intercept, R², standard errors, and more. This returns the whole tree as a
    nested dict.

    Example:
        run_labtalk("fitlr (1,2)")
        get_labtalk_tree("fitlr")   -> {"N": ..., "b": ..., "a": ..., "r2": ..., ...}

    Returns a dict, or an error message if the tree does not exist or is empty.
    """
    tree = op.lt_tree_to_dict(tree_name)
    if tree is None:
        return f"Error: LabTalk tree '{tree_name}' not found or empty."
    return tree


@mcp.tool()
@_safe
def set_labtalk_var(name: str, value, is_string: bool = False) -> str:
    """Set a LabTalk variable so a subsequent script can read it as input.

    Use this to feed inputs into run_labtalk before running it. Numeric
    variables are set with op.set_lt_var; string variables (LabTalk names end
    with '$') with op.set_lt_str. If `is_string` is True or the name ends with
    '$', the value is treated as a string.

    Examples:
        set_labtalk_var("x0", 3.5)                  # numeric input
        set_labtalk_var("fname$", "C:/data.csv")    # string input
        set_labtalk_var("label", "run-7", is_string=True)
    """
    if is_string or name.rstrip().endswith('$'):
        op.set_lt_str(name, str(value))
        return f"String variable '{name}' set to '{value}'."
    op.set_lt_var(name, float(value))
    return f"Numeric variable '{name}' set to {value}."


@mcp.tool()
@_safe
def run_labtalk_with_readback(script: str, outputs: dict[str, str]):
    """Run a LabTalk script, then read several results back in a single round-trip.

    This is the highest-leverage tool: execute analysis and pull its outputs at
    once. `outputs` maps result keys to LabTalk expressions. Each expression is
    read with the same string-vs-numeric rule as get_labtalk_value: an
    expression ending in '$' is read as a string (op.get_lt_str), otherwise it
    is evaluated numerically (op.lt_float).

    Example — run a linear fit, then grab slope/intercept/R² together:
        run_labtalk_with_readback(
            "fitlr (1,2)",
            {"slope": "fitlr.b", "intercept": "fitlr.a", "r2": "fitlr.r2"},
        )
        -> {"ok": True, "results": {"slope": 2.0, "intercept": 0.0, "r2": 1.0}}

    Returns {"ok": <script success bool>, "results": {<key>: <value>, ...}}.
    A per-expression read error is captured as an error string in that key's
    value rather than failing the whole call.
    """
    ok = op.lt_exec(script)
    results = {}
    for key, expr in outputs.items():
        try:
            if expr.rstrip().endswith('$'):
                results[key] = op.get_lt_str(expr)
            else:
                results[key] = op.lt_float(expr)
        except Exception as e:
            results[key] = f"Error reading '{expr}': {e}"
    return {"ok": ok, "results": results}


@mcp.tool()
@_safe
def get_worksheet_data(worksheet_name: str, col=None, as_dataframe: bool = False):
    """Read data back out of a worksheet (complements set_worksheet_data).

    Resolves the sheet with op.find_sheet('w', worksheet_name) — the same lookup
    set_worksheet_data uses. If `col` is given (a 0-based index or a column
    name), returns that single column as a list. Otherwise returns the whole
    sheet as a dict of {column_name: [values]} (JSON-friendly).

    Examples:
        get_worksheet_data("Sheet1")           -> {"A": [1,2,3], "B": [2,4,6]}
        get_worksheet_data("Sheet1", col=0)    -> [1, 2, 3]
        get_worksheet_data("Sheet1", col="B")  -> [2, 4, 6]

    `as_dataframe` is accepted for parity but data is always returned in a
    JSON-serializable form. Returns an error message if the sheet is not found.
    """
    ws = op.find_sheet('w', worksheet_name)
    if ws is None:
        return f"Error: Worksheet '{worksheet_name}' not found."
    if col is not None:
        return ws.to_list(col)
    df = ws.to_df()
    return df.to_dict(orient='list')


# The LabTalk/data-layer subset of the Batalyse repo's ORIGIN-C.md — the items
# that fire through THIS server (several were originally found through it, and
# are marked "Verified via origin-mcp" there). Deliberately NOT the whole file:
# the bulk of it covers compiling Origin C .cpp sources in that repo, which is
# not reachable from here, and a second copy of it would only drift.
#
# ORIGIN-C.md remains the source of truth. Keep this list curated and short.
_LABTALK_TRAPS = """# LabTalk traps when driving Origin through this server

Every item below returns a WRONG answer silently — no exception, no error text,
and usually faster than the correct call. Speed is not evidence of success.

## 1. Aggregates over a qualified column reference return NANUM
`total([Book]Sheet!col(2))` / `mean(...)` / `max(...)` evaluate to NaN in ~0s
without ever scanning the data; lt_exec still reports success. Bind a range
object first:

    range r = [Book]Sheet!col(2); double v = total(r);

(0.013s over 694,329 rows on Origin 10.35.) Activating the sheet and using a
bare `col(2)` also works. The trap is that `wks.col2.nRows` on the SAME
qualified reference works fine, so a probe that reads row counts looks healthy
while every aggregate is quietly NaN.

## 2. `sum()` is a cumulative sum, not a total
In a Set-Column-Values formula, `colC = sum(A)` fills row i with A[1]+...+A[i].
The scalar grand total is `total(A)`. So `sum(D!X[a:b])` gives the cumulative
vector of the subrange, never the single sum. Cumulative `sum()` also skips
missing values, but the cumulative value AT a missing row is still NANUM.

## 3. Set-Column-Values row subsets do not bind per output row
`mean(D!Pot[C:D])` (C/D being start/end-row columns) binds to the FIRST row's
C:D for ALL output rows. Qualifying as `[SD!C:SD!D]` does not fix it. Scalar
cell refs DO bind per row (`D!col[SD!D]`, `D!col[max(SD!C-1,1)]`), so compute a
per-step aggregate as a cell difference over a cumulative column:

    (D!cum[SD!D] - D!cum[max(SD!C-1,1)]) / (D!Time[SD!D] - D!Time[max(SD!C-1,1)])

## 4. The numeric import separator is a GLOBAL setting
`is_numeric("1,234")` is false, yet the importer consumes the comma as a
THOUSANDS separator under `system.numeric.importseparator = 1`, inflating each
column by 10^decimals — silently, and by a different factor per column. Detect
the separator on the data actually being imported (not one probe row) and
restore the setting on every exit path, or the next dot-decimal file is
mis-scaled. Tell for a genuine dot-decimal row: a numeric token containing '.'.

## 5. Do not hand-roll per-cell loops
Origin C is interpreted: a per-cell loop costs ~10.8us/cell regardless of data
size (269.96s for 694,329 x 36). Reading the same cells out over COM and
reducing them in numpy took 22.35s; native aggregates via a range object cost
0.247us/cell. Reach for native X-Functions and LabTalk aggregates.

Source: ORIGIN-C.md in the Batalyse repo (full version also covers compiling
Origin C sources, which is out of scope for this server).
"""


@mcp.resource("origin://labtalk-traps", mime_type="text/markdown")
def labtalk_traps() -> str:
    """LabTalk pitfalls that silently return wrong results through this server.

    Read this before writing non-trivial LabTalk: aggregates over qualified
    column references, cumulative sum() vs total(), Set-Column-Values row
    subsets, the global numeric import separator, and per-cell loop costs.
    """
    return _LABTALK_TRAPS


if __name__ == "__main__":
    # Legacy client-spawned entry point (e.g. Claude Desktop running
    # "python server.py stdio"). The Origin-managed sidecar does NOT use this
    # path — it goes through mcp_bootstrap.py, which binds to the correct Origin
    # instance and installs a watchdog before calling mcp.run().
    transport = sys.argv[1] if len(sys.argv) > 1 else "http"
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        print(f"Starting Origin MCP Server on streamable-HTTP "
              f"({_HOST}:{_PORT}/mcp)")
        mcp.run(transport="streamable-http")
