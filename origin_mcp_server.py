from mcp.server.fastmcp import FastMCP
import originpro as op
import functools
import os
import sys

# Bind address for the SSE transport. Default 127.0.0.1 (loopback only) — the
# server executes arbitrary LabTalk, so it must NOT be exposed to the LAN. A WSL
# client reaches it via WSL2 *mirrored* networking, which shares the Windows
# loopback (client connects to localhost:8000). Only override ORIGIN_MCP_HOST if
# you deliberately need another interface and have firewalled it accordingly.
_HOST = os.environ.get("ORIGIN_MCP_HOST", "127.0.0.1")
_PORT = int(os.environ.get("ORIGIN_MCP_PORT", "8000"))
mcp = FastMCP("Origin-MCP", host=_HOST, port=_PORT)


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


if __name__ == "__main__":
    # Legacy client-spawned entry point (e.g. Claude Desktop running
    # "python server.py stdio"). The Origin-managed sidecar does NOT use this
    # path — it goes through mcp_bootstrap.py, which binds to the correct Origin
    # instance and installs a watchdog before calling mcp.run().
    transport = sys.argv[1] if len(sys.argv) > 1 else "sse"
    if transport == "stdio":
        mcp.run(transport="stdio")
    else:
        print(f"Starting Origin MCP Server on SSE transport ({_HOST}:{_PORT})")
        mcp.run(transport="sse")
