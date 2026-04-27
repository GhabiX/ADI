# Copyright 2019 Ram Rachum and collaborators.
# This program is distributed under the MIT license.

import abc
import re
from pathlib import Path
import sys
from .pycompat import ABC, string_types, collections_abc
import os

def _check_methods(C, *methods):
    mro = C.__mro__
    for method in methods:
        for B in mro:
            if method in B.__dict__:
                if B.__dict__[method] is None:
                    return NotImplemented
                break
        else:
            return NotImplemented
    return True


class WritableStream(ABC):
    @abc.abstractmethod
    def write(self, s):
        pass

    @classmethod
    def __subclasshook__(cls, C):
        if cls is WritableStream:
            return _check_methods(C, 'write')
        return NotImplemented



file_reading_errors = (
    IOError,
    OSError,
    ValueError # IronPython weirdness.
)



def shitcode(s):
    return ''.join(
        (c if (0 < ord(c) < 256) else '?') for c in s
    )


def get_repr_function(item, custom_repr):
    for condition, action in custom_repr:
        if isinstance(condition, type):
            condition = lambda x, y=condition: isinstance(x, y)
        if condition(item):
            return action
    return repr


DEFAULT_REPR_RE = re.compile(r' at 0x[a-f0-9A-F]{4,}')


def normalize_repr(item_repr):
    """Remove memory address (0x...) from a default python repr"""
    return DEFAULT_REPR_RE.sub('', item_repr)


def get_shortish_repr(item, custom_repr=(), max_length=None, normalize=False):
    repr_function = get_repr_function(item, custom_repr)
    try:
        r = repr_function(item)
    except Exception:
        r = 'REPR FAILED'
    r = r.replace('\r', '').replace('\n', '')
    if normalize:
        r = normalize_repr(r)
    if max_length:
        r = truncate(r, max_length)
    return r


def truncate(string, max_length):
    if (max_length is None) or (len(string) <= max_length):
        return string
    else:
        left = (max_length - 3) // 2
        right = max_length - 3 - left
        return u'{}...{}'.format(string[:left], string[-right:])


def ensure_tuple(x):
    if isinstance(x, collections_abc.Iterable) and \
                                               not isinstance(x, string_types):
        return tuple(x)
    else:
        return (x,)

def normalize_frame_id(frame_id: str):
    if not frame_id:
        return None
    fpath, method_name, index = parse_frame_id(frame_id)
    fpath = os.path.abspath(fpath)
    return f"{fpath}:{method_name}#{index}"

# -------- Frame ID helpers --------
def parse_frame_id(frame_id: str):
    """Parse frame_id string into (file_path, method_name, frame_index)."""
    if "#" not in frame_id:
        raise ValueError(f"Invalid frame_id (missing '#'): {frame_id!r}")
    left, index_str = frame_id.rsplit("#", 1)
    if ":" not in left:
        raise ValueError(f"Invalid frame_id (missing ':'): {frame_id!r}")
    file_path_str, method_name = left.rsplit(":", 1)

    try:
        int(index_str)
    except ValueError:
        raise ValueError(f"Invalid frame_id (index must be integer): {frame_id!r}")
    file_path = Path(file_path_str).resolve()
    return file_path, method_name, int(index_str)


def _derive_method_name(frame):
    """
    Given a frame object, return the method name with the class that actually
    defined it (not just the runtime type of `self`).
    
    This function prioritizes co_qualname for accuracy, as it contains the
    exact definition location compiled into the bytecode.
    """


    func_name = frame.f_code.co_name
    owner = None

    qn = getattr(frame.f_code, "co_qualname", "")

    if "." in qn and not qn.startswith("<"):
        parts = qn.rsplit(".", 1)
        if len(parts) == 2:
            potential_owner = parts[0]
            if potential_owner and (potential_owner[0].isupper() or "." in potential_owner):
                owner = potential_owner


    # Helper to walk an MRO and match by code object
    def _find_owner_in_mro(obj_class):
        try:
            for cls in getattr(obj_class, "__mro__", ()):
                func = cls.__dict__.get(func_name)
                # func might be a function/descriptor (staticmethod/classmethod/function)
                code = getattr(func, "__code__", getattr(getattr(func, "__func__", None), "__code__", None))
                if code is frame.f_code:
                    # Prefer __qualname__ to preserve nesting like Outer.Inner
                    return getattr(cls, "__qualname__", cls.__name__)
        except Exception:
            pass
        return None


    if owner is None:
        self_obj = frame.f_locals.get("self")
        if self_obj is not None:
            try:
                obj_class = type(self_obj)  # avoid __class__ to not trigger lazy setups
                found = _find_owner_in_mro(obj_class)
                if found:
                    owner = found
            except (AttributeError, TypeError, RuntimeError, RecursionError, Exception):
                # Be defensive (e.g., Django lazy objects)
                pass


        
    if owner is None:
        cls_obj = frame.f_locals.get("cls")
        if cls_obj is not None:
            found = _find_owner_in_mro(cls_obj)
            if found:
                owner = found


    return f"{owner}.{func_name}" if owner else func_name



def find_first_non_import_line(lines) -> int:
    try:  
        in_docstring = False
        docstring_delim = None
        in_multiline_import = False
        paren_depth = 0
        
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            original_line = line
            
            if not in_docstring:
                if stripped.startswith('"""') or stripped.startswith("'''"):
                    docstring_delim = stripped[:3]
                    if stripped.count(docstring_delim) == 1:
                        in_docstring = True
                        continue
            else:
                if docstring_delim in stripped:
                    in_docstring = False
                    docstring_delim = None
                continue
            
            if stripped == '' or stripped.startswith('#'):
                continue
                
            if stripped.startswith('#!'):
                continue
            
            if in_multiline_import:

                paren_depth += stripped.count('(') - stripped.count(')')


                if paren_depth <= 0:
                    in_multiline_import = False
                    paren_depth = 0
                continue
            
            if (stripped.startswith('import ') or 
                stripped.startswith('from ') or
                stripped.startswith('__')):  # __future__, __version__ etc
                
                paren_depth = stripped.count('(') - stripped.count(')')
                if paren_depth > 0:
                    in_multiline_import = True
                continue
            

            if stripped:
                return i 
        
        return len(lines) + 1
        
    except Exception as e:
        print(f"Warning: Failed to parse import lines: {e}")
        return 1