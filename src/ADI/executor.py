"""Executor for running target programs with tracer injection"""

import os
import re
import shlex
import signal
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Optional, Tuple, List, Dict


# Path to dbgtool package (now in src/ADI/dbgtool)
DBGTOOL_PATH = Path(__file__).parent / "dbgtool"

def _resolve_data_dir(data_dir: Optional[str] = None) -> str:
    """Resolve the directory for runtime data files (state.json, call_graph_data.json, etc.).

    Priority:
    1) Explicit `data_dir` parameter
    2) Current process `ADI_DATA_DIR` env var (if set)
    3) Fallback to the dbgtool package directory (matches dbgtool.tracer.get_data_dir default)
    """
    if data_dir:
        return data_dir
    env_data_dir = os.environ.get("ADI_DATA_DIR")
    if env_data_dir:
        return env_data_dir
    return str(DBGTOOL_PATH)

# Environment variable to override dbgtool path in wrapper (for container execution)
# Set ADI_DBGTOOL_PATH to the container path when running in Docker


class CommandSpec:
    def __init__(
        self,
        python_prefix: str,
        python_argv: List[str],
        env: Dict[str, str],
        mode: str,
        args: Optional[List[str]] = None,
        script_path: Optional[str] = None,
        module_name: Optional[str] = None,
        code: Optional[str] = None,
    ):
        self.python_prefix = python_prefix
        self.python_argv = python_argv
        self.env = env
        self.mode = mode  # "script", "module", or "code"
        self.args = list(args) if args else []
        self.script_path = script_path
        self.module_name = module_name
        self.code = code

    def argv(self) -> List[str]:
        if self.mode == "script":
            if not self.script_path:
                raise ValueError("script_path is required for script mode")
            return [self.script_path] + list(self.args)
        if self.mode == "module":
            if not self.module_name:
                raise ValueError("module_name is required for module mode")
            return [self.module_name] + list(self.args)
        if self.mode == "code":
            return ["-c"] + list(self.args)
        raise ValueError(f"Unknown mode: {self.mode}")


def describe_python_cmd(cmd: str) -> CommandSpec:
    """Parse python command into a normalized CommandSpec.

    Supports:
    1. python [flags] script.py [args]
    2. python [flags] -m module [args]
    3. python [flags] -c "code" [args]
    """
    try:
        tokens = shlex.split(cmd)
    except ValueError as exc:
        raise ValueError(f"Cannot parse python command: {cmd}") from exc

    if not tokens:
        raise ValueError(f"Cannot parse python command: {cmd}")

    # Support common env-prefix forms used by agents, e.g.:
    #   PYTHONPATH=/x python script.py
    #   env PYTHONPATH=/x python -m pkg
    py_idx = None
    for i, tok in enumerate(tokens):
        if re.search(r'^python(?:\d+(?:\.\d+)*)?$', os.path.basename(tok)):
            py_idx = i
            break
    if py_idx is None:
        raise ValueError(f"Cannot parse python command: {cmd}")

    prefix_tokens = tokens[:py_idx]  # env assignments, 'env', etc.
    tokens = tokens[py_idx:]

    def _join_tokens(parts):
        return " ".join(shlex.quote(part) for part in parts)

    def _parse_env_prefix(parts: List[str]) -> Dict[str, str]:
        """Parse a limited env-prefix used by agents.

        Supported forms (no shell required):
          - VAR=VALUE python ...
          - env VAR=VALUE python ...

        Any other prefix token is rejected to avoid implicit shell semantics.
        """
        if not parts:
            return {}

        env = {}
        idx0 = 0
        if parts and parts[0] == "env":
            idx0 = 1

        assign_re = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
        for tok in parts[idx0:]:
            m = assign_re.match(tok)
            if not m:
                raise ValueError(f"Cannot parse python command prefix token: {tok!r}")
            env[m.group(1)] = m.group(2)
        return env

    env_prefix = _parse_env_prefix(prefix_tokens)

    if '-c' in tokens:
        idx = tokens.index('-c')
        python_argv = list(tokens[:idx])
        python_prefix = _join_tokens(prefix_tokens + python_argv)
        if idx + 1 >= len(tokens):
            raise ValueError(f"Cannot parse python command: {cmd}")
        code = tokens[idx + 1]
        args = tokens[idx + 2:] if idx + 2 < len(tokens) else []
        return CommandSpec(
            python_prefix=python_prefix,
            python_argv=python_argv,
            env=env_prefix,
            mode="code",
            code=code,
            args=args,
        )

    if '-m' in tokens:
        idx = tokens.index('-m')
        python_argv = list(tokens[:idx])
        python_prefix = _join_tokens(prefix_tokens + python_argv)
        module = tokens[idx + 1] if idx + 1 < len(tokens) else None
        if not module:
            raise ValueError(f"Cannot parse python command: {cmd}")
        module_args = tokens[idx + 2:] if idx + 2 < len(tokens) else []
        return CommandSpec(
            python_prefix=python_prefix,
            python_argv=python_argv,
            env=env_prefix,
            mode="module",
            module_name=module,
            args=module_args,
        )

    script_index = None
    for i in range(1, len(tokens)):
        if tokens[i].lower().endswith('.py'):
            script_index = i
            break

    if script_index is None:
        raise ValueError(f"Cannot parse python command: {cmd}")

    python_argv = list(tokens[:script_index])
    python_prefix = _join_tokens(prefix_tokens + python_argv)
    script = tokens[script_index]
    args = tokens[script_index + 1:] if script_index + 1 < len(tokens) else []
    return CommandSpec(
        python_prefix=python_prefix,
        python_argv=python_argv,
        env=env_prefix,
        mode="script",
        script_path=os.path.abspath(script),
        args=args,
    )


def build_sys_argv(spec: CommandSpec) -> List[str]:
    """Build sys.argv according to the original command."""
    return spec.argv()


def build_exec_snippet(spec: CommandSpec) -> str:
    """Build execution snippet for wrapper/tracer scripts."""
    if spec.mode == "script":
        script_abs = spec.script_path
        if not script_abs:
            raise ValueError("script_path is required for script mode")
        # Execute the target script in the real __main__ module namespace (like `python script.py`),
        # so that `import __main__` and multiprocessing pickling behave as expected.
        #
        # Note: we intentionally do not use runpy.run_path(run_name="__main__") because it
        # runs in a temporary module and restores sys.modules['__main__'] afterwards, which
        # can surprise atexit handlers or background threads that import __main__ later.
        return (
            f"import tokenize\n"
            f"_adi_main = sys.modules.get('__main__')\n"
            f"_adi_globals = getattr(_adi_main, '__dict__', {{}})\n"
            f"_adi_globals.update({{'__name__': '__main__', '__file__': {script_abs!r}, '__package__': None, '__spec__': None, '__loader__': None}})\n"
            f"with tokenize.open({script_abs!r}) as _adi_f:\n"
            f"    _adi_code = _adi_f.read()\n"
            f"exec(compile(_adi_code, {script_abs!r}, 'exec'), _adi_globals)"
        )
    if spec.mode == "module":
        if not spec.module_name:
            raise ValueError("module_name is required for module mode")
        return f"runpy.run_module({spec.module_name!r}, run_name='__main__')"
    if spec.mode == "code":
        if spec.code is None:
            raise ValueError("code is required for code mode")
        return f"exec(compile({spec.code!r}, '<string>', 'exec'), {{'__name__': '__main__'}})"
    raise ValueError(f"Unknown mode: {spec.mode}")

def _get_dbgtool_path_for_wrapper() -> str:
    """Get dbgtool path for use in wrapper script.

    Uses ADI_DBGTOOL_PATH env var if set (for container execution),
    otherwise uses the default DBGTOOL_PATH.
    """
    return os.environ.get('ADI_DBGTOOL_PATH', str(DBGTOOL_PATH.parent))


def _create_temp_data_dir() -> str:
    """Create a unique temp directory for ADI data files."""
    data_dir = os.path.join(tempfile.gettempdir(), f"adi_{uuid.uuid4().hex[:8]}")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def _cleanup_temp_data_dir(data_dir: str) -> None:
    """Clean up temp data directory and its contents."""
    if not data_dir or not os.path.exists(data_dir):
        return

    # Safety guard: only clean directories created by ADI under the current temp root.
    # This prevents accidental deletion if a wrong path is passed.
    base = os.path.basename(os.path.normpath(data_dir))
    if not base.startswith("adi_"):
        return
    abs_dir = os.path.abspath(data_dir)
    abs_tmp = os.path.abspath(tempfile.gettempdir())
    try:
        if os.path.commonpath([abs_dir, abs_tmp]) != abs_tmp:
            return
    except ValueError:
        return

    import shutil
    shutil.rmtree(abs_dir, ignore_errors=True)


def _decode_subprocess_stream(data: object) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _run_argv_with_timeout(
    argv: List[str],
    *,
    timeout: int,
    env: Dict[str, str],
    cwd: Optional[str] = None,
) -> Tuple[str, str, int, bool]:
    """Run a subprocess argv with timeout and process-group cleanup.

    Returns (stdout, stderr, returncode, timed_out).

    Rationale:
    - We run the child in a new session and kill the whole process group on timeout.
    - We avoid `shell=True` so that paths containing spaces (e.g. TMPDIR) do not break.
    """
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            cwd=cwd,
            start_new_session=True,
        )
    except OSError as exc:
        err_s = f"[ADI] failed to start subprocess: {exc}\n"
        return "", err_s, 127, False
    try:
        out, err = proc.communicate(timeout=timeout)
        return _decode_subprocess_stream(out), _decode_subprocess_stream(err), int(proc.returncode or 0), False
    except subprocess.TimeoutExpired:
        # Best-effort: terminate then force-kill the whole process group.
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            out, err = proc.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = proc.communicate()
        out_s = _decode_subprocess_stream(out)
        err_s = _decode_subprocess_stream(err)
        # Add a stable marker without a Python traceback.
        err_s = (err_s + ("\n" if err_s and not err_s.endswith("\n") else "") +
                 f"[ADI] timeout after {timeout}s (killed process group)\n")
        return out_s, err_s, 124, True


def create_wrapper(
    cmd: str,
    frame_id: str,
    condition: Optional[str] = None,
    condition_when: str = 'entry',
    call_graph_mode: bool = False,
    loop: int = 2,
    loop_index: Optional[int] = None,
    full_watch: Optional[list] = None,
    full_watch_max_length: Optional[int] = None,
    if_eval_lineno: Optional[int] = None,
    ignore_frame_index: bool = False,
    count_total_calls: bool = True,
    allow_external_target: bool = False,
) -> Tuple[str, List[str], Dict[str, str]]:
    """Create a temporary wrapper script for tracer injection.

    Returns (wrapper_path, wrapper_argv, env_prefix).
    """
    spec = describe_python_cmd(cmd)
    script_abs = spec.script_path
    script_dir = os.path.dirname(script_abs) or "." if script_abs else None
    sys_argv = build_sys_argv(spec)
    exec_snippet = build_exec_snippet(spec)

    # Build tracer parameters
    tracer_params = [
        f"target_frame_id={frame_id!r}",
    ]
    if condition:
        tracer_params.append(f"condition={condition!r}")
        tracer_params.append(f"condition_when={condition_when!r}")
    if call_graph_mode:
        tracer_params.append("call_graph_mode=True")
    if loop != 2:
        tracer_params.append(f"loop={loop}")
    if loop_index is not None:
        tracer_params.append(f"observed_loop_index={loop_index}")
    if full_watch:
        tracer_params.append(f"full_watch={full_watch!r}")
        if full_watch_max_length is not None:
            tracer_params.append(f"full_watch_max_length={full_watch_max_length}")
    if if_eval_lineno is not None:
        tracer_params.append(f"if_eval_lineno={if_eval_lineno}")
    if ignore_frame_index:
        tracer_params.append("ignore_frame_index=True")
    if not count_total_calls:
        tracer_params.append("count_total_calls=False")
    if allow_external_target:
        tracer_params.append("allow_external_target=True")

    tracer_params_str = ", ".join(tracer_params)

    # Get dbgtool path for wrapper (may be different in container)
    dbgtool_path = _get_dbgtool_path_for_wrapper()

    wrapper_content = f'''# ADI Wrapper - Auto-generated
import sys
import os
import json
import runpy

# Setup paths
sys.path.insert(0, {dbgtool_path!r})
_adi_cwd = os.getcwd()
sys.path.insert(0, _adi_cwd)
{f"sys.path.insert(0, {script_dir!r})" if script_dir else ""}
sys.argv = {sys_argv!r}

# Initialize state.json for tracer (required by dbgtool)
# Use ADI_DATA_DIR env var if set, otherwise use default dbgtool path
_data_dir = os.environ.get('ADI_DATA_DIR', os.path.join({dbgtool_path!r}, 'dbgtool'))
_state_path = os.path.join(_data_dir, 'state.json')
_state_data = {{
    'cmd': {cmd!r},
    'bp_frame_id': None,
    'curr_frame_id': None,
    'target_frame_parent_id': None,
}}
with open(_state_path, 'w') as _f:
    json.dump(_state_data, _f)

# Import and start tracer
from dbgtool import Tracer
_adi_tracer = Tracer({tracer_params_str})

# Execute target
{exec_snippet}
'''

    # Write to temp file
    fd, wrapper_path = tempfile.mkstemp(suffix='.py', prefix='adi_wrapper_')
    os.close(fd)

    with open(wrapper_path, 'w') as f:
        f.write(wrapper_content)

    # Build argv: use the same python interpreter+flags as the original command.
    wrapper_argv = list(spec.python_argv) + [wrapper_path]

    return wrapper_path, wrapper_argv, dict(spec.env)


def get_call_graph_path(data_dir: Optional[str] = None) -> str:
    """Get path to call_graph_data.json"""
    base_dir = _resolve_data_dir(data_dir)
    return os.path.join(base_dir, "call_graph_data.json")


def get_state_path(data_dir: Optional[str] = None) -> str:
    """Get path to state.json"""
    base_dir = _resolve_data_dir(data_dir)
    return os.path.join(base_dir, "state.json")

def get_flt_path(data_dir: Optional[str] = None) -> str:
    """Get path to flt.json (structured frame lifetime trace)."""
    base_dir = _resolve_data_dir(data_dir)
    return os.path.join(base_dir, "flt.json")

def read_state(data_dir: Optional[str] = None) -> Optional[dict]:
    """Read state.json to get tracer state info."""
    import json
    path = get_state_path(data_dir)
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)
    return None


def read_flt_json(data_dir: Optional[str] = None) -> Optional[dict]:
    """Read flt.json produced by the tracer (strategy1)."""
    import json
    path = get_flt_path(data_dir)
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def read_flt(data_dir: Optional[str] = None):
    """Read flt.json and convert it to a FLT object."""
    payload = read_flt_json(data_dir=data_dir)
    if not payload:
        return None

    from .session import FLT, TraceStep

    trace_steps = []
    for item in payload.get("trace") or []:
        trace_steps.append(
            TraceStep(
                lineno=item.get("lineno"),
                stmt=item.get("stmt") or "",
                new_vars=item.get("new_vars") or {},
                modified_vars=item.get("modified_vars") or {},
                callee_frame_id=item.get("callee_frame_id"),
                iter_num=item.get("iter_num"),
                skipped_before=item.get("skipped_before"),
            )
        )

    return FLT(
        frame_id=payload.get("frame_id") or "",
        caller_frame_id=payload.get("caller_frame_id"),
        args=payload.get("args") or {},
        return_value=payload.get("return_value"),
        trace=trace_steps,
    )


def execute_with_tracer(
    cmd: str,
    frame_id: str,
    condition: Optional[str] = None,
    condition_when: str = 'entry',
    call_graph_mode: bool = False,
    timeout: int = 60,
    loop: int = 2,
    loop_index: Optional[int] = None,
    full_watch: Optional[list] = None,
    full_watch_max_length: Optional[int] = None,
    if_eval_lineno: Optional[int] = None,
    ignore_frame_index: bool = False,
    count_total_calls: bool = True,
    allow_external_target: bool = False,
) -> Tuple[str, str, int]:
    """Execute command with tracer injection.

    Returns (stdout, stderr, returncode, data_dir).
    Note: Caller must clean up `data_dir` after reading state/call_graph results.
    """
    wrapper_path = None
    data_dir = None

    try:
        # Create temp data dir and set env var
        data_dir = _create_temp_data_dir()

        wrapper_path, wrapper_argv, env_prefix = create_wrapper(
            cmd, frame_id, condition, condition_when, call_graph_mode, loop, loop_index,
            full_watch=full_watch, full_watch_max_length=full_watch_max_length,
            if_eval_lineno=if_eval_lineno, ignore_frame_index=ignore_frame_index,
            count_total_calls=count_total_calls,
            allow_external_target=allow_external_target,
        )

        # Set ADI_DATA_DIR env var for the subprocess
        env = os.environ.copy()
        env.update(env_prefix)
        env['ADI_DATA_DIR'] = data_dir

        result_stdout, result_stderr, returncode, _timed_out = _run_argv_with_timeout(
            wrapper_argv,
            timeout=timeout,
            env=env,
        )

        # Tracer outputs to stderr by default, combine both
        combined_output = result_stdout + result_stderr
        return combined_output, result_stderr, returncode, str(data_dir)

    finally:
        # Cleanup wrapper only, data dir cleanup deferred for read operations
        if wrapper_path and os.path.exists(wrapper_path):
            os.remove(wrapper_path)


def cleanup_data_dir(data_dir: Optional[str]) -> None:
    """Clean up an ADI temp data directory created by this module."""
    if not data_dir:
        return
    _cleanup_temp_data_dir(str(data_dir))


def read_call_graph(data_dir: Optional[str] = None) -> Optional[dict]:
    """Read call graph payload from call_graph_data.json.

    Schema:
      {"schema": "adi.call_graph", "version": 1, "calls": [...]}
    """
    import json
    path = get_call_graph_path(data_dir)
    if os.path.exists(path):
        with open(path, 'r') as f:
            return json.load(f)
    return None


def get_insert_stmt_path(data_dir: Optional[str] = None) -> str:
    """Get path to insert_stmt.json"""
    base_dir = _resolve_data_dir(data_dir)
    return os.path.join(base_dir, "insert_stmt.json")


def get_exec_result_path(data_dir: Optional[str] = None) -> str:
    """Get path to exec_result.txt (exec output file)"""
    base_dir = _resolve_data_dir(data_dir)
    return os.path.join(base_dir, "exec_result.txt")


def read_exec_result(data_dir: Optional[str] = None) -> Optional[str]:
    """Read exec result from file."""
    path = get_exec_result_path(data_dir)
    if os.path.exists(path):
        with open(path, 'r') as f:
            return f.read()
    return None


def setup_execute_stmt(
    frame_id: str,
    stmt: str,
    lineno: int = None,
    loop_index: int = 1,
    data_dir: Optional[str] = None,
) -> None:
    """Setup insert_stmt.json for execute functionality.

    Args:
        lineno: Line number to execute at. If None or -1, execute at function exit.
    """
    import json
    # Use -1 to indicate "execute at function exit"
    effective_lineno = -1 if lineno is None else lineno
    data = {
        "frame_id": frame_id,
        "stmt": stmt,
        "start": effective_lineno,
        "end": effective_lineno,
        "loop_index": loop_index,
    }
    path = get_insert_stmt_path(data_dir)
    with open(path, 'w') as f:
        json.dump(data, f)


def cleanup_execute_stmt(data_dir: Optional[str] = None) -> None:
    """Remove insert_stmt.json after execution."""
    path = get_insert_stmt_path(data_dir)
    if os.path.exists(path):
        os.remove(path)


def execute_stmt_in_frame(
    cmd: str,
    frame_id: str,
    stmt: str,
    lineno: int,
    loop_index: int = 1,
    timeout: int = 60,
    allow_external_target: bool = False,
) -> Tuple[str, str, int]:
    """Execute a statement at specific frame and line.

    Returns (stdout, stderr, returncode, data_dir).
    """
    wrapper_path = None
    data_dir = None

    try:
        # Create temp data dir
        data_dir = _create_temp_data_dir()

        # Setup insert_stmt.json in temp dir
        setup_execute_stmt(frame_id, stmt, lineno, loop_index, data_dir)

        wrapper_path, wrapper_argv, env_prefix = create_wrapper(
            cmd,
            frame_id,
            condition=None,
            call_graph_mode=False,
            allow_external_target=allow_external_target,
        )

        # Set ADI_DATA_DIR env var for the subprocess
        env = os.environ.copy()
        env.update(env_prefix)
        env['ADI_DATA_DIR'] = data_dir

        result_stdout, result_stderr, returncode, _timed_out = _run_argv_with_timeout(
            wrapper_argv,
            timeout=timeout,
            env=env,
        )
        combined_output = result_stdout + result_stderr
        return combined_output, result_stderr, returncode, str(data_dir)

    finally:
        if wrapper_path and os.path.exists(wrapper_path):
            os.remove(wrapper_path)
