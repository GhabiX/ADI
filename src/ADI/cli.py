#!/usr/bin/env python3
"""ADI CLI - Simple command-line interface for ADI debugging tool"""

import argparse
import hashlib
import json
import os
import sys
import tempfile
import uuid
from typing import Optional, List, Dict

from .frame_id import (
    find_function_at_line as _find_function_at_line_impl,
    resolve_frame_id as _resolve_frame_id_impl,
)
from .executor import (
    execute_with_tracer,
    execute_stmt_in_frame,
    cleanup_data_dir,
    read_call_graph,
    read_exec_result,
    read_flt,
    read_state,
)
from .parser import parse_error_info

CALL_TREE_MAX_SIBLINGS = 10


# Breakpoint list file path (persistent across commands)
def _stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="surrogateescape")).hexdigest()[:16]


def _get_env_value(name: str) -> Optional[str]:
    value = os.environ.get(name)
    if value:
        return value
    return None


def _get_user_key() -> str:
    if hasattr(os, "getuid"):
        return str(os.getuid())
    return _stable_hash(os.environ.get("USER") or os.environ.get("USERNAME") or "unknown")


def _get_workspace_key() -> str:
    return _stable_hash(os.path.realpath(os.getcwd()))


def _get_session_key() -> Optional[str]:
    for env_name in ("ADI_SESSION_ID", "CODEX_THREAD_ID"):
        value = _get_env_value(env_name)
        if value:
            return _stable_hash(f"{env_name}={value}")
    return None


def _get_cli_data_dir() -> str:
    explicit_state_dir = _get_env_value("ADI_STATE_DIR")
    if explicit_state_dir:
        return explicit_state_dir
    explicit_data_dir = _get_env_value("ADI_DATA_DIR")
    if explicit_data_dir:
        return explicit_data_dir

    parts = [tempfile.gettempdir(), "adi-state", _get_user_key(), _get_workspace_key()]
    session_key = _get_session_key()
    if session_key:
        parts.append(session_key)
    return os.path.join(*parts)


def _get_breakpoints_path():
    """Get path to breakpoints.json file."""
    return os.path.join(_get_cli_data_dir(), 'adi_breakpoints.json')


def _get_last_state_path() -> str:
    return os.path.join(_get_cli_data_dir(), 'adi_last_state.json')


def _get_resolve_cache_path() -> str:
    return os.path.join(_get_cli_data_dir(), "adi_resolve_cache.json")


def _load_resolve_cache() -> Dict[str, str]:
    path = _get_resolve_cache_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: Dict[str, str] = {}
    for k, v in data.items():
        try:
            out[str(k)] = str(v)
        except Exception:
            continue
    return out


def _save_resolve_cache(cache: Dict[str, str]) -> None:
    path = _get_resolve_cache_path()
    try:
        _atomic_write_json(path, cache, ensure_ascii=False)
    except Exception:
        return


def _atomic_write_json(path: str, payload, *, ensure_ascii: bool = True) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=ensure_ascii)
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _save_last_state(state: Optional[Dict]):
    """Persist the last tracer state for `adi state` (survives per-run temp dir cleanup)."""
    # Best-effort: never fail the main command just because we cannot persist state.
    path = _get_last_state_path()
    try:
        payload = state if isinstance(state, dict) else {"adi_error_message": "No state available."}
        _atomic_write_json(path, payload, ensure_ascii=False)
    except Exception as exc:
        print(f"[ADI] Warning: failed to persist last tracer state to {path}: {exc}", file=sys.stderr)


def _load_last_state() -> Optional[Dict]:
    path = _get_last_state_path()
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _last_state_reached_frame(frame_id: str) -> bool:
    state = _load_last_state()
    if not state:
        return False
    return frame_id in {
        state.get('curr_frame_id'),
        state.get('focus_ended_frame_id'),
        state.get('focus_exception_frame_id'),
    }


def _load_breakpoints() -> Dict:
    """Load breakpoints from file."""
    path = _get_breakpoints_path()
    default_data = {
        'breakpoints': {},
        'current_index': 0,
        'current_frame_id': None,
        'caller_frame_id': None,
        'allow_external_target': False,
    }
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Warning: breakpoints file is invalid, resetting: {path}", file=sys.stderr)
            return default_data

        # Migrate old format (list) to new format (dict with conditions)
        if 'breakpoints' in data and isinstance(data['breakpoints'], list):
            # Convert old list format to new dict format
            old_breakpoints = data['breakpoints']
            data['breakpoints'] = {}
            for bp in old_breakpoints:
                data['breakpoints'][bp] = {
                    'condition': None,
                    'condition_when': 'entry',
                    'if_eval_lineno': None,
                    'any_call': False,
                }
        data.setdefault('breakpoints', {})
        data.setdefault('current_index', 0)
        data.setdefault('current_frame_id', None)
        data.setdefault('caller_frame_id', None)
        data.setdefault('allow_external_target', False)
        if isinstance(data.get('breakpoints'), dict):
            for bp_info in data['breakpoints'].values():
                if isinstance(bp_info, dict):
                    bp_info.setdefault('allow_external_target', False)
        return data
    return default_data


def _state_allows_external(args=None) -> bool:
    """Return whether the command should allow stdlib/site-packages target files."""
    if args is not None and bool(getattr(args, 'allow_external', False)):
        return True
    return bool(_load_breakpoints().get('allow_external_target'))


def _save_breakpoints(data: Dict):
    """Save breakpoints to file."""
    path = _get_breakpoints_path()
    _atomic_write_json(path, data)


def _find_function_at_line(file_path: str, line_num: int) -> Optional[str]:
    """Find the function containing a specific line number."""
    return _find_function_at_line_impl(file_path, line_num)


def _resolve_frame_id(frame_id: str) -> str:
    resolved, resolved_from_lineno, error_msg = _resolve_frame_id_impl(frame_id)
    if error_msg:
        print(f"Error: {error_msg}", file=sys.stderr)
        sys.exit(1)
    if resolved_from_lineno and resolved != frame_id:
        cache = _load_resolve_cache()
        cache_key = f"frame_id:{frame_id}"
        if cache.get(cache_key) != resolved:
            print(f"[resolve] {frame_id} -> {resolved}")
            cache[cache_key] = resolved
            _save_resolve_cache(cache)
    return resolved


def _parse_func_spec(spec):
    """Parse function specification: file:func or file:line format.

    Returns (file_path, func_name) or exits on error.
    """
    if ':' not in spec:
        print("Error: Invalid format '{}'. Expected: /path/file.py:func or /path/file.py:line".format(spec), file=sys.stderr)
        sys.exit(1)

    file_part, rest = spec.rsplit(':', 1)

    # Strip #N suffix if present
    if '#' in rest:
        rest = rest.split('#')[0]

    # Check if rest is a line number
    if rest.isdigit():
        line_num = int(rest)
        func_name = _find_function_at_line(file_part, line_num)
        if func_name:
            cache_key = f"func_spec:{file_part}:{line_num}"
            cache_val = f"{file_part}:{func_name}"
            cache = _load_resolve_cache()
            if cache.get(cache_key) != cache_val:
                print("[resolve] {}:{} -> {}:{}".format(file_part, line_num, file_part, func_name))
                cache[cache_key] = cache_val
                _save_resolve_cache(cache)
            return (os.path.abspath(file_part), func_name)
        else:
            print("Error: Line {} is not inside any function in {}".format(line_num, file_part), file=sys.stderr)
            sys.exit(1)

    return (os.path.abspath(file_part), rest)


def _format_args(args_dict):
    """Format args dict as 'key = value, key2 = value2'."""
    if not args_dict:
        return "()"
    parts = [f"{k} = {v}" for k, v in args_dict.items()]
    return ", ".join(parts)


def _parse_full_watch(raw: Optional[str]) -> Optional[List[str]]:
    if not raw:
        return None
    items = raw if isinstance(raw, list) else [raw]
    names = []
    for item in items:
        for part in str(item).split(','):
            part = part.strip()
            if part:
                names.append(part)
    return names or None


def _execute_and_print_flt(cmd: str, frame_id: str, condition: Optional[str] = None,
                           condition_when: str = 'entry', timeout: int = 60, loop: int = 2,
                           loop_index: Optional[int] = None, full_watch: Optional[List[str]] = None,
                           if_eval_lineno: Optional[int] = None, ignore_frame_index: bool = False,
                           count_total_calls: bool = True, allow_external_target: bool = False):
    """Execute tracer and print FLT. Returns (success, flt) tuple."""
    data_dir = None
    try:
        stdout, stderr, rc, data_dir = execute_with_tracer(
            cmd=cmd,
            frame_id=frame_id,
            condition=condition,
            condition_when=condition_when,
            timeout=timeout,
            loop=loop,
            loop_index=loop_index,
            full_watch=full_watch,
            if_eval_lineno=if_eval_lineno,
            ignore_frame_index=ignore_frame_index,
            count_total_calls=count_total_calls,
            allow_external_target=allow_external_target,
        )

        flt = read_flt(data_dir=data_dir)
        state = read_state(data_dir=data_dir)
        _save_last_state(state)

        # If the traced subprocess failed, do not silently treat this as success even if
        # we managed to parse a partial FLT from combined output.
        if flt and rc != 0:
            _print_flt(flt)
            error_msg, candidates = parse_error_info(stdout, stderr, state)
            if "[ADI] timeout after" in (stderr or ""):
                error_msg = error_msg or f"Timeout after {timeout}s."
            if not error_msg:
                error_msg = f"Underlying process exited with return code {rc}."
            print(f"Error: {error_msg}", file=sys.stderr)
            if stderr:
                tail = "\n".join((stderr or "").splitlines()[-60:])
                if tail.strip():
                    print("\n[stderr tail]", file=sys.stderr)
                    print(tail, file=sys.stderr)
            return False, None

        if flt:
            _print_flt(flt)
            # Show frame count stats if available
            if state:
                total_calls = state.get('total_calls')
                matched_index = state.get('matched_frame_index')
                if total_calls is not None and matched_index is not None:
                    print(f"stopped at frame #{matched_index} of {total_calls} total")
                if state.get('focus_ended_by_exception'):
                    suffix = ""
                    if not count_total_calls:
                        suffix = " (--no-count exits early; rerun without --no-count for full traceback)"
                    print(f"[ADI] Warning: focus call ended by exception{suffix}.", file=sys.stderr)
                    exc = state.get('focus_exception')
                    exc_file = state.get('focus_exception_file')
                    exc_lineno = state.get('focus_exception_lineno')
                    loc = f" at {exc_file}:{exc_lineno}" if exc and exc_file and exc_lineno else ""
                    if exc:
                        print(f"[ADI] Exception: {exc}{loc}", file=sys.stderr)
            # Update current frame info in breakpoints
            bp_data = _load_breakpoints()
            bp_data['current_frame_id'] = flt.frame_id
            bp_data['caller_frame_id'] = flt.caller_frame_id
            bp_data['allow_external_target'] = bool(allow_external_target)
            _save_breakpoints(bp_data)
            return True, flt

        # Check if there was an exception before reaching target frame
        exception_frame = state.get('exception_frame') if state else None
        exception_type = state.get('exception_type') if state else None
        exception_message = state.get('exception_message') if state else None
        exception_file = state.get('exception_file') if state else None
        exception_lineno = state.get('exception_lineno') if state else None
        exception_func = state.get('exception_func') if state else None

        if exception_frame:
            # Re-run with exception frame as target to get its FLT
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
                print(f"Target frame not reached. Uncaught {exception_type}{msg_part} at {loc}{func_part}")
            else:
                print(f"Target frame not reached. Exception occurred at: {exception_frame}")
            print("Re-tracing exception frame...")
            print()

            data_dir2 = None
            try:
                stdout2, stderr2, rc2, data_dir2 = execute_with_tracer(
                    cmd=cmd,
                    frame_id=exception_frame,
                    timeout=timeout,
                    allow_external_target=allow_external_target,
                )
                flt2 = read_flt(data_dir=data_dir2)
                state2 = read_state(data_dir=data_dir2)
                _save_last_state(state2)
            finally:
                cleanup_data_dir(data_dir2)
            if flt2 and rc2 != 0:
                _print_flt(flt2, is_exception=True)
                error_msg2, _candidates2 = parse_error_info(stdout2, stderr2, state2)
                if "[ADI] timeout after" in (stderr2 or ""):
                    error_msg2 = error_msg2 or f"Timeout after {timeout}s."
                if not error_msg2:
                    error_msg2 = f"Underlying process exited with return code {rc2}."
                print(f"Error: {error_msg2}", file=sys.stderr)
                if stderr2:
                    tail = "\n".join((stderr2 or "").splitlines()[-60:])
                    if tail.strip():
                        print("\n[stderr tail]", file=sys.stderr)
                        print(tail, file=sys.stderr)
                return False, None

            if flt2:
                _print_flt(flt2, is_exception=True)
                # Update current frame info
                bp_data = _load_breakpoints()
                bp_data['current_frame_id'] = flt2.frame_id
                bp_data['caller_frame_id'] = flt2.caller_frame_id
                bp_data['allow_external_target'] = bool(allow_external_target)
                _save_breakpoints(bp_data)
                return True, flt2

            print("Could not trace exception frame.", file=sys.stderr)
            return False, None

        if (
            state
            and state.get('curr_frame_id') is None
            and (exception_type or exception_file or exception_lineno)
        ):
            error_msg, _candidates = parse_error_info(stdout, stderr, state)
            print(f"Error: {error_msg or 'Target frame was not reached: execution stopped due to an uncaught exception.'}", file=sys.stderr)
            if exception_file and exception_lineno:
                loc = f"{exception_file}:{exception_lineno}"
                func_part = f" ({exception_func})" if exception_func else ""
                print(f"Hint: Uncaught exception at {loc}{func_part}.", file=sys.stderr)
                if if_eval_lineno is not None:
                    print(f"      Note: --if-eval-lineno {if_eval_lineno} may not have been executed before the exception.", file=sys.stderr)
                print("      If the line is inside a function, break there and use `adi exec -l` for line-level inspection.", file=sys.stderr)
            return False, None

        if "[ADI] timeout after" in (stderr or ""):
            print(f"Error: Timeout after {timeout}s.", file=sys.stderr)
            return False, None

        error_msg, candidates = parse_error_info(stdout, stderr, state)
        if error_msg:
            print(f"Error: {error_msg}", file=sys.stderr)
            if error_msg.startswith("Syntax error in condition"):
                print("Hint: Fix the --condition expression syntax.", file=sys.stderr)
                return False, None
            if error_msg.startswith("Variable not found in condition"):
                print("Hint: Entry/return conditions only see function arguments (and `_return` on return).", file=sys.stderr)
                if condition and "_return" in condition and condition_when in ("entry", "both"):
                    print("      If you need the return value, use --on-return.", file=sys.stderr)
                print("      If you need locals, use --if-eval-lineno to evaluate at a specific line.", file=sys.stderr)
                return False, None
            if error_msg.startswith("Condition evaluation failed:"):
                print("Hint: Condition evaluation raised an exception.", file=sys.stderr)
                print("      Consider simplifying the expression or using --if-eval-lineno.", file=sys.stderr)
                return False, None
            if "condition never satisfied" in error_msg:
                print("Hint: Conditions default to function entry/return and only see args.", file=sys.stderr)
                print("      Use --if-eval-lineno to evaluate at a line with locals.", file=sys.stderr)
                if ignore_frame_index:
                    print("      Add #N to target a specific call.", file=sys.stderr)
                else:
                    print("      Omit #N to evaluate the condition across all calls.", file=sys.stderr)
                return False, None

            # Check if user is already using correct format
            # Correct format: contains ':' (with or without '#', since _resolve_frame_id adds #1 if missing)
            is_correct_format = ':' in frame_id

            if is_correct_format:
                # Format is correct, issue is not format-related
                print("Hint: Frame not found. Common reasons:", file=sys.stderr)
                print("  - Function not called during execution", file=sys.stderr)
                print("  - Not called enough times (e.g., #3 but only called twice)", file=sys.stderr)
                if candidates:
                    print(f"  - Did you mean: {', '.join(candidates)}?", file=sys.stderr)
            else:
                # Format is incorrect, show format examples
                if candidates:
                    print(f"Did you mean: {', '.join(candidates)}?", file=sys.stderr)
                print("Frame ID format: /path/file.py:function#N or /path/file.py:line", file=sys.stderr)
            return False, None

        # Try to find candidate function names by running call_graph_mode
        candidates = _find_candidate_functions(cmd, frame_id, timeout)
        if candidates:
            print(f"Target frame not reached. Did you mean: {', '.join(candidates)}?", file=sys.stderr)
            if any('.' in c for c in candidates):
                print("Hint: Use ClassName.method format for class methods", file=sys.stderr)
        else:
            print("No FLT captured. Target frame may not have been reached.", file=sys.stderr)
            print("  Frame ID format: /path/file.py:function#N (e.g., main#1)", file=sys.stderr)
            print("  For class methods: ClassName.method#N", file=sys.stderr)
        return False, None
    finally:
        # Always cleanup the per-run temp data directory (avoids /tmp adi_* leaks).
        cleanup_data_dir(data_dir)


def _find_candidate_functions(cmd: str, frame_id: str, timeout: int = 60) -> List[str]:
    """Find candidate function names by running call_graph_mode."""
    # Extract function name from frame_id
    if ':' not in frame_id or '#' not in frame_id:
        return []

    func_part = frame_id.rsplit(':', 1)[-1]  # func#N or ClassName.method#N
    target_func = func_part.rsplit('#', 1)[0]  # func or ClassName.method

    # Get script path for entry point
    from .executor import describe_python_cmd
    try:
        spec = describe_python_cmd(cmd)
    except ValueError:
        return []
    if spec.mode != "script" or not spec.script_path:
        return []

    script_abs = spec.script_path
    entry_frame = f"{script_abs}:main#1"

    # Run call_graph_mode to collect all function names
    data_dir = None
    try:
        stdout, stderr, rc, data_dir = execute_with_tracer(
            cmd=cmd,
            frame_id=entry_frame,
            call_graph_mode=True,
            timeout=timeout,
        )

        call_graph = read_call_graph(data_dir=data_dir)
        if not call_graph:
            return []
    finally:
        cleanup_data_dir(data_dir)

    calls = call_graph.get("calls") if isinstance(call_graph, dict) else None
    if not isinstance(calls, list):
        return []

    # Extract all function names
    all_func_names = set()
    for entry in calls:
        if entry.get("kind") != "call":
            continue
        fid = entry.get("frame_id") or ""
        if ":" in fid and "#" in fid:
            func_name = fid.rsplit(":", 1)[-1].rsplit("#", 1)[0]
            if func_name:
                all_func_names.add(func_name)

    # Find partial matches
    candidates = []
    target_lower = target_func.lower()
    for func in all_func_names:
        func_lower = func.lower()
        # Match if target is substring or ends with .target
        if target_lower in func_lower or func_lower.endswith('.' + target_lower):
            candidates.append(func)

    return sorted(candidates)[:5]  # Return top 5 candidates


def cmd_break(args):
    """Set a breakpoint and show FLT."""
    # Resolve frame_id (supports both file:func#N and file:line formats)
    raw_frame_id = args.frame_id
    has_explicit_index = '#' in raw_frame_id
    frame_id = _resolve_frame_id(raw_frame_id)
    full_watch = _parse_full_watch(getattr(args, 'full_watch', None))
    if_eval_lineno = getattr(args, 'if_eval_lineno', None)
    condition = getattr(args, 'condition', None)
    if if_eval_lineno is not None and not condition:
        print("Error: --if-eval-lineno requires --condition", file=sys.stderr)
        sys.exit(1)
    ignore_frame_index = bool(condition) and not has_explicit_index

    # Validate frame_id format
    if ':' not in frame_id or '#' not in frame_id:
        print(f"Error: Invalid frame ID format: '{args.frame_id}'", file=sys.stderr)
        print(f"  Expected format: /path/file.py:function#N or /path/file.py:line", file=sys.stderr)
        print(f"  Examples:", file=sys.stderr)
        print(f"    /tmp/test.py:main#1", file=sys.stderr)
        print(f"    /tmp/test.py:237", file=sys.stderr)
        sys.exit(1)

    # Determine condition_when based on flags
    condition_when = 'entry'  # default
    if getattr(args, 'on_entry', False) and getattr(args, 'on_return', False):
        condition_when = 'both'
    elif getattr(args, 'on_return', False):
        condition_when = 'return'

    # Execute and show FLT first (may fail for invalid commands)
    success, _ = _execute_and_print_flt(
        cmd=args.cmd,
        frame_id=frame_id,
        condition=condition,
        condition_when=condition_when,
        timeout=args.timeout,
        loop=getattr(args, 'loop', 2),
        loop_index=getattr(args, 'loop_index', None),
        full_watch=full_watch,
        if_eval_lineno=if_eval_lineno,
        ignore_frame_index=ignore_frame_index,
        count_total_calls=not getattr(args, 'no_count', False),
        allow_external_target=bool(getattr(args, 'allow_external', False)),
    )

    # Only add to breakpoint list if execution succeeded
    if success:
        bp_data = _load_breakpoints()
        any_call_label = " (any-call)" if ignore_frame_index else ""
        allow_external_target = bool(getattr(args, 'allow_external', False))
        if frame_id not in bp_data['breakpoints']:
            bp_data['breakpoints'][frame_id] = {
                'condition': condition,
                'condition_when': condition_when,
                'if_eval_lineno': if_eval_lineno,
                'any_call': ignore_frame_index,
                'allow_external_target': allow_external_target,
            }
            print(f"\n[break] Added breakpoint: {frame_id}{any_call_label}")
        else:
            # Update existing breakpoint with new condition
            bp_data['breakpoints'][frame_id] = {
                'condition': condition,
                'condition_when': condition_when,
                'if_eval_lineno': if_eval_lineno,
                'any_call': ignore_frame_index,
                'allow_external_target': allow_external_target,
            }
            print(f"\n[break] Updated breakpoint: {frame_id}{any_call_label}")

        # Find index of this breakpoint (for backwards compatibility)
        bp_list = list(bp_data['breakpoints'].keys())
        bp_data['current_index'] = bp_list.index(frame_id)
        bp_data['allow_external_target'] = allow_external_target
        _save_breakpoints(bp_data)
    if not success:
        sys.exit(1)


def cmd_clear(args):
    """Clear all breakpoints."""
    bp_data = {
        'breakpoints': [],
        'current_index': 0,
        'current_frame_id': None,
        'caller_frame_id': None,
        'allow_external_target': False,
    }
    _save_breakpoints(bp_data)
    print("[clear] All breakpoints cleared.")


def cmd_continue(args):
    """Continue execution to the next breakpoint hit.

    Behavior:
    - Re-runs the program and stops at the next breakpoint hit after the current frame
    - Uses execution order (call sequence), not breakpoint list order
    """
    bp_data = _load_breakpoints()

    current_frame = bp_data.get('current_frame_id')
    if not current_frame:
        print("Error: No current frame. Use 'adi break' first.", file=sys.stderr)
        sys.exit(1)

    breakpoints = bp_data.get('breakpoints', {})

    if '#' not in current_frame:
        print(f"Error: Invalid frame ID format: {current_frame}", file=sys.stderr)
        sys.exit(1)

    base, index_str = current_frame.rsplit('#', 1)
    try:
        call_index = int(index_str)
    except ValueError:
        print(f"Error: Invalid frame index: {index_str}", file=sys.stderr)
        sys.exit(1)

    # Get condition info from the current breakpoint
    condition = None
    condition_when = 'entry'
    if_eval_lineno = None
    allow_external_target = bool(bp_data.get('allow_external_target'))
    for bp_frame_id, bp_info in breakpoints.items():
        bp_base = bp_frame_id.rsplit('#', 1)[0] if '#' in bp_frame_id else bp_frame_id
        if bp_base == base:
            condition = bp_info.get('condition')
            condition_when = bp_info.get('condition_when', 'entry')
            if_eval_lineno = bp_info.get('if_eval_lineno')
            allow_external_target = bool(bp_info.get('allow_external_target') or allow_external_target)
            break

    # Jump to next call of the same function
    next_call_index = call_index + 1
    next_frame_id = f"{base}#{next_call_index}"
    print(f"[continue] Jumping to next call: {next_frame_id}")
    print(f"[continue] Executing: {args.cmd}")

    print()
    full_watch = _parse_full_watch(getattr(args, 'full_watch', None))

    success, _ = _execute_and_print_flt(
        cmd=args.cmd,
        frame_id=next_frame_id,
        condition=condition,
        condition_when=condition_when,
        timeout=args.timeout,
        if_eval_lineno=getattr(args, 'if_eval_lineno', None) or if_eval_lineno,
        full_watch=full_watch,
        allow_external_target=allow_external_target,
    )
    if not success:
        print(file=sys.stderr)
        print("Hint: The frame was not found in this execution. Common reasons:", file=sys.stderr)
        print("  - The frame is from a different file/execution command", file=sys.stderr)
        print("  - The function was not called enough times in this execution", file=sys.stderr)
        print("  - ADI state is bound to the execution command used in 'adi break'", file=sys.stderr)
        print("  - To debug a different command, use 'adi break' first to set a new breakpoint", file=sys.stderr)
        sys.exit(1)


def cmd_step_in(args):
    """Step into a specific frame (navigation only, doesn't modify breakpoint list)."""
    frame_id = _resolve_frame_id(args.frame_id)
    full_watch = _parse_full_watch(getattr(args, 'full_watch', None))
    success, _ = _execute_and_print_flt(
        cmd=args.cmd,
        frame_id=frame_id,
        timeout=args.timeout,
        full_watch=full_watch,
        allow_external_target=_state_allows_external(args),
    )
    if success:
        print(f"\n[step-in] Stepped into: {frame_id}")
    else:
        if _last_state_reached_frame(frame_id):
            print(f"\n[step-in] Reached: {frame_id} (target exited non-zero)", file=sys.stderr)
        else:
            print(f"\n[step-in] Failed to step into: {frame_id}", file=sys.stderr)
        sys.exit(1)


def cmd_step_out(args):
    """Step out to the caller frame."""
    bp_data = _load_breakpoints()
    caller_frame_id = bp_data.get('caller_frame_id')

    if not caller_frame_id:
        print("Error: No caller frame available. Use 'adi break' or 'adi step-in' first.", file=sys.stderr)
        sys.exit(1)

    full_watch = _parse_full_watch(getattr(args, 'full_watch', None))
    success, _ = _execute_and_print_flt(
        cmd=args.cmd,
        frame_id=caller_frame_id,
        timeout=args.timeout,
        full_watch=full_watch,
        allow_external_target=_state_allows_external(args),
    )
    if success:
        print(f"\n[step-out] Stepped out to: {caller_frame_id}")
    else:
        if _last_state_reached_frame(caller_frame_id):
            print(f"\n[step-out] Reached caller frame: {caller_frame_id} (target exited non-zero)", file=sys.stderr)
        else:
            print(f"\n[step-out] Failed to step out to: {caller_frame_id}", file=sys.stderr)
        sys.exit(1)


def cmd_call_tree(args):
    """Get call tree from a specific frame."""
    frame_id = _resolve_frame_id(args.frame_id)
    data_dir = None
    try:
        stdout, stderr, rc, data_dir = execute_with_tracer(
            cmd=args.cmd,
            frame_id=frame_id,
            call_graph_mode=True,
            timeout=args.timeout,
            allow_external_target=_state_allows_external(args),
        )

        call_graph = read_call_graph(data_dir=data_dir)
        state = read_state(data_dir=data_dir)
        _save_last_state(state)
        if call_graph:
            _print_call_tree(call_graph)
            if rc != 0:
                error_msg, _candidates = parse_error_info(stdout, stderr, state)
                if "[ADI] timeout after" in (stderr or ""):
                    error_msg = error_msg or f"Timeout after {args.timeout}s."
                if not error_msg:
                    error_msg = f"Underlying process exited with return code {rc}."
                print(f"Error: {error_msg}", file=sys.stderr)
                sys.exit(1)
            return
        error_msg, _candidates = parse_error_info(stdout, stderr, state)
        if "[ADI] timeout after" in (stderr or ""):
            error_msg = error_msg or f"Timeout after {args.timeout}s."
        if error_msg:
            print(f"Error: {error_msg}", file=sys.stderr)
        else:
            print("No call tree captured.", file=sys.stderr)
        sys.exit(1)
    finally:
        cleanup_data_dir(data_dir)


def _print_flt(flt, is_exception=False):
    """Print FLT in formatted output."""
    # Header
    print(f"Frame: {flt.frame_id}")
    print(f"Caller: {flt.caller_frame_id or 'None'}")
    print(f"Args: {_format_args(flt.args)}")
    print()

    # Collect all values for hint detection
    all_values = list(flt.args.values())

    # Trace with formatted output
    # Find max line number width for alignment
    max_lineno = max((step.lineno for step in flt.trace), default=0)
    lineno_width = len(str(max_lineno))

    for step in flt.trace:
        # Print "Skipped" message before this step if it has skipped_before
        if step.skipped_before:
            print()
            print(f"------Skipped {step.skipped_before} iterations------")
            print()

        # Line number right-aligned, then |, then statement
        iter_marker = f" [iter #{step.iter_num}]" if step.iter_num else ""
        print(f"{step.lineno:>{lineno_width}} |{iter_marker} {step.stmt}")

        # Delta info with + for new, ~ for modified
        if step.new_vars:
            for var, val in step.new_vars.items():
                print(f"{' ' * lineno_width} |   + {var} = {val}")
                all_values.append(val)
        if step.modified_vars:
            for var, val in step.modified_vars.items():
                print(f"{' ' * lineno_width} |   ~ {var} = {val}")
                all_values.append(val)

        # Show callee frame ID for function calls (enables easy step-in)
        if step.callee_frame_id:
            print(f"{' ' * lineno_width} |   -> {step.callee_frame_id}")

    # Show skipped iterations info (loop cache) - before return value
    # Note: tracer.py already outputs this in the correct position (before last iteration)
    # so we don't need to output it again here
    # if flt.skipped_iterations:
    #     print()
    #     print(f"------Skipped {flt.skipped_iterations} iterations------")

    # Return value or exception indicator
    print()
    if is_exception or flt.return_value == 'None':
        # Check if last trace line is a raise statement
        last_stmt = flt.trace[-1].stmt if flt.trace else ''
        if 'raise' in last_stmt or is_exception:
            print(f"Exception: (see traceback above)")
        else:
            print(f"Return: {flt.return_value}")
    else:
        print(f"Return: {flt.return_value}")

    # Add return value to check
    if flt.return_value:
        all_values.append(flt.return_value)

    # Show hint if truncation or REPR FAILED detected
    if any('REPR FAILED' in str(v) or '[TRUNCATED...]' in str(v) for v in all_values):
        print()
        print("Tip: exec -s 'print(var)' for full values or use --full-watch var1,var2 for longer output")


def _truncate_call_graph_max_siblings(call_graph: List[dict], max_siblings: int) -> List[dict]:
    """Truncate call-tree by limiting direct children per node.

    This keeps output stable for LLM agents when a node has a huge number of
    same-depth siblings. We print the first N children (pre-order) and replace
    the rest of that node's subtree with a single marker line:

      |-- ... (+K more children omitted)
    """
    if not call_graph or max_siblings <= 0:
        return call_graph

    depths: List[int] = []
    for entry in call_graph:
        try:
            depths.append(int(entry.get("depth", 0) or 0))
        except Exception:
            depths.append(0)

    n = len(call_graph)
    parents: List[int] = [-1] * n
    stack: List[tuple[int, int]] = []
    for i, depth in enumerate(depths):
        while stack and stack[-1][0] >= depth:
            stack.pop()
        parents[i] = stack[-1][1] if stack else -1
        stack.append((depth, i))

    child_counts: List[int] = [0] * n
    for p in parents:
        if p >= 0:
            child_counts[p] += 1

    seen_children: List[int] = [0] * n
    out: List[dict] = []
    skip_parent_depth: Optional[int] = None

    for i, entry in enumerate(call_graph):
        depth = depths[i]

        if skip_parent_depth is not None:
            if depth > skip_parent_depth:
                continue
            skip_parent_depth = None

        parent = parents[i]
        if parent >= 0:
            seen_children[parent] += 1
            if child_counts[parent] > max_siblings and seen_children[parent] == max_siblings + 1:
                omitted = child_counts[parent] - max_siblings
                if omitted > 0:
                    out.append({"kind": "omitted", "depth": depth, "omitted_children": omitted})
                skip_parent_depth = depths[parent]
                continue

        out.append(entry)

    return out


def _shorten_frame_id(frame_id: str) -> str:
    if not frame_id:
        return ""
    if ":" in frame_id:
        parts = frame_id.rsplit("/", 1)
        return parts[-1] if len(parts) > 1 else frame_id
    return frame_id


def _print_call_tree(call_graph: dict):
    """Print call graph in tree format."""
    calls = (call_graph or {}).get("calls") or []
    if not isinstance(calls, list):
        calls = []
    calls = _truncate_call_graph_max_siblings(calls, CALL_TREE_MAX_SIBLINGS)
    # Parse call graph entries and build tree structure
    for i, entry in enumerate(calls):
        kind = entry.get("kind", "call")
        depth = entry.get("depth", 0)
        omitted_children = entry.get("omitted_children")
        is_last = _is_last_sibling(calls, i)

        # Build prefix based on depth
        prefix = _build_tree_prefix(calls, i, depth)

        if kind == "omitted" and omitted_children:
            branch = "|-- "
            print(f"{prefix}{branch}... (+{omitted_children} more children omitted)")
            continue

        if kind != "call":
            continue

        signature = entry.get("signature") or ""
        if not signature:
            continue

        ended_by_exception = bool(entry.get("ended_by_exception"))
        if ended_by_exception:
            return_value = "(exception)"
        else:
            raw_return_value = entry.get("return_value")
            return_value = raw_return_value if raw_return_value is not None else "None"

        # Print main line: func(args) -> return_value
        branch = "|-- "
        print(f"{prefix}{branch}{signature} -> {return_value}")

        # Print frame ID on next line
        continuation = "    " if is_last else "|   "
        frame_id_short = _shorten_frame_id(entry.get("frame_id") or "")
        print(f"{prefix}{continuation}    Frame: {frame_id_short}")

        # Add empty line between entries for readability (except last)
        if i < len(calls) - 1:
            next_depth = calls[i + 1].get("depth", 0)
            if next_depth <= depth:
                print(f"{prefix}{continuation}")


def _is_last_sibling(call_graph, index):
    """Check if entry at index is the last sibling at its depth."""
    if index >= len(call_graph) - 1:
        return True
    current_depth = call_graph[index].get('depth', 0)
    # Look ahead to see if there's another entry at same depth
    for j in range(index + 1, len(call_graph)):
        next_depth = call_graph[j].get('depth', 0)
        if next_depth < current_depth:
            return True  # Parent ended, so we're last
        if next_depth == current_depth:
            return False  # Found sibling
    return True


def _build_tree_prefix(call_graph, index, depth):
    """Build the tree prefix (vertical lines) for given depth."""
    if depth == 0:
        return ""

    prefix_parts = []
    for d in range(depth):
        # Check if there are more siblings at this depth level
        has_more = False
        for j in range(index + 1, len(call_graph)):
            j_depth = call_graph[j].get('depth', 0)
            if j_depth < d:
                break
            if j_depth == d:
                has_more = True
                break
        prefix_parts.append("|   " if has_more else "    ")

    return "".join(prefix_parts)


def cmd_exec(args):
    """Execute a statement at a specific frame and line."""
    frame_id = _resolve_frame_id(args.frame_id)

    # Get statement from -s or -f
    if args.file:
        if not os.path.exists(args.file):
            print(f"Error: File not found: {args.file}", file=sys.stderr)
            sys.exit(1)
        with open(args.file, 'r') as f:
            stmt = f.read()
    else:
        # Handle escaped quotes from shell - convert \" to "
        # This allows agent to use: -s 'print(f"x={x}, has attr: {hasattr(x, \"foo\")}")'
        stmt = args.stmt.replace('\\"', '"').replace("\\'", "'")

    # Line number: use provided value or None (executor will use function exit)
    lineno = args.line

    data_dir = None
    try:
        stdout, stderr, rc, data_dir = execute_stmt_in_frame(
            cmd=args.cmd,
            frame_id=frame_id,
            stmt=stmt,
            lineno=lineno,
            loop_index=args.loop_index,
            timeout=args.timeout,
            allow_external_target=_state_allows_external(args),
        )

        state = read_state(data_dir=data_dir)
        _save_last_state(state)
        exec_result = read_exec_result(data_dir=data_dir)
        if exec_result is None:
            if "[ADI] timeout after" in (stderr or ""):
                print(f"Error: Timeout after {args.timeout}s.", file=sys.stderr)
            else:
                print("Error: exec_result.txt not found (injected statement was not executed).", file=sys.stderr)
                detail_msg, _candidates = parse_error_info(stdout, stderr, state)
                if detail_msg:
                    print(f"Detail: {detail_msg}", file=sys.stderr)
                print("Hint: Check frame_id / -l / --loop-index and ensure the target reaches that point.", file=sys.stderr)
            sys.exit(rc if rc != 0 else 1)

        print("[exec output]")
        print(exec_result)

        if rc != 0:
            sys.exit(rc)
    finally:
        cleanup_data_dir(data_dir)


def cmd_state(args):
    """Show current tracer state."""
    path = _get_last_state_path()
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Error: Failed to read last state file: {path}: {exc}", file=sys.stderr)
            sys.exit(1)
        print(json.dumps(state, indent=2, ensure_ascii=False))
        return

    state = read_state()
    if state:
        print(json.dumps(state, indent=2, ensure_ascii=False))
        return

    print(
        "No state available. Run a tracer command first (break/continue/step-in/step-out/call-tree/diff/exec).",
        file=sys.stderr,
    )
    sys.exit(1)


def cmd_diff(args):
    """Compare two frames of the same function."""
    frame1 = _resolve_frame_id(args.frame1)
    frame2 = _resolve_frame_id(args.frame2)
    full_watch = _parse_full_watch(getattr(args, 'full_watch', None))

    data_dir1 = None
    data_dir2 = None
    try:
        # Run tracer twice to collect both FLTs
        stdout1, stderr1, rc1, data_dir1 = execute_with_tracer(
            cmd=args.cmd,
            frame_id=frame1,
            timeout=args.timeout,
            full_watch=full_watch,
            allow_external_target=_state_allows_external(args),
        )
        flt1 = read_flt(data_dir=data_dir1)
        state1 = read_state(data_dir=data_dir1)
        _save_last_state(state1)

        stdout2, stderr2, rc2, data_dir2 = execute_with_tracer(
            cmd=args.cmd,
            frame_id=frame2,
            timeout=args.timeout,
            full_watch=full_watch,
            allow_external_target=_state_allows_external(args),
        )
        flt2 = read_flt(data_dir=data_dir2)
        state2 = read_state(data_dir=data_dir2)
        _save_last_state(state2)

        if not flt1:
            print(f"Error: Could not trace frame {frame1}", file=sys.stderr)
            sys.exit(1)
        if not flt2:
            print(f"Error: Could not trace frame {frame2}", file=sys.stderr)
            sys.exit(1)

        _print_flt_diff(flt1, flt2)
        if rc1 != 0 or rc2 != 0:
            if rc1 != 0:
                error_msg1, _candidates1 = parse_error_info(stdout1, stderr1, state1)
                if "[ADI] timeout after" in (stderr1 or ""):
                    error_msg1 = error_msg1 or f"Timeout after {args.timeout}s."
                if not error_msg1:
                    error_msg1 = f"Underlying process exited with return code {rc1}."
                print(f"Error: {error_msg1}", file=sys.stderr)
            if rc2 != 0:
                error_msg2, _candidates2 = parse_error_info(stdout2, stderr2, state2)
                if "[ADI] timeout after" in (stderr2 or ""):
                    error_msg2 = error_msg2 or f"Timeout after {args.timeout}s."
                if not error_msg2:
                    error_msg2 = f"Underlying process exited with return code {rc2}."
                if rc1 == 0 or error_msg2 != error_msg1:
                    print(f"Error: {error_msg2}", file=sys.stderr)
            sys.exit(1)
    finally:
        cleanup_data_dir(data_dir2)
        cleanup_data_dir(data_dir1)


def _print_flt_diff(flt1, flt2):
    """Print diff between two FLTs."""
    # Extract frame indices
    idx1 = flt1.frame_id.split('#')[-1] if '#' in flt1.frame_id else '?'
    idx2 = flt2.frame_id.split('#')[-1] if '#' in flt2.frame_id else '?'

    print(f"=== Frame Diff: #{idx1} vs #{idx2} ===")
    print()

    # Args diff
    print("Args:")
    all_args = set(flt1.args.keys()) | set(flt2.args.keys())
    has_diff = False
    for arg in sorted(all_args):
        v1 = flt1.args.get(arg, '<missing>')
        v2 = flt2.args.get(arg, '<missing>')
        if v1 != v2:
            print(f"  {arg}: {v1} -> {v2}")
            has_diff = True
    if not has_diff:
        print("  (no differences)")

    # Return diff
    print()
    print("Return:")
    if flt1.return_value != flt2.return_value:
        print(f"  #{idx1}: {flt1.return_value}")
        print(f"  #{idx2}: {flt2.return_value}")
    else:
        print(f"  (same) {flt1.return_value}")

    # Trace diff - compare executed lines
    print()
    print("Trace:")
    lines1 = set(step.lineno for step in flt1.trace)
    lines2 = set(step.lineno for step in flt2.trace)
    only_in_1 = lines1 - lines2
    only_in_2 = lines2 - lines1
    if only_in_1 or only_in_2:
        if only_in_1:
            print(f"  Only in #{idx1}: lines {sorted(only_in_1)}")
        if only_in_2:
            print(f"  Only in #{idx2}: lines {sorted(only_in_2)}")
    else:
        print(f"  (same lines executed: {len(lines1)} lines)")


def cmd_list(args):
    """List all breakpoints."""
    bp_data = _load_breakpoints()
    if not bp_data['breakpoints']:
        print("No breakpoints set.")
        return

    print("Breakpoints:")
    for i, (bp, bp_info) in enumerate(bp_data['breakpoints'].items()):
        marker = " *" if i == bp_data['current_index'] else ""
        any_call = bp_info.get('any_call')
        suffix = " (any-call)" if any_call else ""
        print(f"  {i + 1}. {bp}{suffix}{marker}")

    if bp_data.get('current_frame_id'):
        print(f"\nCurrent frame: {bp_data['current_frame_id']}")
    if bp_data.get('caller_frame_id'):
        print(f"Caller frame: {bp_data['caller_frame_id']}")


def _collect_frames_lightweight(spec, target_specs, timeout, allow_external_target=False):
    """Collect frames for specified (file_path, func_name) specs using lightweight tracing.

    Args:
        spec: CommandSpec parsed from the command string
        target_specs: List of (file_path, func_name) tuples
        allow_external_target: Accepted for CLI contract parity with action commands.
            Lightweight list-frames traces only explicit target files, so no additional
            filtering override is needed here.
    """
    import tempfile
    script_dir = os.path.dirname(spec.script_path) or "." if spec.script_path else None
    target_specs_list = list(target_specs)

    from .executor import DBGTOOL_PATH, build_exec_snippet, build_sys_argv
    dbgtool_path = os.environ.get('ADI_DBGTOOL_PATH', str(DBGTOOL_PATH.parent))
    sys_argv = build_sys_argv(spec)
    exec_snippet = build_exec_snippet(spec)
    exec_snippet = "\n".join(
        f"    {line}" if line else "" for line in exec_snippet.splitlines()
    )

    tracer_code = '''
import sys
import os
import atexit
import runpy

# IMPORTANT: Keep all default paths for module imports
# Insert script_dir and current working directory at the beginning
sys.path.insert(0, os.getcwd())  # Current working directory (e.g., /testbed)
{script_dir_insert}
sys.path.insert(0, {dbgtool_path!r})  # dbgtool path for _derive_method_name

# Import _derive_method_name from dbgtool for consistent method name resolution
from dbgtool.utils import _derive_method_name

# Target specs: list of (file_path, func_name) tuples
TARGET_SPECS = {target_specs_list!r}
TARGET_FILES = set(path for path, _ in TARGET_SPECS)
TARGET_BY_FILE = {{}}
TARGET_METHOD_NAMES = set()
for file_path, func_name in TARGET_SPECS:
    targets = TARGET_BY_FILE.setdefault(file_path, [])
    if '.' in func_name:
        target_class, target_method = func_name.rsplit('.', 1)
    else:
        target_class, target_method = None, func_name
    TARGET_METHOD_NAMES.add(target_method)
    targets.append((func_name, target_class, target_method))
TARGET_BASENAMES = set(os.path.basename(path) for path in TARGET_FILES if path)

ABSPATH_CACHE = {{}}
BASENAME_CACHE = {{}}
METHOD_NAME_CACHE = {{}}
MRO_NAME_CACHE = {{}}

# Results
frames = []  # (order, line_frame_id, func_name, caller_info, lineno, canonical_frame_id)
line_call_counter = {{}}
method_call_counter = {{}}
call_order = [0]
_trace_error = None

def trace_calls(frame, event, arg):
    if event != 'call':
        return trace_calls

    code = frame.f_code
    if TARGET_METHOD_NAMES and code.co_name not in TARGET_METHOD_NAMES:
        return trace_calls
    filename = code.co_filename
    if filename and not filename.startswith('<'):
        cached_base = BASENAME_CACHE.get(filename)
        if cached_base is None:
            cached_base = os.path.basename(filename)
            BASENAME_CACHE[filename] = cached_base
        if cached_base not in TARGET_BASENAMES:
            return trace_calls
        cached = ABSPATH_CACHE.get(filename)
        if cached is None:
            cached = os.path.abspath(filename)
            ABSPATH_CACHE[filename] = cached
        filename = cached
    else:
        return trace_calls

    if filename not in TARGET_FILES:
        return trace_calls

    func_name = METHOD_NAME_CACHE.get(code)
    if func_name is None:
        func_name = _derive_method_name(frame)
        METHOD_NAME_CACHE[code] = func_name

    # Check if this function matches any target (file_path, func_name)
    matched = False
    for target_func, target_class, target_method in TARGET_BY_FILE.get(filename, []):
        # Case 1: Exact match
        if func_name == target_func:
            matched = True
            break

        # Case 2: target is just method name, func_name is Class.method
        if func_name.endswith('.' + target_func):
            matched = True
            break

        # Case 3: Both are class methods - check inheritance via MRO
        if target_class and '.' in func_name:
            actual_method = func_name.rsplit('.', 1)[1]
            if target_method != actual_method:
                continue
            try:
                cls_obj = None
                if 'self' in frame.f_locals:
                    cls_obj = frame.f_locals['self'].__class__
                elif 'cls' in frame.f_locals:
                    cls_obj = frame.f_locals['cls']
                if cls_obj is not None and hasattr(cls_obj, '__mro__'):
                    mro_names = MRO_NAME_CACHE.get(cls_obj)
                    if mro_names is None:
                        mro_names = [c.__name__ for c in cls_obj.__mro__]
                        MRO_NAME_CACHE[cls_obj] = mro_names
                    if target_class in mro_names:
                        matched = True
                        break
            except Exception:
                pass

    if matched:
        # Count line and method targets separately. The method target mirrors the
        # full tracer's runtime-derived frame_id and is the recommended
        # copy-paste target for break/exec/call-tree. The line target remains a
        # useful fallback and display aid.
        lineno = frame.f_code.co_firstlineno
        line_key = "{{0}}:{{1}}".format(filename, lineno)
        line_call_counter[line_key] = line_call_counter.get(line_key, 0) + 1
        line_idx = line_call_counter[line_key]

        method_key = "{{0}}:{{1}}".format(filename, func_name)
        method_call_counter[method_key] = method_call_counter.get(method_key, 0) + 1
        method_idx = method_call_counter[method_key]

        line_frame_id = "{{0}}:{{1}}#{{2}}".format(filename, lineno, line_idx)
        canonical_frame_id = "{{0}}:{{1}}#{{2}}".format(filename, func_name, method_idx)
        call_order[0] += 1

        # Get caller info
        caller_info = ""
        caller_frame = frame.f_back
        if caller_frame:
            caller_file = os.path.basename(caller_frame.f_code.co_filename)
            caller_func = caller_frame.f_code.co_name
            caller_line = caller_frame.f_lineno
            caller_info = "{{0}}:{{1}}:{{2}}".format(caller_file, caller_func, caller_line)

        frames.append((call_order[0], line_frame_id, func_name, caller_info, lineno, canonical_frame_id))

    return trace_calls

# Setup - keep current working directory for proper module resolution
sys.argv = {script_argv!r}
# NOTE: Do NOT change working directory - keep the original cwd for proper module resolution

# Output function - registered with atexit to handle sys.exit() in target script
def _output_frames():
    import json
    print("__ADI_FRAMES_START__")
    print(json.dumps({{"frames": frames, "error": _trace_error}}))
    print("__ADI_FRAMES_END__")

atexit.register(_output_frames)

# Start profiling (call/return only)
sys.setprofile(trace_calls)

try:
{exec_snippet}
except SystemExit:
    pass  # Let atexit handle output
except Exception as e:
    _trace_error = str(e)
finally:
    sys.setprofile(None)
'''.format(
        script_dir_insert=(f"sys.path.insert(0, {script_dir!r})" if script_dir else ""),
        dbgtool_path=dbgtool_path,
        target_specs_list=target_specs_list,
        script_argv=sys_argv,
        exec_snippet=exec_snippet,
    )

    # Write tracer script to temp file
    fd, tracer_path = tempfile.mkstemp(suffix='.py', prefix='adi_list_frames_')
    os.close(fd)

    try:
        with open(tracer_path, 'w') as f:
            f.write(tracer_code)

        # Build argv: use the same python interpreter+flags as the original command.
        tracer_argv = list(spec.python_argv) + [tracer_path]

        # Run the tracer with inherited environment and current working directory.
        # Use a new session and kill the process group on timeout to avoid orphan processes.
        env = os.environ.copy()
        env.update(getattr(spec, "env", {}) or {})
        from .executor import _run_argv_with_timeout
        result_stdout, result_stderr, _rc, timed_out = _run_argv_with_timeout(
            tracer_argv,
            timeout=timeout,
            env=env,
            cwd=os.getcwd(),  # Preserve current working directory
        )
        if timed_out:
            print("Timeout while collecting frames.", file=sys.stderr)
            return None

        # Parse output
        output = result_stdout + result_stderr
        if "__ADI_FRAMES_START__" in output and "__ADI_FRAMES_END__" in output:
            start = output.find("__ADI_FRAMES_START__") + len("__ADI_FRAMES_START__")
            end = output.find("__ADI_FRAMES_END__")
            json_str = output[start:end].strip()
            data = json.loads(json_str)
            data['all_func_names'] = set(data.get('all_func_names', []))
            return data

        return {'frames': [], 'all_func_names': set()}

    except Exception as e:
        print(f"Error collecting frames: {e}", file=sys.stderr)
        return None
    finally:
        if os.path.exists(tracer_path):
            os.remove(tracer_path)


def cmd_list_frames(args):
    """List all frames for specified function:func or file:line specs."""
    MAX_DISPLAY = 100

    from .executor import describe_python_cmd
    try:
        cmd_spec = describe_python_cmd(args.cmd)
    except ValueError:
        print("Error: Cannot parse Python command.", file=sys.stderr)
        sys.exit(1)

    # Parse function specs to extract (file_path, func_name) tuples
    target_specs = []  # List of (file_path, func_name)
    for func_spec in args.func_names:
        target_specs.append(_parse_func_spec(func_spec))

    # Use lightweight frame collection
    frames_data = _collect_frames_lightweight(
        cmd_spec,
        target_specs,
        args.timeout,
        allow_external_target=bool(getattr(args, 'allow_external', False)),
    )

    if frames_data is None:
        print("Failed to collect frames.", file=sys.stderr)
        sys.exit(1)

    # Check for execution errors
    trace_error = frames_data.get('error')
    if trace_error:
        print("Warning: Script execution error: {}".format(trace_error), file=sys.stderr)
        print("  Some functions may not have been traced.", file=sys.stderr)
        print()

    all_frames = frames_data.get('frames', [])

    if not all_frames:
        print("No matching frames found.", file=sys.stderr)
        print("  Make sure the function is called during execution.", file=sys.stderr)
        sys.exit(1)

    # Filter by caller if specified
    caller_filter = getattr(args, 'caller', None)
    if caller_filter:
        all_frames = [f for f in all_frames if len(f) > 3 and caller_filter in f[3]]
        if not all_frames:
            print(f"No frames found with caller matching '{caller_filter}'.", file=sys.stderr)
            sys.exit(1)

    # Sort by call order
    all_frames.sort(key=lambda x: x[0])

    # Display results with caller info
    total_frames = len(all_frames)
    print(f"Frames ({total_frames} total):" if total_frames <= MAX_DISPLAY else f"Frames (showing first {MAX_DISPLAY} of {total_frames}):")
    for i, frame_data in enumerate(all_frames[:MAX_DISPLAY]):
        order, line_frame_id, func_name = frame_data[0], frame_data[1], frame_data[2]
        caller_info = frame_data[3] if len(frame_data) > 3 else ""
        lineno = frame_data[4] if len(frame_data) > 4 else None
        canonical_frame_id = frame_data[5] if len(frame_data) > 5 else line_frame_id

        # Extract short frame id - use lineno format with method name as reference
        _file_func, _line_idx_str = line_frame_id.rsplit('#', 1)
        _canonical_file_func, idx_str = canonical_frame_id.rsplit('#', 1)
        if lineno:
            short_id = f"{lineno}#{idx_str} ({func_name})"
        else:
            short_id = f"{func_name}#{idx_str}"

        if caller_info:
            print(f"  #{idx_str:<4} {short_id:<40} target: {canonical_frame_id} line-target: {line_frame_id} <- {caller_info}")
        else:
            print(f"  #{idx_str:<4} {short_id:<40} target: {canonical_frame_id} line-target: {line_frame_id}")

    if total_frames > MAX_DISPLAY:
        print(f"  ... and {total_frames - MAX_DISPLAY} more")

    # Summary by function
    print("\nSummary:")
    func_counts = {}
    for frame_data in all_frames:
        line_frame_id, func_name = frame_data[1], frame_data[2]
        canonical_frame_id = frame_data[5] if len(frame_data) > 5 else line_frame_id
        if func_name not in func_counts:
            func_counts[func_name] = []
        # Extract index from frame_id
        idx = int(canonical_frame_id.rsplit('#', 1)[-1])
        func_counts[func_name].append(idx)

    for func_name in sorted(func_counts.keys()):
        indices = sorted(func_counts[func_name])
        count = len(indices)
        if count == 1:
            print(f"  {func_name}: 1 call (#{indices[0]})")
        else:
            print(f"  {func_name}: {count} calls (#{indices[0]}-#{indices[-1]})")


def main():
    parser = argparse.ArgumentParser(
        prog='adi',
        description='ADI - Agent-centric Debugging Interface CLI',
    )
    subparsers = parser.add_subparsers(dest='command')
    subparsers.required = True  # Python 3.6 compatible way

    # === Breakpoint commands ===

    # break command
    p_break = subparsers.add_parser('break', help='Set a breakpoint and show FLT')
    p_break.add_argument('cmd', help='Python command to run (e.g., "python script.py")')
    p_break.add_argument('frame_id', help='Frame ID: "/path/file.py:func#1" or "/path/file.py:237"')
    p_break.add_argument('--condition', '-c', help='Break condition (can use _return for return value)')
    p_break.add_argument('--on-entry', action='store_true', help='Check condition at function entry (default)')
    p_break.add_argument('--on-return', action='store_true', help='Check condition at function return (can use _return)')
    p_break.add_argument('--if-eval-lineno', type=int, help='Evaluate condition only when this line is executed (uses locals)')
    p_break.add_argument('--loop', type=int, default=2, help='Max loop iterations to show (default: 2)')
    p_break.add_argument('--loop-index', type=int, help='Only show specific loop iteration (1-based)')
    p_break.add_argument('--full-watch', help='Comma-separated variable names to show (filters FLT output, 600-char cap)')
    p_break.add_argument('--no-count', action='store_true', help='Exit immediately after capturing FLT (skip counting total calls)')
    p_break.add_argument('--allow-external', action='store_true', help='Allow targeting stdlib/site-packages files (override EXCLUDE_PATHS for target file only)')
    p_break.add_argument('--timeout', '-t', type=int, default=60, help='Timeout in seconds')
    p_break.set_defaults(func=cmd_break)

    # clear command
    p_clear = subparsers.add_parser('clear', help='Clear all breakpoints')
    p_clear.set_defaults(func=cmd_clear)

    # continue command
    p_cont = subparsers.add_parser('continue', help='Continue to next breakpoint')
    p_cont.add_argument('cmd', help='Python command to run')
    p_cont.add_argument('--if-eval-lineno', type=int, help='Evaluate condition only when this line is executed (uses locals)')
    p_cont.add_argument('--full-watch', help='Comma-separated variable names to show (filters FLT output, 600-char cap)')
    p_cont.add_argument('--timeout', '-t', type=int, default=60, help='Timeout in seconds')
    p_cont.set_defaults(func=cmd_continue)

    # list command
    p_list = subparsers.add_parser('list', help='List all breakpoints')
    p_list.set_defaults(func=cmd_list)

    # === Navigation commands ===

    # step-in command
    p_step_in = subparsers.add_parser('step-in', help='Step into a specific frame')
    p_step_in.add_argument('cmd', help='Python command to run')
    p_step_in.add_argument('frame_id', help='Frame ID: "/path/file.py:func#1" or "/path/file.py:237"')
    p_step_in.add_argument('--full-watch', help='Comma-separated variable names to show (filters FLT output, 600-char cap)')
    p_step_in.add_argument('--allow-external', action='store_true', help='Allow targeting stdlib/site-packages files (override EXCLUDE_PATHS for target file only)')
    p_step_in.add_argument('--timeout', '-t', type=int, default=60, help='Timeout in seconds')
    p_step_in.set_defaults(func=cmd_step_in)

    # step-out command
    p_step_out = subparsers.add_parser('step-out', help='Step out to caller frame')
    p_step_out.add_argument('cmd', help='Python command to run')
    p_step_out.add_argument('--full-watch', help='Comma-separated variable names to show (filters FLT output, 600-char cap)')
    p_step_out.add_argument('--allow-external', action='store_true', help='Allow targeting stdlib/site-packages files (override EXCLUDE_PATHS for target file only)')
    p_step_out.add_argument('--timeout', '-t', type=int, default=60, help='Timeout in seconds')
    p_step_out.set_defaults(func=cmd_step_out)

    # === Auxiliary commands ===

    # call-tree command (renamed from callgraph)
    p_ct = subparsers.add_parser('call-tree', help='Get call tree from a frame')
    p_ct.add_argument('cmd', help='Python command to run')
    p_ct.add_argument('frame_id', help='Frame ID: "/path/file.py:func#1" or "/path/file.py:237"')
    p_ct.add_argument('--allow-external', action='store_true', help='Allow targeting stdlib/site-packages files (override EXCLUDE_PATHS for target file only)')
    p_ct.add_argument('--timeout', '-t', type=int, default=60, help='Timeout in seconds')
    p_ct.set_defaults(func=cmd_call_tree)

    # list-frames command
    p_lf = subparsers.add_parser('list-frames', help='List frames for specified file:func or file:line specs')
    p_lf.add_argument('cmd', help='Python command to run')
    p_lf.add_argument('func_names', nargs='+', help='Function specs: /path/file.py:func or /path/file.py:line')
    p_lf.add_argument('--caller', help='Filter frames by caller (substring match)')
    p_lf.add_argument('--allow-external', action='store_true', help='Allow listing stdlib/site-packages target files (contract parity with break/exec/call-tree)')
    p_lf.add_argument('--timeout', '-t', type=int, default=60, help='Timeout in seconds')
    p_lf.set_defaults(func=cmd_list_frames)

    # exec command
    p_exec = subparsers.add_parser('exec', help='Execute statement at a frame/line')
    p_exec.add_argument('cmd', help='Python command to run')
    p_exec.add_argument('frame_id', help='Frame ID: "/path/file.py:func#1" or "/path/file.py:237"')
    exec_group = p_exec.add_mutually_exclusive_group(required=True)
    exec_group.add_argument('--stmt', '-s', help='Statement to execute')
    exec_group.add_argument('--file', '-f', help='File containing code to execute')
    p_exec.add_argument('--line', '-l', type=int, help='Line number (default: function exit)')
    p_exec.add_argument('--loop-index', '-i', type=int, default=1, help='Loop iteration index')
    p_exec.add_argument('--allow-external', action='store_true', help='Allow targeting stdlib/site-packages files (override EXCLUDE_PATHS for target file only)')
    p_exec.add_argument('--timeout', '-t', type=int, default=60, help='Timeout in seconds')
    p_exec.set_defaults(func=cmd_exec)

    # state command (for debugging)
    p_state = subparsers.add_parser('state', help='Show tracer state')
    p_state.set_defaults(func=cmd_state)

    # diff command
    p_diff = subparsers.add_parser('diff', help='Compare two frames of the same function')
    p_diff.add_argument('cmd', help='Python command to run')
    p_diff.add_argument('frame1', help='First frame ID: "/path/file.py:func#1"')
    p_diff.add_argument('frame2', help='Second frame ID: "/path/file.py:func#3"')
    p_diff.add_argument('--full-watch', help='Comma-separated variable names to show (filters FLT output, 600-char cap)')
    p_diff.add_argument('--allow-external', action='store_true', help='Allow targeting stdlib/site-packages files (override EXCLUDE_PATHS for target file only)')
    p_diff.add_argument('--timeout', '-t', type=int, default=60, help='Timeout in seconds')
    p_diff.set_defaults(func=cmd_diff)

    args = parser.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
