"""Parsing helpers for ADI runtime outputs.

ADI no longer parses FLT from tracer stdout. The FLT protocol is serialized as
structured JSON (`flt.json`) and loaded by `executor.read_flt()`.

This module only retains error parsing utilities used by the CLI/server.
"""

from typing import Dict, List, Optional, Tuple


def parse_error_info(output: str, stderr: str, state: Optional[Dict] = None) -> Tuple[Optional[str], List[str]]:
    """Parse error information from tracer output and state.

    Args:
        output: Combined stdout+stderr from tracer (kept for backward compat; no longer parsed)
        stderr: stderr from tracer (kept for backward compat; no longer parsed)
        state: Optional state dict from state.json (read via executor.read_state()).

    Returns (error_message, candidate_methods).
    """
    error_msg: Optional[str] = None
    candidates: List[str] = []

    # Prefer structured error messages emitted by the tracer into state.json.
    if state:
        msg = (state.get("adi_error_message") or "").strip()
        if msg:
            return msg, []

    # Prefer state.json exception metadata when present (uncaught exception aborts execution).
    if state:
        exception_type = state.get('exception_type')
        exception_message = state.get('exception_message')
        exception_file = state.get('exception_file')
        exception_lineno = state.get('exception_lineno')
        exception_func = state.get('exception_func')
        if exception_type or exception_file or exception_lineno:
            msg = (exception_message or "").strip()
            if msg:
                msg = msg.splitlines()[0]
            msg_part = f": {msg}" if msg else ""
            loc_parts = []
            if exception_file:
                loc_parts.append(str(exception_file))
            if exception_lineno:
                loc_parts.append(str(exception_lineno))
            loc = ":".join(loc_parts) if loc_parts else "unknown location"
            func_part = f" ({exception_func})" if exception_func else ""
            curr_frame_id = state.get('curr_frame_id')
            if curr_frame_id is None:
                error_msg = (
                    f"Target frame was not reached: execution stopped due to uncaught "
                    f"{exception_type}{msg_part} at {loc}{func_part}"
                )
            else:
                error_msg = (
                    f"Execution stopped due to uncaught {exception_type}{msg_part} "
                    f"at {loc}{func_part}"
                )
            return error_msg, []

    # Check state.json for additional error info
    if state:
        curr_frame_id = state.get('curr_frame_id')
        candidate_method_names = state.get('candidate_method_names', [])
        frame_matched_count = state.get('frame_matched_count', 0)
        condition_matched_count = state.get('condition_matched_count', 0)

        # If no current frame was set, the target frame was not reached
        if curr_frame_id is None:
            # Distinguish between frame ID not found vs condition not satisfied
            if frame_matched_count > 0 and condition_matched_count == 0:
                # Frame ID matched but condition was never satisfied
                error_msg = "Target frame was not reached: condition never satisfied"
                candidates = []
                return error_msg, candidates
            elif candidate_method_names:
                candidates = candidate_method_names
                error_msg = "Target frame was not reached. Did you mean one of these methods?"
            else:
                error_msg = "Target frame was not reached during execution"
            return error_msg, candidates

    return error_msg, candidates
