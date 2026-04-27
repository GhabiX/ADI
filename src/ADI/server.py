"""ADI MCP Server - Main entry point"""

import json
import logging
from typing import Any, Dict, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

from .session import SessionManager, Session, FLT
from .executor import (
    cleanup_data_dir,
    execute_stmt_in_frame,
    execute_with_tracer,
    read_call_graph,
    read_exec_result,
    read_flt,
    read_state,
)
from .frame_id import resolve_frame_id as _resolve_frame_id
from .parser import parse_error_info

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("adi-mcp")

# Global session manager
session_manager = SessionManager()

# Create MCP server
server = Server("adi-mcp")


def flt_to_dict(flt: Optional[FLT]) -> Optional[Dict[str, Any]]:
    """Convert FLT object to dictionary for JSON serialization"""
    if not flt:
        return None
    return {
        "frame_id": flt.frame_id,
        "caller_frame_id": flt.caller_frame_id,
        "args": flt.args,
        "return_value": flt.return_value,
        "trace": [
            {
                "lineno": step.lineno,
                "stmt": step.stmt,
                "new_vars": step.new_vars,
                "modified_vars": step.modified_vars,
                "callee_frame_id": step.callee_frame_id,
                "iter_num": step.iter_num,
                "skipped_before": step.skipped_before,
            }
            for step in flt.trace
        ],
    }


def make_response(
    success: bool,
    flt: Optional[FLT] = None,
    error: Optional[str] = None,
    candidates: Optional[list] = None,
    **extra
) -> str:
    """Create a JSON response string"""
    result = {"success": success}
    if flt:
        result["flt"] = flt_to_dict(flt)
    if error:
        result["error"] = error
    if candidates:
        result["candidates"] = candidates
    result.update(extra)
    return json.dumps(result, indent=2)


def _parse_full_watch(raw) -> Optional[list]:
    if not raw:
        return None
    if isinstance(raw, list):
        items = raw
    else:
        items = [raw]
    names = []
    for item in items:
        for part in str(item).split(","):
            part = part.strip()
            if part:
                names.append(part)
    return names or None


def execute_and_parse(
    session: Session,
    frame_id: str,
    condition: Optional[str] = None,
    condition_when: str = "entry",
    timeout: int = 60,
    loop: int = 2,
    loop_index: Optional[int] = None,
    full_watch: Optional[list] = None,
    if_eval_lineno: Optional[int] = None,
    count_total_calls: bool = True,
    allow_external_target: bool = False,
) -> tuple[Optional[FLT], Optional[str], list]:
    """Execute tracer and parse results.

    Returns (flt, error_msg, candidates).
    """
    data_dir = None
    try:
        stdout, stderr, returncode, data_dir = execute_with_tracer(
            cmd=session.cmd,
            frame_id=frame_id,
            condition=condition,
            condition_when=condition_when,
            timeout=timeout,
            loop=loop,
            loop_index=loop_index,
            full_watch=full_watch,
            if_eval_lineno=if_eval_lineno,
            count_total_calls=count_total_calls,
            allow_external_target=allow_external_target,
        )

        # Read FLT from flt.json (strategy1).
        flt = read_flt(data_dir=data_dir)

        # Never silently treat tracer subprocess failures as "success".
        if returncode != 0:
            state = read_state(data_dir=data_dir)
            error_msg, candidates = parse_error_info(stdout, stderr, state)
            if "[ADI] timeout after" in (stderr or "") and not error_msg:
                error_msg = "Timeout while running traced command."
            if not error_msg:
                error_msg = f"Underlying process exited with return code {returncode}."
            # If we still got a partial FLT, return it alongside the error for debugging.
            return flt, error_msg, candidates

        # If no FLT, check for errors (including state.json)
        if not flt:
            state = read_state(data_dir=data_dir)
            error_msg, candidates = parse_error_info(stdout, stderr, state)
            return None, error_msg, candidates

        # Update session state
        session.curr_frame_id = flt.frame_id
        session.parent_frame_id = flt.caller_frame_id

        return flt, None, []
    finally:
        cleanup_data_dir(data_dir)


# ============ Tool Definitions ============

@server.list_tools()
async def list_tools():
    """List all available ADI tools"""
    return [
        Tool(
            name="adi_create_session",
            description="Create a new debug session for a command",
            inputSchema={
                "type": "object",
                "properties": {
                    "cmd": {
                        "type": "string",
                        "description": "The command to debug (e.g., 'python test.py')"
                    }
                },
                "required": ["cmd"]
            }
        ),
        Tool(
            name="adi_close_session",
            description="Close a debug session",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The session ID to close"
                    }
                },
                "required": ["session_id"]
            }
        ),
        Tool(
            name="adi_break",
            description="Set a breakpoint at a function and return its Frame Lifetime Trace (FLT)",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The session ID"
                    },
                    "func": {
                        "type": "string",
                        "description": "Function or line to break at (format: file_path:method_name or file_path:line_number)"
                    },
                    "index": {
                        "type": "integer",
                        "description": "Which invocation to break at (1-based, -1 for last)",
                        "default": 1
                    },
                    "condition": {
                        "type": "string",
                        "description": "Optional condition expression"
                    },
                    "condition_when": {
                        "type": "string",
                        "description": "Where to evaluate condition: entry|return|both (default: entry)",
                        "default": "entry"
                    },
                    "on_entry": {
                        "type": "boolean",
                        "description": "Alias: evaluate condition at function entry",
                        "default": False
                    },
                    "on_return": {
                        "type": "boolean",
                        "description": "Alias: evaluate condition at function return",
                        "default": False
                    },
                    "if_eval_lineno": {
                        "type": "integer",
                        "description": "Evaluate condition only when this line is executed (uses locals)"
                    },
                    "loop": {
                        "type": "integer",
                        "description": "Max loop iterations to show (default: 2)",
                        "default": 2
                    },
                    "loop_index": {
                        "type": "integer",
                        "description": "Only show specific loop iteration (1-based)"
                    },
                    "full_watch": {
                        "type": ["array", "string"],
                        "items": {"type": "string"},
                        "description": "Variable names to show (array or comma-separated string)"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds",
                        "default": 60
                    },
                    "no_count": {
                        "type": "boolean",
                        "description": "Exit immediately after capturing FLT (skip counting total calls)",
                        "default": False
                    },
                    "allow_external": {
                        "type": "boolean",
                        "description": "Allow targeting stdlib/site-packages files (override EXCLUDE_PATHS for target file only)",
                        "default": False
                    }
                },
                "required": ["session_id", "func"]
            }
        ),
        Tool(
            name="adi_clear",
            description="Clear the current breakpoint",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The session ID"
                    }
                },
                "required": ["session_id"]
            }
        ),
        Tool(
            name="adi_continue",
            description="Continue to the next breakpoint hit and return its FLT",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The session ID"
                    }
                },
                "required": ["session_id"]
            }
        ),
        Tool(
            name="adi_prev",
            description="Go back to the previous breakpoint hit and return its FLT",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The session ID"
                    }
                },
                "required": ["session_id"]
            }
        ),
        Tool(
            name="adi_step_into",
            description="Step into a specific frame and return its FLT",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The session ID"
                    },
                    "frame_id": {
                        "type": "string",
                        "description": "The frame ID to step into (format: file_path:method_name#index)"
                    }
                },
                "required": ["session_id", "frame_id"]
            }
        ),
        Tool(
            name="adi_step_out",
            description="Step out to the caller frame and return its FLT",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The session ID"
                    }
                },
                "required": ["session_id"]
            }
        ),
        Tool(
            name="adi_call_graph",
            description="Get 3-level call graph from current focus frame (schema: adi.call_graph v1)",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The session ID"
                    }
                },
                "required": ["session_id"]
            }
        ),
        Tool(
            name="adi_execute",
            description="Execute a Python statement at a specific frame and line number",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "The session ID"
                    },
                    "stmt": {
                        "type": "string",
                        "description": "Python statement to execute (e.g., 'print(x)')"
                    },
                    "lineno": {
                        "type": "integer",
                        "description": "Line number where to execute the statement"
                    },
                    "loop_index": {
                        "type": "integer",
                        "description": "Execute on Nth time the line is reached (default: 1)",
                        "default": 1
                    }
                },
                "required": ["session_id", "stmt", "lineno"]
            }
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: Dict[str, Any]):
    """Handle tool calls"""

    # ---- adi_create_session ----
    if name == "adi_create_session":
        cmd = arguments.get("cmd")
        if not cmd:
            return [TextContent(type="text", text=make_response(False, error="Missing 'cmd' parameter"))]

        session = session_manager.create_session(cmd)
        return [TextContent(
            type="text",
            text=make_response(True, session_id=session.session_id)
        )]

    # ---- adi_close_session ----
    if name == "adi_close_session":
        session_id = arguments.get("session_id")
        if not session_id:
            return [TextContent(type="text", text=make_response(False, error="Missing 'session_id' parameter"))]

        if session_manager.close_session(session_id):
            return [TextContent(type="text", text=make_response(True, message="Session closed"))]
        else:
            return [TextContent(type="text", text=make_response(False, error="Session not found"))]

    # ---- Get session for remaining tools ----
    session_id = arguments.get("session_id")
    if not session_id:
        return [TextContent(type="text", text=make_response(False, error="Missing 'session_id' parameter"))]

    session = session_manager.get_session(session_id)
    if not session:
        return [TextContent(type="text", text=make_response(False, error="Session not found"))]

    # ---- adi_break ----
    if name == "adi_break":
        func = arguments.get("func")
        if not func:
            return [TextContent(type="text", text=make_response(False, error="Missing 'func' parameter"))]

        index = arguments.get("index", 1)
        condition = arguments.get("condition")
        condition_when = arguments.get("condition_when", "entry")
        on_entry = bool(arguments.get("on_entry", False))
        on_return = bool(arguments.get("on_return", False))
        if on_entry and on_return:
            condition_when = "both"
        elif on_return and condition_when == "entry":
            condition_when = "return"
        elif on_entry and condition_when == "return":
            condition_when = "both"
        if condition_when not in ("entry", "return", "both"):
            return [TextContent(type="text", text=make_response(False, error="Invalid 'condition_when' (expected: entry/return/both)"))]
        if_eval_lineno = arguments.get("if_eval_lineno")
        if if_eval_lineno is not None and not condition:
            return [TextContent(type="text", text=make_response(False, error="'if_eval_lineno' requires 'condition'"))]
        loop = arguments.get("loop", 2)
        loop_index = arguments.get("loop_index")
        full_watch = _parse_full_watch(arguments.get("full_watch"))
        timeout = arguments.get("timeout", 60)
        no_count = bool(arguments.get("no_count", False))
        allow_external = bool(arguments.get("allow_external", False))

        # Build + resolve frame_id (support CLI-like file:line inputs)
        raw_frame_id = func if "#" in func else f"{func}#{index}"
        frame_id, _from_lineno, error_msg = _resolve_frame_id(raw_frame_id)
        if error_msg:
            return [TextContent(type="text", text=make_response(False, error=error_msg))]
        base, idx = frame_id.rsplit("#", 1)
        try:
            idx_int = int(idx)
        except ValueError:
            idx_int = index

        # Update session breakpoint info
        session.bp_func = base
        session.bp_index = idx_int
        session.bp_condition = condition
        session.bp_condition_when = condition_when
        session.bp_if_eval_lineno = if_eval_lineno
        session.bp_loop = int(loop)
        session.bp_loop_index = loop_index
        session.bp_full_watch = full_watch
        session.bp_timeout = int(timeout)
        session.bp_count_total_calls = not no_count
        session.bp_allow_external_target = allow_external

        # Execute and parse
        flt, error, candidates = execute_and_parse(
            session,
            frame_id,
            condition=condition,
            condition_when=condition_when,
            timeout=int(timeout),
            loop=int(loop),
            loop_index=loop_index,
            full_watch=full_watch,
            if_eval_lineno=if_eval_lineno,
            count_total_calls=not no_count,
            allow_external_target=allow_external,
        )

        if error:
            session.candidate_methods = candidates
            return [TextContent(type="text", text=make_response(False, flt=flt, error=error, candidates=candidates))]

        return [TextContent(type="text", text=make_response(True, flt=flt))]

    # ---- adi_clear ----
    if name == "adi_clear":
        session.bp_func = None
        session.bp_index = 1
        session.bp_condition = None
        session.bp_condition_when = "entry"
        session.bp_if_eval_lineno = None
        session.bp_loop = 2
        session.bp_loop_index = None
        session.bp_full_watch = None
        session.bp_timeout = 60
        session.bp_count_total_calls = True
        session.bp_allow_external_target = False
        return [TextContent(type="text", text=make_response(True, message="Breakpoint cleared"))]

    # ---- adi_continue ----
    if name == "adi_continue":
        if not session.bp_func:
            return [TextContent(type="text", text=make_response(False, error="No breakpoint set. Use adi_break first."))]

        # Increment index
        session.bp_index += 1
        frame_id = f"{session.bp_func}#{session.bp_index}"

        flt, error, candidates = execute_and_parse(
            session,
            frame_id,
            condition=session.bp_condition,
            condition_when=session.bp_condition_when,
            timeout=session.bp_timeout,
            loop=session.bp_loop,
            loop_index=session.bp_loop_index,
            full_watch=session.bp_full_watch,
            if_eval_lineno=session.bp_if_eval_lineno,
            count_total_calls=session.bp_count_total_calls,
            allow_external_target=session.bp_allow_external_target,
        )

        if error:
            # Revert index on failure
            session.bp_index -= 1
            return [TextContent(type="text", text=make_response(False, flt=flt, error=error, candidates=candidates))]

        return [TextContent(type="text", text=make_response(True, flt=flt))]

    # ---- adi_prev ----
    if name == "adi_prev":
        if not session.bp_func:
            return [TextContent(type="text", text=make_response(False, error="No breakpoint set. Use adi_break first."))]

        if session.bp_index <= 1:
            return [TextContent(type="text", text=make_response(False, error="Already at the first breakpoint"))]

        # Decrement index
        session.bp_index -= 1
        frame_id = f"{session.bp_func}#{session.bp_index}"

        flt, error, candidates = execute_and_parse(
            session,
            frame_id,
            condition=session.bp_condition,
            condition_when=session.bp_condition_when,
            timeout=session.bp_timeout,
            loop=session.bp_loop,
            loop_index=session.bp_loop_index,
            full_watch=session.bp_full_watch,
            if_eval_lineno=session.bp_if_eval_lineno,
            count_total_calls=session.bp_count_total_calls,
            allow_external_target=session.bp_allow_external_target,
        )

        if error:
            # Revert index on failure
            session.bp_index += 1
            return [TextContent(type="text", text=make_response(False, flt=flt, error=error, candidates=candidates))]

        return [TextContent(type="text", text=make_response(True, flt=flt))]

    # ---- adi_step_into ----
    if name == "adi_step_into":
        frame_id = arguments.get("frame_id")
        if not frame_id:
            return [TextContent(type="text", text=make_response(False, error="Missing 'frame_id' parameter"))]

        frame_id, _from_lineno, error_msg = _resolve_frame_id(frame_id)
        if error_msg:
            return [TextContent(type="text", text=make_response(False, error=error_msg))]

        flt, error, candidates = execute_and_parse(session, frame_id)

        if error:
            return [TextContent(type="text", text=make_response(False, flt=flt, error=error, candidates=candidates))]

        return [TextContent(type="text", text=make_response(True, flt=flt))]

    # ---- adi_step_out ----
    if name == "adi_step_out":
        if not session.parent_frame_id:
            return [TextContent(type="text", text=make_response(False, error="No caller frame available (already at top level)"))]

        flt, error, candidates = execute_and_parse(session, session.parent_frame_id)

        if error:
            return [TextContent(type="text", text=make_response(False, flt=flt, error=error, candidates=candidates))]

        return [TextContent(type="text", text=make_response(True, flt=flt))]

    # ---- adi_call_graph ----
    if name == "adi_call_graph":
        if not session.curr_frame_id:
            return [TextContent(type="text", text=make_response(False, error="No current frame. Use adi_break first."))]

        data_dir = None
        try:
            # Execute with call_graph_mode
            stdout, stderr, returncode, data_dir = execute_with_tracer(
                cmd=session.cmd,
                frame_id=session.curr_frame_id,
                condition=session.bp_condition,
                call_graph_mode=True,
            )

            # Read call graph data
            call_graph = read_call_graph(data_dir=data_dir)
            if not call_graph:
                return [TextContent(type="text", text=make_response(False, error="Failed to generate call graph"))]

            return [TextContent(type="text", text=make_response(True, call_graph=call_graph))]
        finally:
            cleanup_data_dir(data_dir)

    # ---- adi_execute ----
    if name == "adi_execute":
        if not session.curr_frame_id:
            return [TextContent(type="text", text=make_response(False, error="No current frame. Use adi_break first."))]

        stmt = arguments.get("stmt")
        lineno = arguments.get("lineno")
        if not stmt or lineno is None:
            return [TextContent(type="text", text=make_response(False, error="Missing 'stmt' or 'lineno' parameter"))]

        loop_index = arguments.get("loop_index", 1)

        data_dir = None
        try:
            stdout, stderr, returncode, data_dir = execute_stmt_in_frame(
                cmd=session.cmd,
                frame_id=session.curr_frame_id,
                stmt=stmt,
                lineno=lineno,
                loop_index=loop_index,
            )

            exec_result = read_exec_result(data_dir=data_dir)
            if exec_result is None:
                state = read_state(data_dir=data_dir)
                error_msg, _candidates = parse_error_info(stdout, stderr, state)
                error_msg = error_msg or "exec_result.txt not found (injected statement was not executed)."
                return [TextContent(type="text", text=make_response(False, error=error_msg))]

            return [TextContent(type="text", text=make_response(True, result=exec_result))]
        finally:
            cleanup_data_dir(data_dir)

    return [TextContent(type="text", text=make_response(False, error=f"Unknown tool: {name}"))]


async def main():
    """Main entry point for the MCP server"""
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())

def main_cli():
    """Sync entrypoint for console_scripts (wraps async main)."""
    import asyncio

    asyncio.run(main())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
