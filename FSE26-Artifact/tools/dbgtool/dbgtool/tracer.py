# Copyright 2019 Ram Rachum and collaborators.
# This program is distributed under the MIT license.

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
from .variables import CommonVariable, Exploding, BaseVariable
from . import utils, pycompat
if pycompat.PY2:
    from io import open
from .utils import _derive_method_name, parse_frame_id, normalize_frame_id
import site


EXCLUDE_PATHS = []
EXCLUDE_PATHS.extend(site.getsitepackages())     # 系统 site-packages
EXCLUDE_PATHS.append(site.getusersitepackages()) # 用户 site-packages
EXCLUDE_PATHS.append(os.path.dirname(os.__file__))  # stdlib 目录



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


def get_local_reprs(frame, watch=(), custom_repr=(), max_length=None, normalize=False):
    code = frame.f_code
    vars_order = (code.co_varnames + code.co_cellvars + code.co_freevars +
                  tuple(frame.f_locals.keys()))

    result_items = [(key, utils.get_shortish_repr(value, custom_repr,
                                                  max_length, normalize))
                    for key, value in frame.f_locals.items()]
    result_items.sort(key=lambda key_value: vars_order.index(key_value[0]))
    result = collections.OrderedDict(result_items)

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
                 max_variable_length=100, normalize=False, relative_time=False, color=False, target_frame_id = None,
                 observed_loop_index = None, call_graph_mode = False,  condition = None):
        
        self.candidate_method_names = set()

        self.observed_file, self.method_name, self.observed_frame_index = parse_frame_id(target_frame_id)
        self.observed_file = os.path.abspath(self.observed_file)
        self.target_frame_id = f'{os.path.abspath(self.observed_file)}:{self.method_name}#{self.observed_frame_index}'

        self.is_executed = False
        self.insert_stmt = None
        insert_stmt_path = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'insert_stmt.json'))
        if os.path.exists(insert_stmt_path):
            with open(insert_stmt_path, 'r') as f:
                insert_data = json.load(f)
            self.insert_stmt = insert_data.get('stmt', None)
            self.insert_frame_id = normalize_frame_id(insert_data.get('frame_id', None))
            # self.insert_lineno = int(insert_data.get('lineno', None))
            self.insert_start_line = int(insert_data.get('start', None))
            self.insert_end_line = int(insert_data.get('end', None))
            self.insert_loop_index = int(insert_data.get('loop_index', None))
            self.insert_lineno_excuted_times = 0


        self.allow_trace_skipped = True

        self.is_last_skip = False
        self.is_last_call_skip = False
        self.loop = 2
        self.observed_loop_index = observed_loop_index
        self.skip_loop_record = {}
        
        self.depth_expanded = not call_graph_mode
        self.depth = depth if not call_graph_mode else 3
        # self.is_in_expanded_status = False
        
        self.call_graph_output_path =  os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'call_graph_data.json')) if call_graph_mode else None
        self.frame_status_path = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state.json'))
        self.bp_frame_name = None
        self.bp_frame_index = None

        self.target_frame_parent_id = None
        self.frame_counter = dict()
        self.frame_to_id = dict()
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
        
        self.frame_line_executed = {}

        self.condition = condition

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
        sys.settrace(self.trace)
        sys.excepthook = self._excepthook

    def stop(self):
        sys.settrace(None)

    def write(self, s, force = False):
        if force:
            self._write(s)
            return
        if self.insert_stmt:
            return
        if not self.call_graph_output_path:
            s = u'{self.prefix}{s}\n'.format(**locals())
            self._write(s)
        else:
            current_dir = os.path.dirname(os.path.abspath(__file__))
            call_graph_output_path = os.path.join(current_dir, self.call_graph_output_path)
            with open(call_graph_output_path, 'w') as f:
                json.dump(self.call_infos, f, indent=4)




    def _is_internal_frame(self, frame):
        return os.path.abspath(frame.f_code.co_filename) == os.path.abspath(__file__)

    def set_thread_info_padding(self, thread_info):
        current_thread_len = len(thread_info)
        self.thread_info_padding = max(self.thread_info_padding,
                                       current_thread_len)
        return thread_info.ljust(self.thread_info_padding)

    def trace(self, frame, event, arg):
        frame_file_name = frame.f_code.co_filename
        if self.observed_file:
            if any(frame_file_name.startswith(p) for p in EXCLUDE_PATHS):
                return None
            curr_method_name = _derive_method_name(frame)
            curr_frame_name = f"{frame_file_name}:{curr_method_name}"
            
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

            if frame not in self.frame_to_id:
                curr_frame_id = f"{curr_frame_name}#{self.frame_counter[curr_frame_name]}"
                self.frame_to_id[frame] = curr_frame_id
            else:
                curr_frame_id = self.frame_to_id[frame]

            if event == 'call' and frame_file_name == self.observed_file:
                if curr_method_name.split('.')[-1] == self.method_name.split('.')[-1]:
                    self.candidate_method_names.add(curr_method_name)
                    self.state_data['candidate_method_names'] = list(self.candidate_method_names)
                    with open(self.frame_status_path, 'w') as f:
                        json.dump(self.state_data, f, indent=4)



            if self.insert_stmt and self.insert_frame_id == curr_frame_id:
                self.allow_trace_skipped = False
                if self.insert_start_line <= frame.f_lineno <= self.insert_end_line and not self.is_executed:
                    self.insert_lineno_excuted_times += 1
                    if self.insert_loop_index is None or self.insert_loop_index == self.insert_lineno_excuted_times:

                        result = exec_in_frame(frame, self.insert_stmt)
                        self.is_executed = True
                        self.write(f'[execute] Injection statements executed Result:\n{result}', force=True)
            else:
                self.allow_trace_skipped = True


            if not self.target_frames:
                if frame_file_name == self.observed_file:
                    if self.method_name == curr_method_name:
                        if self.condition:
                            if not self.pass_condition_filter(frame, self.condition):
                                return None if self.allow_trace_skipped else self.trace
                        elif curr_frame_id != self.target_frame_id:
                            if self.observed_frame_index == -1:
                                self.state_data['last_focus_frame_id'] = curr_frame_id
                                with open(self.frame_status_path, 'w') as f:
                                    json.dump(self.state_data, f, indent=4)
                            return None if self.allow_trace_skipped else self.trace

                        parent_frame = frame.f_back
                        if parent_frame:
                            self.target_frame_parent_id = self.frame_to_id[parent_frame] if parent_frame in self.frame_to_id else None
                        if self.bp_frame_name and self.bp_frame_name in self.frame_counter:
                            self.bp_frame_index = self.frame_counter[self.bp_frame_name]

                        self.state_data['target_frame_parent_id'] = self.target_frame_parent_id
                        self.state_data['bp_frame_id'] = f'{self.bp_frame_name}#{self.bp_frame_index}' if self.bp_frame_name and self.bp_frame_index else None
                        self.state_data['curr_frame_id'] = curr_frame_id

                        self.write(f'\n{self._FOREGROUND_BLUE}Current focus frame: {curr_frame_id}{self._STYLE_RESET_ALL}')
                        self.write(f'{self._FOREGROUND_BLUE}Called by: {self.target_frame_parent_id}{self._STYLE_RESET_ALL}')
                        if self.bp_frame_name:
                            self.write(f'{self._FOREGROUND_BLUE}Breakpoint reached at: {self.bp_frame_name}#{self.bp_frame_index}.{self._STYLE_RESET_ALL}')
                        self.write(f'{self._FOREGROUND_BLUE}Runtime details for the focus frame:{self._STYLE_RESET_ALL}\n')

                        with open(self.frame_status_path, 'w') as f:
                            json.dump(self.state_data, f, indent=4)

                        
                        self.target_frames.add(frame)
                        self.start_times[frame] = datetime_module.datetime.now()
                        thread_global.depth = -1
                    else:
                        return None if self.allow_trace_skipped else self.trace
                else:
                    return None if self.allow_trace_skipped else self.trace
            else:
                is_in_scope = self.is_in_code_scope(frame)
                if frame not in self.target_frames and is_in_scope:
                    if curr_frame_id == self.target_frame_id or (self.condition and self.pass_condition_filter(frame, self.condition)): 
                        self.target_frames.add(frame)

                elif frame in self.target_frames and not is_in_scope:
                    if event == 'return' or event == 'exception':
                        thread_global.depth -= 1
                        # self.target_frames.discard(frame)
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
                                self.call_infos.append({'depth': thread_global.depth + 1,
                                                        'content': ['......Skipping repeated (loop) calling details......'],
                                                        })
                                self.is_last_call_skip = True
                            return None
                    if self.depth_expanded:
                        if i == back_depth - 1:
                            if event != 'call':
                                return None if self.allow_trace_skipped else self.trace
                            else:
                                tmp_indent = ' ' * 4 * (thread_global.depth+1)
                                if self.loop and self.is_skip_loop(frame):
                                    self.record_frame_line_executed(frame)
                                    self.write(f'{tmp_indent}{self._FOREGROUND_PURPLE}{self._STYLE_DIM}Skipping repeated calling details......{self._STYLE_RESET_ALL}')
                                    return None if self.allow_trace_skipped else self.trace
                                self.record_frame_line_executed(frame)
                                self.write(f'{tmp_indent}{self._FOREGROUND_PURPLE}{self._STYLE_DIM}call:\t{curr_frame_id}{self._STYLE_RESET_ALL}')
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




        if self.loop:
            if event != 'return' and event != 'exception' and event != 'call':
                if self.is_skip_loop(frame):
                    self.record_frame_line_executed(frame)
                    if not self.is_last_skip:
 
                        # clear skip_loop_record for this frame
                        self.skip_loop_record[frame] = {} 
                        self.is_last_skip = True

                    if frame.f_lineno not in self.skip_loop_record[frame]:
                        self.skip_loop_record[frame][frame.f_lineno] = 0
                    self.skip_loop_record[frame][frame.f_lineno] += 1                
                    return self.trace
                # else:
            if self.is_last_skip:
                ## count largest skip_loop_times in self.skip_loop_record[frame]
                max_skip_loop_times = max(self.skip_loop_record[frame].values())
                self.write(u'{indent}{_FOREGROUND_YELLOW}{_STYLE_DIM}\n------Skipping {max_skip_loop_times} times of repeated execution details------\n{_STYLE_RESET_ALL}'.format(**locals()))
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
        source_line = source[line_no - 1]
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
        old_local_reprs = self.frame_to_local_reprs.get(frame, {})
        self.frame_to_local_reprs[frame] = local_reprs = \
                                       get_local_reprs(frame,
                                                       watch=self.watch, custom_repr=self.custom_repr,
                                                       max_length=self.max_variable_length,
                                                       normalize=self.normalize,
                                                       )

        newish_string = ('Argument value:\t' if event == 'call' else
                                                            '    New var:\t')
        
        input_para_string = ''
        modify_var_string = ''
        for name, value_repr in local_reprs.items():
            if name not in old_local_reprs:
                input_para_string += f'{name} = {value_repr},    '
                
            elif old_local_reprs[name] != value_repr:
                modify_var_string += f'{name} = {value_repr}, '
        
        if input_para_string:
            input_para_string = input_para_string.rstrip().strip(',')
            if len(input_para_string) > 200:
                input_para_string = input_para_string[:94] + ' ......'
            # self.write('{indent}{_FOREGROUND_GREEN}{_STYLE_DIM}'
            #         '{newish_string}{_STYLE_NORMAL}{input_para_string}{_STYLE_RESET_ALL}'.format(**locals()))
            self.write('{indent}{newish_string}{_STYLE_NORMAL}{input_para_string}{_STYLE_RESET_ALL}'.format(**locals()))
            
        if modify_var_string:
            modify_var_string = modify_var_string.rstrip().strip(',')
            if len(modify_var_string) > 200:
                modify_var_string = modify_var_string[:94] + ' ......'
            self.write('{indent}{_FOREGROUND_GREEN}{_STYLE_DIM}'
                    '    Modified var:\t{_STYLE_NORMAL}{modify_var_string}{_STYLE_RESET_ALL}'.format(**locals()))


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


        if ended_by_exception:
            self.write('{_FOREGROUND_RED}{indent}Call ended by exception{_STYLE_RESET_ALL}'.
                       format(**locals()))
        else:
            self.write(u'{indent}{_STYLE_DIM}{thread_info}{event:9}{line_no:4}\t{source_line}'.format(**locals()))

        if not ended_by_exception:
            return_value_repr = utils.get_shortish_repr(arg,
                                                        custom_repr=self.custom_repr,
                                                        max_length=self.max_variable_length,
                                                        normalize=self.normalize,
                                                        )


        if self.call_graph_output_path:
            if event == 'call':
                # if curr_frame_id not in self.call_frames:
                self.call_frames[curr_frame_id] = []
                result_str_lst = self.call_frames[curr_frame_id]
                
                

                self.call_infos.append({'depth': thread_global.depth,
                                            'content': result_str_lst,
                    })
                
                self.is_last_call_skip = False
                

                result_str_lst.append(f'Call ... {source_line}')
                result_str_lst.append(f'Frame ID: {curr_frame_id}')
                input_para_string = 'Starting var:.. '
                
                for name, value_repr in local_reprs.items():
                    if name not in old_local_reprs:
                        input_para_string += ('{name} = '
                                '{value_repr}, '.format(**locals()))

                input_para_string = input_para_string.rstrip().strip(',')
                if len(input_para_string) > 100:
                    input_para_string = input_para_string[:94] + ' ......'
                
                if input_para_string != 'Starting var:..':
                    result_str_lst.append(input_para_string)
                
                
            if event == 'return':
                if curr_frame_id not in self.call_frames:
                    # print(f'Frame in file {frame.f_code.co_filename}-{frame.f_lineno} not found in call_frames.')
                    return self.trace
                result_str_lst = self.call_frames[curr_frame_id]
                if ended_by_exception:
                    result_str_lst.append('Call ended by exception')
                else:
                    result_str_lst.append(f'Return ... {source_line}')

                if not ended_by_exception:
                    result_str_lst.append(f'Return value:.. {return_value_repr}')                


        if event == 'return':
            if not self.observed_file or frame not in self.target_frames:
                self.frame_to_local_reprs.pop(frame, None)
                self.start_times.pop(frame, None)
            thread_global.depth -= 1

            if not ended_by_exception:
                self.write('{indent}{_FOREGROUND_CYAN}{_STYLE_DIM}'
                           'Return value:\t{_STYLE_NORMAL}{return_value_repr}'
                           '{_STYLE_RESET_ALL}'.
                           format(**locals()))
            
            if self.observed_file:
                if frame in self.target_frames:
                    self.manual_exit(frame)

                if not self.call_graph_output_path:
                    sys.settrace(None)
                    sys.exit()

        if event == 'exception':
            thread_global.depth -= 1
            exception = '\n'.join(traceback.format_exception_only(*arg[:2])).strip()
            if self.max_variable_length:
                exception = utils.truncate(exception, self.max_variable_length)
            self.write('{indent}{_FOREGROUND_RED}Exception:..... '
                       '{_STYLE_BRIGHT}{exception}'
                       '{_STYLE_RESET_ALL}'.format(**locals()))
            if self.observed_file and not self.observed_frame_index and not self.condition:
                if frame in self.target_frames:
                    self.manual_exit(frame)

        return self.trace
    
    def pass_condition_filter(self, frame, condition):
        if condition is None:
            return True
        try:
            args, varargs, varkw, locals_ = inspect.getargvalues(frame)
            context = {}

            for a in args:
                context[a] = locals_[a]
            # *args
            if varargs:
                context[varargs] = locals_[varargs]
            # **kwargs
            if varkw:
                context[varkw] = locals_[varkw]

            frame_globals = frame.f_globals
            result = eval(condition, frame_globals, context)

            return bool(result)

        except Exception as e:
            self.write(f'Condition evaluation failed: {e}')
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
        duration = datetime_module.datetime.now() - start_time
        elapsed_time_string = pycompat.timedelta_format(duration)
        indent = ' ' * 4 * (thread_global.depth)

    
    def is_in_code_scope(self, frame):
        return  _derive_method_name(frame) == self.method_name and os.path.abspath(frame.f_code.co_filename) == self.observed_file


                
    def is_skip_loop(self, frame, max_loop_times = None):
        looped_times = 0
        max_loop_times = max_loop_times if max_loop_times is not None else self.loop
        
        if frame in self.frame_line_executed and frame.f_lineno in self.frame_line_executed[frame]:
            looped_times = self.frame_line_executed[frame][frame.f_lineno]
        else:
            return False

        if self.observed_loop_index:
            if looped_times == self.observed_loop_index - 1:
                return False
            else:
                return True
        if looped_times >= max_loop_times:
            return True

        return False

    def record_frame_line_executed(self, frame):
        if frame not in self.frame_line_executed:
            self.frame_line_executed[frame] = {}
        if frame.f_lineno not in self.frame_line_executed[frame]:
            self.frame_line_executed[frame][frame.f_lineno] = 0
        self.frame_line_executed[frame][frame.f_lineno] += 1

    
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
        with open(self.frame_status_path, 'r') as f_state:
            state_data = json.load(f_state)
            state_data['exception_frame'] = self.frame_to_id.get(f, None)
        with open(self.frame_status_path, 'w') as f_state:
            json.dump(state_data, f_state, indent=4)


        traceback.print_exception(tp, val, tb, file=sys.__stderr__, chain=True)
        sys.__stderr__.flush()

        return 