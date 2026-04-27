# Copyright 2019 Ram Rachum and collaborators.
# This program is distributed under the MIT license.

import atexit
import functools
import inspect
import opcode
import os
import sys
import re
import collections
import datetime as datetime_module
import itertools
import threading
import traceback
import json
import ctypes
import linecache
from typing import List, Dict, Tuple, Any
from .variables import CommonVariable, Exploding, BaseVariable
from .utils import normalize_frame_id


class LoopState:
    """State for tracking a single loop's iteration caching."""

    def __init__(self):
        self.skipped_count = 0  # Number of skipped iterations
        self.current_iter = 0  # Current iteration number
        # Cache holds structured FLT steps for the most recent (potentially incomplete) iteration.
        # We intentionally keep this as "steps", not raw text lines, because FLT is now emitted via
        # flt.json (no more text protocol parsing on the parent side).
        self.cache = []
        self.prev_cache = []
from . import utils, pycompat
if pycompat.PY2:
    from io import open
from .utils import _derive_method_name, parse_frame_id, normalize_frame_id
import site


EXCLUDE_PATHS = []
EXCLUDE_PATHS.extend(site.getsitepackages())     # 系统 site-packages
EXCLUDE_PATHS.append(site.getusersitepackages()) # 用户 site-packages
EXCLUDE_PATHS.append(os.path.dirname(os.__file__))  # stdlib 目录


def get_data_dir():
    """Get ADI data directory. Uses ADI_DATA_DIR env var if set, otherwise module dir."""
    return os.environ.get('ADI_DATA_DIR', os.path.dirname(os.path.abspath(__file__)))



PyFrame_LocalsToFast = ctypes.pythonapi.PyFrame_LocalsToFast
PyFrame_LocalsToFast.argtypes = [ctypes.py_object, ctypes.c_int]
PyFrame_LocalsToFast.restype = None

def exec_in_frame(frame, code_str):
    import io, builtins
    from contextlib import redirect_stdout, redirect_stderr

    g, l = frame.f_globals, frame.f_locals
    g.setdefault("__builtins__", builtins)

    buf = io.StringIO()
    with redirect_stdout(buf), redirect_stderr(buf):
        try:
            exec(code_str, g, l)
        except BaseException as e:
            traceback.print_exception(type(e), e, e.__traceback__)
    try:
        PyFrame_LocalsToFast(frame, 1)  # 同步局部变量（如果可用）
    except Exception:
        pass
    return buf.getvalue()





ipython_filename_pattern = re.compile('^<ipython-input-([0-9]+)-.*>$')
ansible_filename_pattern = re.compile(r'^(.+\.zip)[/|\\](ansible[/|\\]modules[/|\\].+\.py)$')
ipykernel_filename_pattern = re.compile(r'^/var/folders/.*/ipykernel_[0-9]+/[0-9]+.py$')
RETURN_OPCODES = {
    'RETURN_GENERATOR', 'RETURN_VALUE', 'RETURN_CONST',
    'INSTRUMENTED_RETURN_GENERATOR', 'INSTRUMENTED_RETURN_VALUE',
    'INSTRUMENTED_RETURN_CONST', 'YIELD_VALUE', 'INSTRUMENTED_YIELD_VALUE'
}


def get_local_reprs(frame, watch=(), custom_repr=(), max_length=None, normalize=False,
                    full_watch=None, full_watch_max_length=None):
    code = frame.f_code
    vars_order = (code.co_varnames + code.co_cellvars + code.co_freevars +
                  tuple(frame.f_locals.keys()))

    full_watch_set = set(full_watch) if full_watch else None
    local_items = frame.f_locals.items()
    if full_watch_set:
        local_items = [(key, value) for key, value in local_items if key in full_watch_set]

    result_items = []
    for key, value in local_items:
        item_max_length = max_length
        if full_watch_set and full_watch_max_length is not None and key in full_watch_set:
            item_max_length = full_watch_max_length
        result_items.append((key, utils.get_shortish_repr(value, custom_repr,
                                                          item_max_length, normalize)))
    result_items.sort(key=lambda key_value: vars_order.index(key_value[0]))
    result = collections.OrderedDict(result_items)

    if not full_watch_set:
        for variable in watch:
            result.update(sorted(variable.items(frame, normalize)))
    return result


class UnavailableSource(object):
    def __getitem__(self, i):
        return u'SOURCE IS UNAVAILABLE'


source_and_path_cache = {}


def get_path_and_source_from_frame(frame):
    globs = frame.f_globals or {}
    module_name = globs.get('__name__')
    file_name = frame.f_code.co_filename
    cache_key = (module_name, file_name)
    try:
        return source_and_path_cache[cache_key]
    except KeyError:
        pass
    loader = globs.get('__loader__')

    source = None
    if hasattr(loader, 'get_source'):
        try:
            source = loader.get_source(module_name)
        except ImportError:
            pass
        if source is not None:
            source = source.splitlines()
    if source is None:
        ipython_filename_match = ipython_filename_pattern.match(file_name)
        ansible_filename_match = ansible_filename_pattern.match(file_name)
        ipykernel_filename_match = ipykernel_filename_pattern.match(file_name)
        if ipykernel_filename_match:
            try:
                import linecache
                _, _, source, _ = linecache.cache.get(file_name)
                source = [line.rstrip() for line in source] # remove '\n' at the end
            except Exception:
                pass
        elif ipython_filename_match:
            entry_number = int(ipython_filename_match.group(1))
            try:
                import IPython
                ipython_shell = IPython.get_ipython()
                ((_, _, source_chunk),) = ipython_shell.history_manager. \
                                  get_range(0, entry_number, entry_number + 1)
                source = source_chunk.splitlines()
            except Exception:
                pass
        elif ansible_filename_match:
            try:
                import zipfile
                archive_file = zipfile.ZipFile(ansible_filename_match.group(1), 'r')
                source = archive_file.read(ansible_filename_match.group(2).replace('\\', '/')).splitlines()
            except Exception:
                pass
        else:
            try:
                with open(file_name, 'rb') as fp:
                    source = fp.read().splitlines()
            except utils.file_reading_errors:
                pass
    if not source:
        # We used to check `if source is None` but I found a rare bug where it
        # was empty, but not `None`, so now we check `if not source`.
        source = UnavailableSource()

    # If we just read the source from a file, or if the loader did not
    # apply tokenize.detect_encoding to decode the source into a
    # string, then we should do that ourselves.
    if isinstance(source[0], bytes):
        encoding = 'utf-8'
        for line in source[:2]:
            # File coding may be specified. Match pattern from PEP-263
            # (https://www.python.org/dev/peps/pep-0263/)
            match = re.search(br'coding[:=]\s*([-\w.]+)', line)
            if match:
                encoding = match.group(1).decode('ascii')
                break
        source = [pycompat.text_type(sline, encoding, 'replace') for sline in
                  source]

    result = (file_name, source)
    source_and_path_cache[cache_key] = result
    return result


def _safe_get_source_line(source, lineno):
    """Best-effort source line lookup.

    `get_path_and_source_from_frame` may return a real list that is shorter than `lineno`
    (e.g. when `compile(..., filename=...)` points at a different on-disk file), which
    would otherwise raise IndexError and crash the tracer.
    """
    try:
        if lineno is None or lineno < 1:
            return u'SOURCE IS UNAVAILABLE'
        return source[lineno - 1]
    except Exception:
        return u'SOURCE IS UNAVAILABLE'


def get_write_function(output, overwrite):
    is_path = isinstance(output, (pycompat.PathLike, str))
    if overwrite and not is_path:
        raise Exception('`overwrite=True` can only be used when writing '
                        'content to file.')
    if output is None:
        def write(s):
            stderr = sys.stderr
            try:
                stderr.write(s)
            except UnicodeEncodeError:
                # God damn Python 2
                stderr.write(utils.shitcode(s))
    elif is_path:
        return FileWriter(output, overwrite).write
    elif callable(output):
        write = output
    else:
        assert isinstance(output, utils.WritableStream)

        def write(s):
            output.write(s)
    return write


class FileWriter(object):
    def __init__(self, path, overwrite):
        self.path = pycompat.text_type(path)
        self.overwrite = overwrite

    def write(self, s):
        with open(self.path, 'w' if self.overwrite else 'a',
                  encoding='utf-8') as output_file:
            output_file.write(s)
        self.overwrite = False


thread_global = threading.local()
DISABLED = bool(os.getenv('DBGTOOL_DISABLED', ''))

class Tracer:

    def __init__(self, output=None, watch=(), watch_explode=(), depth=1, prefix='', overwrite=False, thread_info=False, custom_repr=(),
                 max_variable_length=200, normalize=False, relative_time=False, color=False, target_frame_id = None,
                 observed_loop_index = None, call_graph_mode = False,  condition = None, condition_when = 'entry', loop = 2,
                 full_watch=None, full_watch_max_length=600, if_eval_lineno=None, ignore_frame_index=False,
                 count_total_calls=True, allow_external_target=False):
        
        self.candidate_method_names = set()

        self.observed_file, self.method_name_or_lineno, self.observed_frame_index = parse_frame_id(target_frame_id)
        self.observed_file = os.path.abspath(self.observed_file)
        # method_name_or_lineno can be int (line number) or str (method name)
        self.target_is_lineno = isinstance(self.method_name_or_lineno, int)
        self.target_lineno = self.method_name_or_lineno if self.target_is_lineno else None
        self.method_name = str(self.method_name_or_lineno)  # Keep as string for backward compat
        self.target_frame_id = f'{os.path.abspath(self.observed_file)}:{self.method_name_or_lineno}#{self.observed_frame_index}'

        self.is_executed = False
        self.insert_stmt = None
        insert_stmt_path = os.path.abspath(os.path.join(get_data_dir(), 'insert_stmt.json'))
        if os.path.exists(insert_stmt_path):
            with open(insert_stmt_path, 'r') as f:
                insert_data = json.load(f)
            self.insert_stmt = insert_data.get('stmt', None)
            # Keep original frame_id format (both line number and method name formats supported)
            # The unified _match_frame_id() method will handle both formats
            self.insert_frame_id = normalize_frame_id(insert_data.get('frame_id', None))
            # self.insert_lineno = int(insert_data.get('lineno', None))
            self.insert_start_line = int(insert_data.get('start', None))
            self.insert_end_line = int(insert_data.get('end', None))
            self.insert_loop_index = int(insert_data.get('loop_index', None))
            self.insert_lineno_excuted_times = 0



        # --- Performance fast-path configuration ---
        # Before the focus frame is hit, sys.settrace will still invoke our callback for
        # *every* Python call. To keep overhead tiny, we can early-return for frames that
        # are not from a small set of interesting files (typically just the target file).
        self._fast_include_files = {self.observed_file}
        self._method_name_last = self.method_name.split('.')[-1] if self.method_name else ''
        try:
            if self.insert_stmt and getattr(self, 'insert_frame_id', None):
                _ins_file, _, _ = parse_frame_id(self.insert_frame_id)
                self._fast_include_files.add(os.path.abspath(_ins_file))
        except Exception:
            pass
        self._fast_include_basenames = {
            os.path.basename(path) for path in self._fast_include_files if path
        }
        # Cache raw co_filename -> (abs_path if included else False). This avoids repeated
        # basename/abspath work in profile() once a filename has been seen.
        self._co_filename_fast_include_cache = {}
        # Cache for normalizing co_filename -> abs path (avoid repeated os.path.abspath)
        self._co_filename_abspath_cache = {}
        self._co_filename_basename_cache = {}
        # Lightweight call-index tracking for active frames (enables caller capture for step-out).
        self._code_call_counter = {}  # code object -> call count

        self.allow_trace_skipped = True
        self._profile_active = False
        self._prev_sys_profile = None
        self._prev_threading_profile = None
        self._prev_threading_trace = None

        self.is_last_skip = False
        self.is_last_call_skip = False
        self.loop = loop
        self.observed_loop_index = observed_loop_index
        # Loop state: key = (frame, loop_header_lineno), value = LoopState
        self.loop_states: Dict[Tuple[Any, int], LoopState] = {}
        self.current_loop_lineno: Dict[Any, int] = {}  # frame -> current loop header lineno
        
        self.depth_expanded = not call_graph_mode
        self.depth = depth if not call_graph_mode else 3  # Limit call-tree depth to 3 to cap output
        # self.is_in_expanded_status = False
        
        self.call_graph_output_path =  os.path.abspath(os.path.join(get_data_dir(), 'call_graph_data.json')) if call_graph_mode else None
        self.frame_status_path = os.path.abspath(os.path.join(get_data_dir(), 'state.json'))
        self.flt_json_path = os.path.abspath(os.path.join(get_data_dir(), 'flt.json'))
        self.bp_frame_name = None
        self.bp_frame_index = None

        self.target_frame_parent_id = None
        self.frame_counter = dict()
        self.frame_to_id = dict()
        self.state_data = {}
        if os.path.exists(self.frame_status_path):
            with open(self.frame_status_path, 'r') as f:
                self.state_data = json.load(f)
                bp_frame_id = normalize_frame_id(self.state_data.get('bp_frame_id', None))
                if bp_frame_id:
                    self.bp_frame_name, self.bp_frame_index = bp_frame_id.rsplit('#', 1)
                self.target_frame_parent_id = self.state_data.get('target_frame_parent_id', None)

        if call_graph_mode:

            self.call_frames = {}
            self.call_infos = []
            self._call_graph_written = False
            atexit.register(self._write_call_graph)

        self.frame_line_executed = {}

        self.condition = condition
        self.condition_when = condition_when  # 'entry', 'return', or 'both'
        self.condition_error_printed = False  # Flag to print condition error only once
        self.ignore_frame_index = bool(ignore_frame_index)
        self._method_name_cache = {}  # code object -> derived method name
        self.if_eval_lineno = if_eval_lineno
        self._condition_code = None
        self._condition_compile_error = None
        if self.condition:
            try:
                self._condition_code = compile(self.condition, "<adi-condition>", "eval")
            except SyntaxError as e:
                self._condition_compile_error = e
        self._condition_arginfo_cache = {}  # code object -> (arg_names, varargs_name, varkw_name)
        self._prechecked_condition_frames = set()
        self._state_dirty = False
        atexit.register(self._flush_state_data)
        # FLT JSON capture (strategy1): tracer does not emit FLT text; parent reads flt.json.
        self._flt_data: Dict[str, Any] = None
        self._flt_steps: List[Dict[str, Any]] = []
        self._flt_last_step: Dict[str, Any] = None
        self._flt_written = False
        self._focus_frame = None
        self._exec_return_only = bool(
            self.insert_stmt
            and self.insert_start_line == -1
            and not self.condition
            and self.if_eval_lineno is None
        )
        self.pending_condition_frames = set()
        self._force_next_line_frames = set()

        # Counting mode: continue execution after target frame to count total calls
        self.count_total_calls = bool(count_total_calls)
        self.counting_mode = False
        self.saved_stdout = None
        self.saved_stderr = None
        self.allow_external_target = bool(allow_external_target)
        self.target_frame_name = f"{self.observed_file}:{self.method_name}"
        self.matched_frame_index = None  # The index we stopped at
        self.frame_matched_count = 0  # Count of frames matching frame ID
        self.condition_matched_count = 0  # Count of frames matching condition

        if self.observed_file:
            assert os.path.exists(self.observed_file)

        self._write = get_write_function(output, overwrite)

        self.watch = [
            v if isinstance(v, BaseVariable) else CommonVariable(v)
            for v in utils.ensure_tuple(watch)
         ] + [
             v if isinstance(v, BaseVariable) else Exploding(v)
             for v in utils.ensure_tuple(watch_explode)
        ]
        full_watch_set = {name for name in utils.ensure_tuple(full_watch) if name}
        self.full_watch = full_watch_set
        self.full_watch_max_length = full_watch_max_length if full_watch_set else None
        self.frame_to_local_reprs = {}
        self.start_times = {}
        self.prefix = prefix
        self.thread_info = thread_info
        self.thread_info_padding = 0
        assert self.depth >= 1
        self.target_codes = set()
        self.target_frames = set()
        self.thread_local = threading.local()
        if len(custom_repr) == 2 and not all(isinstance(x,
                      pycompat.collections_abc.Iterable) for x in custom_repr):
            custom_repr = (custom_repr,)
        self.custom_repr = custom_repr
        self.last_source_path = None
        self.max_variable_length = max_variable_length
        self.normalize = normalize
        self.relative_time = relative_time
        self.color = color and sys.platform in ('linux', 'linux2', 'cygwin', 'darwin')

        if self.color:
            self._FOREGROUND_BLUE = '\x1b[34m'
            self._FOREGROUND_PURPLE = '\x1b[95m'
            self._FOREGROUND_CYAN = '\x1b[36m'
            self._FOREGROUND_GREEN = '\x1b[32m'
            self._FOREGROUND_MAGENTA = '\x1b[35m'
            self._FOREGROUND_RED = '\x1b[31m'
            self._FOREGROUND_RESET = '\x1b[39m'
            self._FOREGROUND_YELLOW = '\x1b[33m'
            self._STYLE_BRIGHT = '\x1b[1m'
            self._STYLE_DIM = '\x1b[2m'
            self._STYLE_NORMAL = '\x1b[22m'
            self._STYLE_RESET_ALL = '\x1b[0m'
        else:
            self._FOREGROUND_BLUE = ''
            self._FOREGROUND_PURPLE = ''
            self._FOREGROUND_CYAN = ''
            self._FOREGROUND_GREEN = ''
            self._FOREGROUND_MAGENTA = ''
            self._FOREGROUND_RED = ''
            self._FOREGROUND_RESET = ''
            self._FOREGROUND_YELLOW = ''
            self._STYLE_BRIGHT = ''
            self._STYLE_DIM = ''
            self._STYLE_NORMAL = ''
            self._STYLE_RESET_ALL = ''

        self.start()
        

    def start(self):
        sys.excepthook = self._excepthook
        if self.call_graph_output_path:
            self._enable_profile(self.trace)
            return
        self._profile_active = True
        self._enable_profile(self.profile)

    def stop(self):
        if self.call_graph_output_path:
            self._disable_profile()
            return
        if self._profile_active:
            self._disable_profile()
            return
        self._disable_trace()

    def _enable_profile(self, func):
        if self._prev_sys_profile is None:
            try:
                self._prev_sys_profile = sys.getprofile()
            except Exception:
                self._prev_sys_profile = None
        sys.setprofile(func)
        if hasattr(threading, 'setprofile'):
            if self._prev_threading_profile is None:
                try:
                    self._prev_threading_profile = threading.getprofile() if hasattr(threading, 'getprofile') else None
                except Exception:
                    self._prev_threading_profile = None
            try:
                threading.setprofile(func)
            except Exception:
                pass

    def _disable_profile(self):
        sys.setprofile(self._prev_sys_profile)
        if hasattr(threading, 'setprofile'):
            try:
                threading.setprofile(self._prev_threading_profile)
            except Exception:
                pass

    def _enable_trace(self):
        sys.settrace(self.trace)
        if hasattr(threading, 'settrace'):
            if self._prev_threading_trace is None:
                try:
                    self._prev_threading_trace = threading.gettrace() if hasattr(threading, 'gettrace') else None
                except Exception:
                    self._prev_threading_trace = None
            try:
                threading.settrace(self.trace)
            except Exception:
                pass

    def _disable_trace(self):
        sys.settrace(None)
        if hasattr(threading, 'settrace'):
            try:
                threading.settrace(self._prev_threading_trace)
            except Exception:
                pass

    def _switch_to_trace(self, frame):
        if not self._profile_active:
            return
        self._profile_active = False
        self._disable_profile()
        self._enable_trace()
        try:
            frame.f_trace = self.trace
        except Exception:
            pass

    def profile(self, frame, event, arg):
        if self.call_graph_output_path:
            return
        if event not in ('call', 'return', 'exception'):
            return
        code = frame.f_code
        frame_file_name = frame.f_code.co_filename

        # exec-return-only mode: we only care about the target function's return event.
        # Returning early here avoids all filename work for the vast majority of frames.
        if (
            event in ("return", "exception")
            and self._exec_return_only
            and (not self.target_is_lineno)
            and self._method_name_last
            and code.co_name != self._method_name_last
        ):
            return

        # Track the active Python call stack before file-based filtering. This lets
        # a target frame in a package/library report a caller that lives in an
        # external script, even when that caller file is not traced as a target file.
        call_stack = None
        if not self._exec_return_only:
            call_stack = getattr(self.thread_local, "profile_call_stack", None)
            if call_stack is None:
                call_stack = []
                self.thread_local.profile_call_stack = call_stack
            if event == 'call':
                call_index = self._code_call_counter.get(code, 0) + 1
                self._code_call_counter[code] = call_index
                call_stack.append((frame, call_index))
            elif event in ('return', 'exception'):
                if call_stack:
                    if call_stack[-1][0] is frame:
                        call_stack.pop()
                    else:
                        for idx in range(len(call_stack) - 1, -1, -1):
                            if call_stack[idx][0] is frame:
                                del call_stack[idx:]
                                break
                return
        elif event == 'exception':
            return

        cached_include = self._co_filename_fast_include_cache.get(frame_file_name)
        if cached_include is False:
            return
        if cached_include is None:
            if not frame_file_name or frame_file_name.startswith('<'):
                self._co_filename_fast_include_cache[frame_file_name] = False
                return
            if frame_file_name in self._fast_include_files:
                cached_include = frame_file_name
            else:
                cached_base = self._co_filename_basename_cache.get(frame_file_name)
                if cached_base is None:
                    cached_base = os.path.basename(frame_file_name)
                    self._co_filename_basename_cache[frame_file_name] = cached_base
                if cached_base not in self._fast_include_basenames:
                    self._co_filename_fast_include_cache[frame_file_name] = False
                    return
                cached_abs = self._co_filename_abspath_cache.get(frame_file_name)
                if cached_abs is None:
                    cached_abs = os.path.abspath(frame_file_name)
                    self._co_filename_abspath_cache[frame_file_name] = cached_abs
                if cached_abs not in self._fast_include_files:
                    self._co_filename_fast_include_cache[frame_file_name] = False
                    return
                cached_include = cached_abs
            self._co_filename_fast_include_cache[frame_file_name] = cached_include
        frame_file_name = cached_include

        if self.observed_file:
            if any(frame_file_name.startswith(p) for p in EXCLUDE_PATHS):
                if not (self.allow_external_target and frame_file_name == self.observed_file):
                    return

        # Super fast pre-filter: if we are targeting a method name (not a line number),
        # ignore all calls whose code object name doesn't match the target's last segment.
        if (
            event == 'call'
            and not self.target_is_lineno
            and self._method_name_last
            and code.co_name != self._method_name_last
            and not self._exec_return_only
        ):
            return

        func_start_lineno = code.co_firstlineno
        if self.target_is_lineno:
            if self.target_lineno is not None and func_start_lineno != self.target_lineno:
                return
        else:
            if self._method_name_last and code.co_name != self._method_name_last:
                return
        curr_method_name = self._method_name_cache.get(code)
        if curr_method_name is None:
            curr_method_name = _derive_method_name(frame)
            self._method_name_cache[code] = curr_method_name
        curr_frame_name = f"{frame_file_name}:{curr_method_name}"
        if not hasattr(self, '_lineno_to_method'):
            self._lineno_to_method = {}
        self._lineno_to_method[f"{frame_file_name}:{func_start_lineno}"] = curr_method_name

        if curr_frame_name not in self.frame_counter:
            self.frame_counter[curr_frame_name] = 0
        curr_call_index = None
        curr_frame_id = None
        if self._exec_return_only:
            # In exit-only exec mode we must keep per-frame IDs around to match at return.
            if frame not in self.frame_to_id:
                if event == 'call':
                    self.frame_counter[curr_frame_name] += 1
                    curr_frame_id = f"{curr_frame_name}#{self.frame_counter[curr_frame_name]}"
                    self.frame_to_id[frame] = curr_frame_id
                else:
                    return
            else:
                curr_frame_id = self.frame_to_id[frame]
        else:
            # In normal profile scanning, avoid retaining frame objects for every call.
            # We only need the call index; the focus frame will be saved right before switching.
            self.frame_counter[curr_frame_name] += 1
            curr_call_index = self.frame_counter[curr_frame_name]

        if event == 'call' and frame_file_name == self.observed_file:
            if curr_method_name.split('.')[-1] == self.method_name.split('.')[-1]:
                before = len(self.candidate_method_names)
                self.candidate_method_names.add(curr_method_name)
                if len(self.candidate_method_names) != before:
                    self.state_data['candidate_method_names'] = list(self.candidate_method_names)
                    self._mark_state_dirty()

        if event == 'call':
            if not self.target_frames and not self.pending_condition_frames:
                is_target_frame = False
                if self.target_is_lineno:
                    is_target_frame = (
                        self.target_lineno is not None
                        and func_start_lineno == self.target_lineno
                        and frame_file_name == self.observed_file
                    )
                else:
                    is_target_frame = (
                        frame_file_name == self.observed_file
                        and curr_method_name == self.method_name
                    )
                if is_target_frame and (not self.ignore_frame_index):
                    try:
                        is_target_frame = curr_call_index == self.observed_frame_index
                    except Exception:
                        is_target_frame = False

                if is_target_frame:
                    if self._exec_return_only:
                        return
                    # Performance: if the condition is an entry-time filter (default) and does not
                    # require locals from a specific line, evaluate it in the lightweight profile
                    # stage. If it fails, keep scanning in profile mode instead of switching to
                    # sys.settrace (which is heavier and would start tracing every call in-file).
                    if (
                        self.condition
                        and self.if_eval_lineno is None
                        and self.condition_when in ('entry', 'both')
                    ):
                        if not self.pass_condition_filter(frame, self.condition, event='call'):
                            self.frame_matched_count += 1
                            self.state_data['frame_matched_count'] = self.frame_matched_count
                            self.state_data['condition_matched_count'] = self.condition_matched_count
                            self._mark_state_dirty()
                            return
                        self._prechecked_condition_frames.add(frame)
                    if frame not in self.frame_to_id:
                        if curr_frame_id is None:
                            curr_frame_id = f"{curr_frame_name}#{curr_call_index}"
                        self.frame_to_id[frame] = curr_frame_id
                    if self.counting_mode:
                        return
                    self._switch_to_trace(frame)
                    self.trace(frame, event, arg)
            return

        if self.insert_stmt and self.insert_start_line == -1:
            if curr_frame_id is None and curr_call_index is not None:
                curr_frame_id = f"{curr_frame_name}#{curr_call_index}"
            if self._match_frame_id(self.insert_frame_id, curr_frame_id, func_start_lineno, curr_method_name, frame_file_name):
                if not self.is_executed:
                    self.insert_lineno_excuted_times += 1
                    if self.insert_loop_index is None or self.insert_loop_index == self.insert_lineno_excuted_times:
                        result = exec_in_frame(frame, self.insert_stmt)
                        self.is_executed = True
                        exec_result_path = os.path.join(get_data_dir(), 'exec_result.txt')
                        with open(exec_result_path, 'w') as f:
                            f.write(str(result) if result is not None else '')
                        self._disable_profile()
                        self._profile_active = False

    def write(self, s, force = False):
        if force:
            self._write(s + '\n')
            return
        if self.insert_stmt:
            return
        if self.call_graph_output_path:
            return
        s = u'{self.prefix}{s}\n'.format(**locals())
        self._write(s)

    def _mark_state_dirty(self):
        self._state_dirty = True

    def _record_adi_error(self, message: str) -> None:
        if not message:
            return
        try:
            self.state_data["adi_error_message"] = str(message)
        except Exception:
            return
        self._mark_state_dirty()

    def _flush_state_data(self):
        """Merge in-memory state_data into state.json once at process exit.

        Avoids high-frequency I/O during tracing/profiling.
        """
        if not self._state_dirty:
            return
        try:
            merged = {}
            if os.path.exists(self.frame_status_path):
                with open(self.frame_status_path, 'r') as f:
                    merged = json.load(f) or {}
            merged.update(self.state_data or {})
            with open(self.frame_status_path, 'w') as f:
                json.dump(merged, f, indent=4)
            self._state_dirty = False
        except Exception:
            pass


    def _new_flt_step(self, lineno: int, stmt: str, iter_num: int = None) -> Dict[str, Any]:
        return {
            "lineno": int(lineno) if lineno is not None else 0,
            "stmt": stmt,
            "iter_num": int(iter_num) if iter_num is not None else None,
            "skipped_before": None,
            "new_vars": {},
            "modified_vars": {},
            "callee_frame_id": None,
        }

    def _append_flt_step(self, step: Dict[str, Any]) -> None:
        if not self._flt_data:
            return
        self._flt_steps.append(step)
        self._flt_last_step = step

    def _attach_callee_frame_id(self, callee_frame_id: str) -> None:
        if not self._flt_data or not self._flt_last_step:
            return
        # Match the old text+parser behavior: last 'call:' wins for a step.
        self._flt_last_step["callee_frame_id"] = str(callee_frame_id)

    def _loop_cache_output_line_count(self, steps: List[Dict[str, Any]]) -> int:
        """Approximate the old text protocol's cache 'line count'.

        Old loop cache stored:
        - 1 line per executed source line
        - +1 line per new/modified var (each printed as its own line)

        We use this to preserve "len(cache) > 1" semantics without storing raw strings.
        """
        total = 0
        for step in steps or []:
            total += 1
            try:
                total += len(step.get("new_vars") or {})
            except Exception:
                pass
            try:
                total += len(step.get("modified_vars") or {})
            except Exception:
                pass
        return total

    def _emit_loop_state_cache(self, state: LoopState) -> None:
        """Emit a loop state's cached last-iteration steps into the FLT trace."""
        if not self._flt_data or not state or state.skipped_count <= 0:
            return
        cache_to_output = state.prev_cache or state.cache
        if not cache_to_output:
            return
        first = cache_to_output[0]
        if first.get("skipped_before") is None:
            first["skipped_before"] = int(state.skipped_count)
        for step in cache_to_output:
            self._append_flt_step(step)

    def _write_flt_json(self) -> None:
        """Atomically write flt.json once.

        Must be called at focus end (return/exception unwind) to survive timeout SIGKILL
        during post-hit counting mode.
        """
        if self._flt_written or not self._flt_data:
            return
        try:
            tmp_path = self.flt_json_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._flt_data, f, ensure_ascii=False)
            os.replace(tmp_path, self.flt_json_path)
            self._flt_written = True
        except Exception as exc:
            # Best-effort: do not crash tracing if writing fails, but leave diagnostics in state.json.
            try:
                self.state_data["flt_json_write_error"] = str(exc)
                if not (self.state_data.get("adi_error_message") or "").strip():
                    self._record_adi_error(f"Failed to write flt.json: {exc}")
            except Exception:
                pass



    def _is_internal_frame(self, frame):
        return os.path.abspath(frame.f_code.co_filename) == os.path.abspath(__file__)

    def set_thread_info_padding(self, thread_info):
        current_thread_len = len(thread_info)
        self.thread_info_padding = max(self.thread_info_padding,
                                       current_thread_len)
        return thread_info.ljust(self.thread_info_padding)

    def _match_frame_id(self, target_frame_id, curr_frame_id, func_start_lineno, curr_method_name, frame_file_name):
        """Unified frame ID matching that supports both line number and method name formats.

        Args:
            target_frame_id: The frame ID to match (e.g., "/path/file.py:func#1" or "/path/file.py:42#1")
            curr_frame_id: Current frame ID in method name format (e.g., "/path/file.py:func#1")
            func_start_lineno: Function start line number
            curr_method_name: Current method name
            frame_file_name: Current frame file name

        Returns:
            True if the frame matches, False otherwise
        """
        if not target_frame_id:
            return False

        # Direct string match (fast path)
        if target_frame_id == curr_frame_id:
            return True

        # Parse target frame ID to check format
        try:
            from .utils import parse_frame_id
            target_file, target_method_or_lineno, target_index = parse_frame_id(target_frame_id)
            target_file = os.path.abspath(target_file)

            # Extract current frame index from curr_frame_id
            if '#' not in curr_frame_id:
                return False
            curr_frame_name, curr_index_str = curr_frame_id.rsplit('#', 1)
            try:
                curr_index = int(curr_index_str)
            except ValueError:
                return False

            # File must match (normalize both to absolute paths for comparison)
            frame_file_name_abs = os.path.abspath(frame_file_name)
            if target_file != frame_file_name_abs:
                return False

            # Index must match unless we are scanning any call
            if not self.ignore_frame_index and target_index != curr_index:
                return False

            # Check if target is line number format or method name format
            if isinstance(target_method_or_lineno, int):
                # Line number format: compare function start line
                return func_start_lineno == target_method_or_lineno
            else:
                # Method name format: compare method name
                return target_method_or_lineno == curr_method_name

        except Exception:
            # Fallback to string comparison
            return target_frame_id == curr_frame_id

    def _activate_target_frame(self, frame, curr_frame_id, curr_method_name, reset_depth=False):
        """Activate the current frame as the focus frame.

        Strategy1: tracer no longer emits FLT text. It captures structured FLT data and
        writes it to `flt.json` when the focus frame ends.
        """
        parent_frame = frame.f_back
        self.target_frame_parent_id = None
        if parent_frame:
            parent_id = self.frame_to_id.get(parent_frame)
            if parent_id is None:
                parent_call_index = None
                call_stack = getattr(self.thread_local, "profile_call_stack", None)
                if call_stack:
                    # Expected stack shape during the target 'call' event:
                    # [..., parent_frame, frame]
                    if len(call_stack) >= 2 and call_stack[-2][0] is parent_frame:
                        parent_call_index = call_stack[-2][1]
                    else:
                        for _frm, _idx in reversed(call_stack):
                            if _frm is parent_frame:
                                parent_call_index = _idx
                                break
                if parent_call_index is not None:
                    parent_code = parent_frame.f_code
                    parent_file = parent_code.co_filename
                    if parent_file and not parent_file.startswith('<'):
                        cached_abs = self._co_filename_abspath_cache.get(parent_file)
                        if cached_abs is None:
                            cached_abs = os.path.abspath(parent_file)
                            self._co_filename_abspath_cache[parent_file] = cached_abs
                        parent_file = cached_abs
                        parent_method = self._method_name_cache.get(parent_code)
                        if parent_method is None:
                            parent_method = _derive_method_name(parent_frame)
                            self._method_name_cache[parent_code] = parent_method
                        parent_id = f"{parent_file}:{parent_method}#{parent_call_index}"
            self.target_frame_parent_id = parent_id
        if self.bp_frame_name and self.bp_frame_name in self.frame_counter:
            self.bp_frame_index = self.frame_counter[self.bp_frame_name]

        self.state_data['target_frame_parent_id'] = self.target_frame_parent_id
        self.state_data['bp_frame_id'] = f'{self.bp_frame_name}#{self.bp_frame_index}' if self.bp_frame_name and self.bp_frame_index else None
        self.state_data['curr_frame_id'] = curr_frame_id
        self.state_data['frame_matched_count'] = self.frame_matched_count
        self.state_data['condition_matched_count'] = self.condition_matched_count
        if '#' in str(curr_frame_id):
            try:
                self.matched_frame_index = int(str(curr_frame_id).rsplit('#', 1)[1])
            except ValueError:
                pass

        method_display = f" ({curr_method_name})" if curr_method_name else ""
        parent_display = ""
        if self.target_frame_parent_id and hasattr(self, '_lineno_to_method'):
            parent_key = self.target_frame_parent_id.rsplit('#', 1)[0] if '#' in str(self.target_frame_parent_id) else None
            if parent_key and parent_key in self._lineno_to_method:
                parent_display = f" ({self._lineno_to_method[parent_key]})"

        with open(self.frame_status_path, 'w') as f:
            json.dump(self.state_data, f, indent=4)

        # Initialize FLT JSON capture for this focus frame.
        self._focus_frame = frame
        self._flt_steps = []
        self._flt_last_step = None
        self._flt_written = False
        self._flt_data = {
            "schema_version": 1,
            "frame_id": str(curr_frame_id),
            "caller_frame_id": self.target_frame_parent_id,
            "args": {},
            "return_value": None,
            "trace": self._flt_steps,
        }

        self.target_frames.add(frame)
        self.start_times[frame] = datetime_module.datetime.now()
        if reset_depth:
            thread_global.depth = -1

    def trace(self, frame, event, arg):
        frame_file_name = frame.f_code.co_filename
        if not hasattr(thread_global, 'depth'):
            thread_global.depth = 0
        if self.call_graph_output_path and event not in ('call', 'return', 'exception'):
            return None
        if self.observed_file and not self.target_frames:
            if frame_file_name and not frame_file_name.startswith('<'):
                cached_base = self._co_filename_basename_cache.get(frame_file_name)
                if cached_base is None:
                    cached_base = os.path.basename(frame_file_name)
                    self._co_filename_basename_cache[frame_file_name] = cached_base
                if cached_base not in self._fast_include_basenames:
                    return None
            else:
                return None

        # Normalize filename once (cheap per distinct file) to make comparisons stable.
        # Many performance-critical checks depend on early filename filtering.
        if frame_file_name and not frame_file_name.startswith('<'):
            cached_abs = self._co_filename_abspath_cache.get(frame_file_name)
            if cached_abs is None:
                cached_abs = os.path.abspath(frame_file_name)
                self._co_filename_abspath_cache[frame_file_name] = cached_abs
            frame_file_name = cached_abs

        # Fast pre-filter: before we hit the focus frame (and when not collecting a call-graph),
        # ignore all frames outside a tiny set of relevant files.
        if self.observed_file and not self.target_frames:
            if frame_file_name not in self._fast_include_files:
                return None
        if self.observed_file:
            if any(frame_file_name.startswith(p) for p in EXCLUDE_PATHS):
                if not (self.allow_external_target and frame_file_name == self.observed_file):
                    return None
            code = frame.f_code
            curr_method_name = self._method_name_cache.get(code)
            if curr_method_name is None:
                curr_method_name = _derive_method_name(frame)
                self._method_name_cache[code] = curr_method_name
            func_start_lineno = frame.f_code.co_firstlineno
            # Use method name as primary identifier to avoid collision when multiple functions start at same line
            curr_frame_name = f"{frame_file_name}:{curr_method_name}"
            # Store method name for display purposes
            if not hasattr(self, '_lineno_to_method'):
                self._lineno_to_method = {}
            self._lineno_to_method[f"{frame_file_name}:{func_start_lineno}"] = curr_method_name

            if event == 'call':
                # Initialize and increment counter for call events
                if curr_frame_name not in self.frame_counter:
                    self.frame_counter[curr_frame_name] = 0
                if not (frame in self.frame_line_executed or frame in self.frame_to_id):
                    self.frame_counter[curr_frame_name] += 1
            else:
                # For non-call events, ensure counter exists with default value 1
                if curr_frame_name not in self.frame_counter:
                    self.frame_counter[curr_frame_name] = 1

            # In counting mode, only count target function calls, skip everything else
            if self.counting_mode:
                return self.trace

            if frame not in self.frame_to_id:
                curr_frame_id = f"{curr_frame_name}#{self.frame_counter[curr_frame_name]}"
                self.frame_to_id[frame] = curr_frame_id
            else:
                curr_frame_id = self.frame_to_id[frame]

            if event == 'call' and frame_file_name == self.observed_file:
                if curr_method_name.split('.')[-1] == self.method_name.split('.')[-1]:
                    before = len(self.candidate_method_names)
                    self.candidate_method_names.add(curr_method_name)
                    if len(self.candidate_method_names) != before:
                        self.state_data['candidate_method_names'] = list(self.candidate_method_names)
                        self._mark_state_dirty()



            if self.insert_stmt and self._match_frame_id(self.insert_frame_id, curr_frame_id, func_start_lineno, curr_method_name, frame_file_name):
                self.allow_trace_skipped = False
                if self.insert_start_line <= frame.f_lineno <= self.insert_end_line and not self.is_executed:
                    self.insert_lineno_excuted_times += 1
                    if self.insert_loop_index is None or self.insert_loop_index == self.insert_lineno_excuted_times:

                        result = exec_in_frame(frame, self.insert_stmt)
                        self.is_executed = True
                        # Write exec result to file instead of stderr (cleaner output)
                        exec_result_path = os.path.join(get_data_dir(), 'exec_result.txt')
                        with open(exec_result_path, 'w') as f:
                            f.write(str(result) if result is not None else '')
            else:
                self.allow_trace_skipped = True


            if not self.target_frames and not self.pending_condition_frames:
                # Use unified frame ID matching (supports both line number and method name formats)
                if self._match_frame_id(self.target_frame_id, curr_frame_id, func_start_lineno, curr_method_name, frame_file_name):
                    # Record frame ID match
                    self.frame_matched_count += 1
                    self.state_data['frame_matched_count'] = self.frame_matched_count
                    self.state_data['condition_matched_count'] = self.condition_matched_count
                    self._mark_state_dirty()

                    if self.condition and self.if_eval_lineno is not None:
                        # Defer condition evaluation to a specific line event
                        self.pending_condition_frames.add(frame)
                        self._mark_state_dirty()
                        return self.trace

                    # Check condition (only for the matching call number)
                    if self.condition:
                        if event == 'call' and frame in self._prechecked_condition_frames:
                            self._prechecked_condition_frames.discard(frame)
                        else:
                            if not self.pass_condition_filter(frame, self.condition):
                                return None if self.allow_trace_skipped else self.trace
                        # Record condition match
                        self.condition_matched_count += 1
                        self.state_data['condition_matched_count'] = self.condition_matched_count
                        self._mark_state_dirty()
                    self._activate_target_frame(frame, curr_frame_id, curr_method_name, reset_depth=True)
                else:
                    return None if self.allow_trace_skipped else self.trace
            else:
                is_in_scope = self.is_in_code_scope(frame)
                if frame not in self.target_frames and is_in_scope:
                    if self._match_frame_id(self.target_frame_id, curr_frame_id, func_start_lineno, curr_method_name, frame_file_name) or (self.condition and self.pass_condition_filter(frame, self.condition)):
                        self.target_frames.add(frame)

                elif frame in self.target_frames and not is_in_scope:
                    if event == 'return' or event == 'exception':
                        thread_global.depth -= 1
                        # self.target_frames.discard(frame)
                    return self.trace
        
        if frame in self.pending_condition_frames:
            if event == 'line' and frame.f_lineno == self.if_eval_lineno:
                iter_num = self.frame_line_executed.get(frame, {}).get(frame.f_lineno, 0)
                if self.observed_loop_index and iter_num != self.observed_loop_index - 1:
                    self.record_frame_line_executed(frame)
                    return self.trace
                if self.pass_line_condition_filter(frame, self.condition):
                    self.condition_matched_count += 1
                    self.pending_condition_frames.discard(frame)
                    self._force_next_line_frames.add(frame)
                    self._activate_target_frame(frame, curr_frame_id, curr_method_name, reset_depth=False)
                else:
                    self.record_frame_line_executed(frame)
                    return self.trace
            else:
                if event == 'line':
                    self.record_frame_line_executed(frame)
                if event in ('return', 'exception'):
                    self.pending_condition_frames.discard(frame)
                return self.trace

        if not (frame.f_code in self.target_codes or frame in self.target_frames):
            if self._is_internal_frame(frame):
                return None
            _frame_candidate = frame
            back_depth = self.depth + 1 if self.depth_expanded else self.depth
            for i in range(1, back_depth):
                _frame_candidate = _frame_candidate.f_back
                if _frame_candidate is None:
                    return self.trace
                elif _frame_candidate.f_code in self.target_codes or (_frame_candidate in self.target_frames and self.is_in_code_scope(_frame_candidate)):
                    if self.loop:
                        if self.is_skip_loop(_frame_candidate, max_loop_times = self.loop + 1):
                            if self.call_graph_output_path and event == 'call' and not self.is_last_call_skip:
                                self.call_infos.append(
                                    {
                                        "kind": "skip",
                                        "depth": thread_global.depth + 1,
                                        "reason": "loop_skip",
                                        "message": "Skipping repeated (loop) calling details.",
                                    }
                                )
                                self.is_last_call_skip = True
                            return None
                    if self.depth_expanded:
                        if i == back_depth - 1:
                            if event != 'call':
                                return None if self.allow_trace_skipped else self.trace
                            else:
                                if self.loop and self.is_skip_loop(frame):
                                    self.record_frame_line_executed(frame)
                                    return None if self.allow_trace_skipped else self.trace
                                self.record_frame_line_executed(frame)
                                self._attach_callee_frame_id(curr_frame_id)
                                return None if self.allow_trace_skipped else self.trace

                    break
            else:
                return self.trace

        indent = ' ' * 4 * thread_global.depth
        _FOREGROUND_BLUE = self._FOREGROUND_BLUE
        _FOREGROUND_CYAN = self._FOREGROUND_CYAN
        _FOREGROUND_GREEN = self._FOREGROUND_GREEN
        _FOREGROUND_MAGENTA = self._FOREGROUND_MAGENTA
        _FOREGROUND_RED = self._FOREGROUND_RED
        _FOREGROUND_RESET = self._FOREGROUND_RESET
        _FOREGROUND_YELLOW = self._FOREGROUND_YELLOW
        _STYLE_BRIGHT = self._STYLE_BRIGHT
        _STYLE_DIM = self._STYLE_DIM
        _STYLE_NORMAL = self._STYLE_NORMAL
        _STYLE_RESET_ALL = self._STYLE_RESET_ALL

        force_no_skip = frame in self._force_next_line_frames
        if force_no_skip:
            self._force_next_line_frames.discard(frame)

        if self.loop and not force_no_skip:
            if event != 'return' and event != 'exception' and event != 'call':
                # Detect if current line is a loop header
                loop_lineno = self._detect_loop_header(frame)
                if loop_lineno:
                    # If we're entering a new loop, flush the old loop's cache first
                    old_loop = self.current_loop_lineno.get(frame, 0)
                    if old_loop and old_loop != loop_lineno:
                        key = (frame, old_loop)
                        if key in self.loop_states and self.loop_states[key].skipped_count > 0:
                            state = self.loop_states[key]
                            if frame is self._focus_frame:
                                self._emit_loop_state_cache(state)
                            del self.loop_states[key]
                    self.current_loop_lineno[frame] = loop_lineno

                # Get current loop's lineno (use detected or last known)
                current_loop = self.current_loop_lineno.get(frame, 0)

                # Get iteration number for current line
                iter_num = self.frame_line_executed.get(frame, {}).get(frame.f_lineno, 0)
                self._current_iter_num = iter_num

                if self.is_skip_loop(frame):
                    # Skip mode: cache output for last iteration
                    self.record_frame_line_executed(frame)
                    new_iter = self.frame_line_executed[frame][frame.f_lineno]

                    # Skip caching return statements to avoid duplication
                    source_path, source = get_path_and_source_from_frame(frame)
                    source_line = _safe_get_source_line(source, frame.f_lineno)
                    if source_line.strip().startswith('return'):
                        # Bug fix: flush cache before return to show skipped iterations
                        self._flush_loop_cache(frame, thread_global.depth)
                        self.is_last_skip = True
                        return self.trace

                    # Bug fix: detect break/continue and flush cache for current loop only
                    stripped_line = source_line.strip()
                    if stripped_line == 'break' or stripped_line == 'continue' or stripped_line.startswith('break ') or stripped_line.startswith('continue '):
                        self._flush_loop_cache(frame, thread_global.depth, current_loop)
                        self.is_last_skip = False  # Reset since we're exiting the loop

                    # Get or create loop state for current loop
                    if current_loop:
                        state = self._get_loop_state(frame, current_loop)

                        # Check if this is a new iteration (iter number increased on loop header)
                        if loop_lineno and new_iter != state.current_iter:
                            # Save current cache as the previous complete iteration
                            if self._loop_cache_output_line_count(state.cache) > 1:
                                state.prev_cache = state.cache[:]
                                state.skipped_count += 1
                            state.current_iter = new_iter
                            state.cache = []

                        # Cache this line for potential output later
                        iter_num = state.current_iter if state.current_iter > 0 else None
                        cached_step = self._new_flt_step(
                            lineno=frame.f_lineno,
                            stmt=source_line.strip(),
                            iter_num=iter_num,
                        )

                        # Cache variable changes (same semantics as the old text cache: changes
                        # observed at this line event belong to the previously executed line).
                        old_local_reprs = self.frame_to_local_reprs.get(frame, {})
                        local_reprs = get_local_reprs(
                            frame,
                            watch=self.watch,
                            custom_repr=self.custom_repr,
                            max_length=self.max_variable_length,
                            normalize=self.normalize,
                            full_watch=self.full_watch,
                            full_watch_max_length=self.full_watch_max_length,
                        )
                        self.frame_to_local_reprs[frame] = local_reprs

                        for name, value_repr in local_reprs.items():
                            if name not in old_local_reprs:
                                cached_step["new_vars"][name] = value_repr
                            elif old_local_reprs[name] != value_repr:
                                cached_step["modified_vars"][name] = value_repr

                        state.cache.append(cached_step)

                    self.is_last_skip = True
                    return self.trace
                else:
                    # Not skipping: output skip message and cached last iteration if we were skipping before
                    if self.is_last_skip:
                        # Flush all loop caches for this frame
                        keys_to_flush = [k for k in self.loop_states if k[0] == frame]
                        for key in keys_to_flush:
                            state = self.loop_states[key]
                            if state.skipped_count > 0:
                                if frame is self._focus_frame:
                                    self._emit_loop_state_cache(state)
                                # Reset state for this loop
                                state.cache = []
                                state.prev_cache = []
                                state.skipped_count = 0
                    self.is_last_skip = False
                    self.record_frame_line_executed(frame)
        #                                                                     #
        ### Finished checking whether we should trace this line. ##############
        if event == 'call':
            thread_global.depth += 1

        indent = ' ' * 4 * thread_global.depth


        ### Making timestamp: #################################################
        #                                                                     #
        if self.normalize:
            timestamp = ' ' * 15
        elif self.relative_time:
            try:
                start_time = self.start_times[frame]
            except KeyError:
                start_time = self.start_times[frame] = \
                                                 datetime_module.datetime.now()
            duration = datetime_module.datetime.now() - start_time
            timestamp = pycompat.timedelta_format(duration)
        else:
            timestamp = pycompat.time_isoformat(
                datetime_module.datetime.now().time(),
                timespec='microseconds'
            )
        #                                                                     #
        ### Finished making timestamp. ########################################

        line_no = frame.f_lineno
        source_path, source = get_path_and_source_from_frame(frame)
        source_path = source_path if not self.normalize else os.path.basename(source_path)
        if self.last_source_path != source_path:
            # self.write(u'{_FOREGROUND_YELLOW}{_STYLE_DIM}{indent}Source path:... '
            #            u'{_STYLE_NORMAL}{source_path}'
            #            u'{_STYLE_RESET_ALL}'.format(**locals()))
            self.last_source_path = source_path
        source_line = _safe_get_source_line(source, line_no)
        thread_info = ""
        if self.thread_info:
            if self.normalize:
                raise NotImplementedError("normalize is not supported with "
                                          "thread_info")
            current_thread = threading.current_thread()
            thread_info = "{ident}-{name} ".format(
                ident=current_thread.ident, name=current_thread.name)
        thread_info = self.set_thread_info_padding(thread_info)

        ### Reporting newish and modified variables: ##########################
        #                                                                     #
        if self.call_graph_output_path:
            old_local_reprs = {}
            local_reprs = {}
        else:
            old_local_reprs = self.frame_to_local_reprs.get(frame, {})
            self.frame_to_local_reprs[frame] = local_reprs = get_local_reprs(
                frame,
                watch=self.watch,
                custom_repr=self.custom_repr,
                max_length=self.max_variable_length,
                normalize=self.normalize,
                full_watch=self.full_watch,
                full_watch_max_length=self.full_watch_max_length,
            )

            # FLT JSON capture: attach variable diffs to the previous recorded step.
            if frame is self._focus_frame and self._flt_data is not None:
                new_vars: Dict[str, str] = {}
                modified_vars: Dict[str, str] = {}
                for name, value_repr in local_reprs.items():
                    if name not in old_local_reprs:
                        new_vars[name] = value_repr
                    elif old_local_reprs[name] != value_repr:
                        modified_vars[name] = value_repr

                if event == 'call':
                    # Mirror the old 'Argument value:' behavior: store as FLT args.
                    self._flt_data["args"] = new_vars
                else:
                    if self._flt_last_step is not None:
                        if new_vars:
                            self._flt_last_step["new_vars"].update(new_vars)
                        if modified_vars:
                            self._flt_last_step["modified_vars"].update(modified_vars)


        if event == 'call' and source_line.lstrip().startswith('@'):
            for candidate_line_no in itertools.count(line_no):
                try:
                    candidate_source_line = source[candidate_line_no - 1]
                except IndexError:
                    break

                if candidate_source_line.lstrip().startswith('def'):
                    # Found the def line!
                    line_no = candidate_line_no
                    source_line = candidate_source_line
                    break
                
        
        code_byte = frame.f_code.co_code[frame.f_lasti]
        if not isinstance(code_byte, int):
            code_byte = ord(code_byte)
        ended_by_exception = (
            event == 'return'
            and arg is None
            and opcode.opname[code_byte] not in RETURN_OPCODES
        )

        # If the focus call ends via exception unwinding, record a stable marker for the
        # parent process (CLI) to surface, especially important when --no-count triggers
        # an early SystemExit(0) that can hide the uncaught exception.
        if ended_by_exception and self.observed_file and frame in self.target_frames:
            self.state_data['focus_ended_by_exception'] = True
            try:
                self.state_data['focus_ended_frame_id'] = str(curr_frame_id)
            except Exception:
                pass
            self._mark_state_dirty()


        if event != 'return':
            # Bug 3 fix: Skip outputting a separate 'return' step (line event already captured it).
            # Add iteration marker for loops (only when line executed more than once).
            iter_num = None
            if self.loop and event == 'line' and hasattr(self, '_current_iter_num') and self._current_iter_num >= 1:
                iter_num = self._current_iter_num + 1
            if frame is self._focus_frame and self._flt_data is not None:
                self._append_flt_step(
                    self._new_flt_step(
                        lineno=line_no,
                        stmt=source_line.strip(),
                        iter_num=iter_num,
                    )
                )

        if not ended_by_exception:
            return_value_repr = utils.get_shortish_repr(arg,
                                                        custom_repr=self.custom_repr,
                                                        max_length=self.max_variable_length,
                                                        normalize=self.normalize,
                                                        )


        if self.call_graph_output_path:
            if event == 'call':
                node = {
                    "kind": "call",
                    "depth": thread_global.depth,
                    "frame_id": str(curr_frame_id),
                    "signature": self._format_call_signature(source_line),
                    "return_value": None,
                    "ended_by_exception": False,
                }
                self.call_frames[curr_frame_id] = node
                self.call_infos.append(node)
                self.is_last_call_skip = False

            if event == 'return':
                node = self.call_frames.get(curr_frame_id)
                if not node:
                    return self.trace
                if ended_by_exception:
                    node["ended_by_exception"] = True
                else:
                    node["return_value"] = return_value_repr


        if event == 'return':
            # Flush loop cache before return output
            if self.loop and self.is_last_skip:
                self._flush_loop_cache(frame, thread_global.depth)
                self.is_last_skip = False

            if not self.observed_file or frame not in self.target_frames:
                self.frame_to_local_reprs.pop(frame, None)
                self.start_times.pop(frame, None)
            thread_global.depth -= 1

            # Execute insert_stmt at function return if start=-1 (exit mode)
            if self.insert_stmt and self.insert_start_line == -1 and self._match_frame_id(self.insert_frame_id, curr_frame_id, func_start_lineno, curr_method_name, frame_file_name):
                if not self.is_executed:
                    self.insert_lineno_excuted_times += 1
                    if self.insert_loop_index is None or self.insert_loop_index == self.insert_lineno_excuted_times:
                        result = exec_in_frame(frame, self.insert_stmt)
                        self.is_executed = True
                        exec_result_path = os.path.join(get_data_dir(), 'exec_result.txt')
                        with open(exec_result_path, 'w') as f:
                            f.write(str(result) if result is not None else '')

            # Check return condition if --on-return is set
            if self.condition and self.condition_when in ('return', 'both'):
                if frame in self.target_frames:
                    if not self.pass_condition_filter(frame, self.condition, event='return', return_value=arg):
                        # Condition not met at return, skip output
                        self.target_frames.discard(frame)
                        return self.trace

            # Persist FLT immediately when the focus ends (must survive timeout SIGKILL during
            # post-hit counting mode). Do not rely on atexit.
            if frame is self._focus_frame and self._flt_data is not None:
                if not ended_by_exception:
                    try:
                        self._flt_data["return_value"] = return_value_repr
                    except Exception:
                        pass
                self._write_flt_json()

            if self.observed_file:
                if frame in self.target_frames:
                    self.manual_exit(frame)
                    # Record the index we stopped at
                    if self.matched_frame_index is None:
                        self.matched_frame_index = self.observed_frame_index
                    if self.call_graph_output_path:
                        self._write_call_graph()
                        self.stop()
                        return None

                if not self.call_graph_output_path:
                    if not self.count_total_calls:
                        self.stop()
                        raise SystemExit(0)
                    if not self.counting_mode:
                        # Enter counting mode instead of exiting
                        self.counting_mode = True
                        self.saved_stdout = sys.stdout
                        self.saved_stderr = sys.stderr
                        sys.stdout = open(os.devnull, 'w')
                        sys.stderr = open(os.devnull, 'w')
                        # Register atexit handler to write final stats
                        atexit.register(self._write_final_stats)
                        # Reduce overhead during post-hit counting: switch back to profile-only.
                        try:
                            self.stop()
                            self._profile_active = True
                            self._enable_profile(self.profile)
                        except Exception:
                            pass
                        return None

        if event == 'exception':
            # Flush loop cache before exception output (Bug fix: prevent memory leak)
            if self.loop and self.is_last_skip:
                self._flush_loop_cache(frame, thread_global.depth)
                self.is_last_skip = False
            thread_global.depth -= 1
            exception = '\n'.join(traceback.format_exception_only(*arg[:2])).strip()
            if self.max_variable_length:
                exception = utils.truncate(exception, self.max_variable_length)
            # Best-effort: capture the last exception seen while the focus frame is active.
            # This lets the CLI surface a clear warning even when the subprocess exits early.
            if self.observed_file and self.target_frames:
                self.state_data['focus_exception'] = exception
                try:
                    self.state_data['focus_exception_file'] = frame_file_name
                except Exception:
                    pass
                try:
                    self.state_data['focus_exception_lineno'] = int(frame.f_lineno)
                except Exception:
                    pass
                try:
                    self.state_data['focus_exception_frame_id'] = self.frame_to_id.get(frame, None)
                except Exception:
                    pass
                self._mark_state_dirty()
            if self.observed_file and not self.observed_frame_index and not self.condition:
                if frame in self.target_frames:
                    self.manual_exit(frame)

        return self.trace

    def pass_condition_filter(self, frame, condition, event='call', return_value=None):
        """Check if condition passes.

        Args:
            frame: The current frame
            condition: The condition expression to evaluate
            event: 'call' for entry, 'return' for return
            return_value: The return value (only available when event='return')

        Returns:
            True if condition passes, False otherwise
        """
        if condition is None:
            return True

        # Check if we should evaluate at this event
        if event == 'call' and self.condition_when == 'return':
            return True  # Skip entry check, will check at return
        if event == 'return' and self.condition_when == 'entry':
            return True  # Already checked at entry

        if condition == self.condition and self._condition_compile_error is not None:
            if not self.condition_error_printed:
                msg = f'Syntax error in condition "{condition}": {self._condition_compile_error}'
                self._record_adi_error(msg)
                self.write(msg)
                self.condition_error_printed = True
            return False

        try:
            frame_locals = frame.f_locals
            code = frame.f_code
            cached_arginfo = self._condition_arginfo_cache.get(code)
            if cached_arginfo is None:
                posonly = getattr(code, "co_posonlyargcount", 0)
                argcount = getattr(code, "co_argcount", 0)
                kwonly = getattr(code, "co_kwonlyargcount", 0)
                n_args = posonly + argcount + kwonly
                varnames = code.co_varnames
                arg_names = varnames[:n_args]
                i = n_args
                varargs_name = None
                varkw_name = None
                if code.co_flags & inspect.CO_VARARGS:
                    if i < len(varnames):
                        varargs_name = varnames[i]
                    i += 1
                if code.co_flags & inspect.CO_VARKEYWORDS:
                    if i < len(varnames):
                        varkw_name = varnames[i]
                cached_arginfo = (arg_names, varargs_name, varkw_name)
                self._condition_arginfo_cache[code] = cached_arginfo

            arg_names, varargs_name, varkw_name = cached_arginfo
            context = {}
            for name in arg_names:
                if name in frame_locals:
                    context[name] = frame_locals[name]
            if varargs_name and varargs_name in frame_locals:
                context[varargs_name] = frame_locals[varargs_name]
            if varkw_name and varkw_name in frame_locals:
                context[varkw_name] = frame_locals[varkw_name]

            # Add _return variable for genuine "return" events (including return None).
            # Note: CPython can emit a "return" trace event with arg=None when unwinding due
            # to an exception; guard by checking whether the last executed opcode is a real
            # return/yield opcode.
            if event == 'return':
                ended_by_exception = False
                if return_value is None:
                    code_byte = frame.f_code.co_code[frame.f_lasti]
                    if not isinstance(code_byte, int):
                        code_byte = ord(code_byte)
                    ended_by_exception = opcode.opname[code_byte] not in RETURN_OPCODES
                if not ended_by_exception:
                    context['_return'] = return_value

            frame_globals = frame.f_globals
            if condition == self.condition and self._condition_code is not None:
                result = eval(self._condition_code, frame_globals, context)
            else:
                result = eval(condition, frame_globals, context)

            return bool(result)

        except SyntaxError as e:
            if not self.condition_error_printed:
                msg = f'Syntax error in condition "{condition}": {e}'
                self._record_adi_error(msg)
                self.write(msg)
                self.condition_error_printed = True
            return False
        except NameError as e:
            if not self.condition_error_printed:
                msg = f'Variable not found in condition "{condition}": {e}'
                self._record_adi_error(msg)
                self.write(msg)
                self.condition_error_printed = True
            return False
        except Exception as e:
            if not self.condition_error_printed:
                msg = f'Condition evaluation failed: {e}'
                self._record_adi_error(msg)
                self.write(msg)
                self.condition_error_printed = True
            return False

    def pass_line_condition_filter(self, frame, condition):
        """Check condition at a specific line using full locals."""
        if condition is None:
            return True
        if condition == self.condition and self._condition_compile_error is not None:
            if not self.condition_error_printed:
                msg = f'Syntax error in condition "{condition}": {self._condition_compile_error}'
                self._record_adi_error(msg)
                self.write(msg)
                self.condition_error_printed = True
            return False
        try:
            frame_globals = frame.f_globals
            context = frame.f_locals
            if condition == self.condition and self._condition_code is not None:
                result = eval(self._condition_code, frame_globals, context)
            else:
                result = eval(condition, frame_globals, context)
            return bool(result)
        except SyntaxError as e:
            if not self.condition_error_printed:
                msg = f'Syntax error in condition "{condition}": {e}'
                self._record_adi_error(msg)
                self.write(msg)
                self.condition_error_printed = True
            return False
        except NameError as e:
            if not self.condition_error_printed:
                msg = f'Variable not found in condition "{condition}": {e}'
                self._record_adi_error(msg)
                self.write(msg)
                self.condition_error_printed = True
            return False
        except Exception as e:
            if not self.condition_error_printed:
                msg = f'Condition evaluation failed: {e}'
                self._record_adi_error(msg)
                self.write(msg)
                self.condition_error_printed = True
            return False


    def manual_exit(self, frame):
        self.target_frames.discard(frame)
        
        self.frame_to_local_reprs.pop(frame, None)
        ### Writing elapsed time: #############################################
        #                                                                     #
        _FOREGROUND_YELLOW = self._FOREGROUND_YELLOW
        _STYLE_DIM = self._STYLE_DIM
        _STYLE_NORMAL = self._STYLE_NORMAL
        _STYLE_RESET_ALL = self._STYLE_RESET_ALL

        start_time = self.start_times.pop(frame, None)
        if start_time is None:
            return
        duration = datetime_module.datetime.now() - start_time
        elapsed_time_string = pycompat.timedelta_format(duration)
        indent = ' ' * 4 * (thread_global.depth)

    def _write_final_stats(self):
        """Write final call count stats to state.json (called via atexit)."""
        try:
            # Restore stdout/stderr
            if self.saved_stdout:
                sys.stdout = self.saved_stdout
            if self.saved_stderr:
                sys.stderr = self.saved_stderr

            # Get total calls for target function
            total_calls = self.frame_counter.get(self.target_frame_name, 0)

            # Update state.json with total calls
            with open(self.frame_status_path, 'r') as f:
                state_data = json.load(f)
            state_data['total_calls'] = total_calls
            state_data['matched_frame_index'] = self.matched_frame_index
            with open(self.frame_status_path, 'w') as f:
                json.dump(state_data, f, indent=4)
        except Exception:
            pass  # Silently fail in atexit handler

    def _write_call_graph(self):
        """Write call graph data once (used by call-tree)."""
        if not self.call_graph_output_path or self._call_graph_written:
            return
        try:
            with open(self.call_graph_output_path, 'w') as f:
                payload = {
                    "schema": "adi.call_graph",
                    "version": 1,
                    "calls": self.call_infos,
                }
                json.dump(payload, f, indent=4)
            self._call_graph_written = True
        except Exception:
            pass

    @staticmethod
    def _format_call_signature(source_line: str) -> str:
        sig = (source_line or "").strip()
        if sig.startswith("async def "):
            sig = sig[len("async def "):]
        elif sig.startswith("def "):
            sig = sig[len("def "):]
        sig = sig.strip()
        if sig.endswith(":"):
            sig = sig[:-1]
        return sig.strip()

    def is_in_code_scope(self, frame):
        return  _derive_method_name(frame) == self.method_name and os.path.abspath(frame.f_code.co_filename) == self.observed_file


                
    def is_skip_loop(self, frame, max_loop_times = None):
        looped_times = 0
        max_loop_times = max_loop_times if max_loop_times is not None else self.loop

        if frame in self.frame_line_executed and frame.f_lineno in self.frame_line_executed[frame]:
            looped_times = self.frame_line_executed[frame][frame.f_lineno]

        # When observed_loop_index is set, only show that specific iteration
        if self.observed_loop_index:
            if looped_times == self.observed_loop_index - 1:
                return False
            else:
                return True

        # For first execution of this line, don't skip
        if looped_times == 0:
            return False

        if looped_times >= max_loop_times:
            return True

        return False

    def record_frame_line_executed(self, frame):
        if frame not in self.frame_line_executed:
            self.frame_line_executed[frame] = {}
        if frame.f_lineno not in self.frame_line_executed[frame]:
            self.frame_line_executed[frame][frame.f_lineno] = 0
        self.frame_line_executed[frame][frame.f_lineno] += 1

    def _detect_loop_header(self, frame) -> int:
        """Detect if current line is a loop header (for/while). Returns lineno or 0."""
        source_path, source = get_path_and_source_from_frame(frame)
        line = _safe_get_source_line(source, frame.f_lineno).strip()
        if line.startswith('for ') or line.startswith('while '):
            return frame.f_lineno
        return 0

    def _get_loop_state(self, frame, loop_lineno: int) -> LoopState:
        """Get or create LoopState for a specific loop."""
        key = (frame, loop_lineno)
        if key not in self.loop_states:
            self.loop_states[key] = LoopState()
        return self.loop_states[key]

    def _flush_loop_cache(self, frame, depth: int, loop_lineno: int = None):
        """Flush loop caches for a frame, outputting skipped messages.

        Args:
            frame: The frame to flush caches for
            depth: Current indentation depth
            loop_lineno: If specified, only flush this specific loop. Otherwise flush all loops.
        """
        if loop_lineno:
            # Flush only the specified loop
            key = (frame, loop_lineno)
            if key in self.loop_states:
                state = self.loop_states[key]
                if state.skipped_count > 0 and frame is self._focus_frame:
                    self._emit_loop_state_cache(state)
                del self.loop_states[key]
                if self.current_loop_lineno.get(frame) == loop_lineno:
                    self.current_loop_lineno.pop(frame, None)
        else:
            # Flush all loops for this frame
            keys_to_flush = [k for k in self.loop_states if k[0] == frame]
            for key in keys_to_flush:
                state = self.loop_states[key]
                if state.skipped_count > 0 and frame is self._focus_frame:
                    self._emit_loop_state_cache(state)
                del self.loop_states[key]
            self.current_loop_lineno.pop(frame, None)

    
    def _excepthook(self, tp, val, tb):
        root = val.__cause__ or (val.__context__ if not getattr(val, "__suppress_context__", False) else None) or val
        tb = root.__traceback__ or tb
        tb_head_id = id(tb)

        inn = tb
        while inn and inn.tb_next:
            inn = inn.tb_next

        if inn is None:
            # print(f"[UNHANDLED] {tp.__name__}: {val} (no runtime traceback)")
            return

        f = inn.tb_frame
        exception_frame_id = self.frame_to_id.get(f, None)
        exception_type = type(root).__name__ if root is not None else getattr(tp, "__name__", None)
        try:
            exception_message = str(root) if root is not None else str(val)
        except Exception:
            exception_message = None
        exception_file = f.f_code.co_filename
        if exception_file and not exception_file.startswith('<'):
            exception_file = os.path.abspath(exception_file)
        try:
            exception_func = _derive_method_name(f)
        except Exception:
            exception_func = None
        try:
            exception_lineno = int(getattr(inn, "tb_lineno", None) or 0) or None
        except Exception:
            exception_lineno = None

        try:
            with open(self.frame_status_path, 'r') as f_state:
                state_data = json.load(f_state) or {}
        except Exception:
            state_data = {}

        state_data['exception_frame'] = exception_frame_id
        state_data['exception_type'] = exception_type
        state_data['exception_message'] = exception_message
        state_data['exception_file'] = exception_file
        state_data['exception_lineno'] = exception_lineno
        state_data['exception_func'] = exception_func
        try:
            with open(self.frame_status_path, 'w') as f_state:
                json.dump(state_data, f_state, indent=4)
        except Exception:
            pass


        traceback.print_exception(tp, val, tb, file=sys.__stderr__, chain=True)
        sys.__stderr__.flush()

        return 
