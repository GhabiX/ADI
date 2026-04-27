# Copyright 2019 Ram Rachum and collaborators.
# This program is distributed under the MIT license.

import abc
import re
from pathlib import Path
import sys
from .pycompat import ABC, string_types, collections_abc
import os
from functools import lru_cache

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
    """Select a repr function for `item` based on `custom_repr` rules.

    `custom_repr` is an iterable of (condition, action) where:
      - condition can be a type or a predicate callable
      - action is a callable that returns a textual representation

    Any errors in user-provided predicates are treated as non-matches to avoid
    breaking tracing output.
    """
    for condition, action in custom_repr:
        try:
            if isinstance(condition, type):
                if isinstance(item, condition):
                    return action
            else:
                if condition(item):
                    return action
        except Exception:
            # Defensive: ignore bad predicates.
            continue
    return repr


DEFAULT_REPR_RE = re.compile(r' at 0x[a-f0-9A-F]{4,}')


def normalize_repr(item_repr):
    """Remove memory address (0x...) from a default python repr"""
    return DEFAULT_REPR_RE.sub('', item_repr)


def _safe_to_text(value):
    """Convert `value` to a safe, printable text string.

    This is intentionally defensive because ADI may inspect arbitrary objects
    and user-provided custom repr hooks.

    Policy:
    - Always return `str`.
    - Avoid raising during conversion.
    - Sanitize for the active stdout encoding to prevent UnicodeEncodeError in
      ASCII-only environments.
    """
    try:
        if isinstance(value, str):
            s = value
        elif isinstance(value, bytes):
            s = value.decode('utf-8', errors='replace')
        else:
            s = str(value)
    except Exception:
        # Last-resort fallback; avoid calling repr(value) since it may recurse.
        s = '<unprintable>'

    # Ensure the result is encodable for the current output stream.
    encoding = getattr(sys.stdout, 'encoding', None) or 'utf-8'
    try:
        s.encode(encoding)
    except Exception:
        s = s.encode(encoding, errors='backslashreplace').decode(encoding, errors='replace')

    return s


def get_shortish_repr(item, custom_repr=(), max_length=None, normalize=False):
    """Return a safe, possibly-truncated representation of an object.

    This function is used heavily by the tracer. It must be:
      - exception-safe (never propagate repr-related errors)
      - encoding-safe (avoid UnicodeEncodeError on ASCII-only streams)
      - stable (keep the 'REPR FAILED' marker for downstream detection)
    """
    repr_function = get_repr_function(item, custom_repr)
    try:
        r = repr_function(item)
    except Exception as e:
        # Keep the historical marker for downstream detection, but also add a
        # stable identity hint so users can still reason about objects (esp.
        # `self` during __init__ where custom __repr__ often breaks).
        #
        # Do NOT call repr(item) again here. Stick to safe primitives only.
        try:
            t = type(item)
            t_name = getattr(t, "__name__", None) or "unknown"
            t_module = getattr(t, "__module__", None) or ""
            type_name = f"{t_module}.{t_name}" if t_module and t_module not in ("builtins", "__main__") else t_name
        except Exception:
            type_name = "unknown"

        identity = type_name
        if not normalize:
            try:
                identity = f"{type_name} id={hex(id(item))}"
            except Exception:
                identity = type_name

        # Optionally include exception details when debugging ADI itself.
        if os.environ.get('ADI_REPR_ERROR_DETAILS', '').lower() in ('1', 'true', 'yes'):
            r = f"REPR FAILED ({type(e).__name__}: {e}; {identity})"
        else:
            r = f"REPR FAILED ({identity})"

    r = _safe_to_text(r)
    r = r.replace('\r', '').replace('\n', '')
    if normalize:
        r = normalize_repr(r)
    if max_length:
        r = truncate(r, max_length)
    return r


def truncate(string, max_length):
    if (max_length is None) or (len(string) <= max_length):
        return string

    suffix = '[TRUNCATED...]'
    if max_length <= 0:
        return suffix
    if max_length <= len(suffix):
        return suffix[:max_length]
    left = max_length - len(suffix)
    return f'{string[:left]}{suffix}'


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
@lru_cache(maxsize=1024)
def parse_frame_id(frame_id: str):
    """Parse frame_id string into (file_path, method_name_or_lineno, frame_index).

    Supports two formats:
    - Line number format: /path/file.py:1234#1 -> returns (file_path, 1234, 1) where 1234 is int
    - Method name format: /path/file.py:func#1 -> returns (file_path, "func", 1) where func is str
    """
    if "#" not in frame_id:
        raise ValueError(f"Invalid frame_id (missing '#'): {frame_id!r}")
    left, index_str = frame_id.rsplit("#", 1)
    if ":" not in left:
        raise ValueError(f"Invalid frame_id (missing ':'): {frame_id!r}")
    file_path_str, method_or_lineno = left.rsplit(":", 1)

    try:
        int(index_str)
    except ValueError:
        raise ValueError(f"Invalid frame_id (index must be integer): {frame_id!r}")
    file_path = Path(file_path_str).absolute()

    # Check if method_or_lineno is a line number (all digits)
    if method_or_lineno.isdigit():
        return file_path, int(method_or_lineno), int(index_str)
    else:
        return file_path, method_or_lineno, int(index_str)


def _derive_method_name(frame):
    """Derive a stable human-readable name for the current frame.

    Performance note
    - This function is called from the global tracer and can be executed hundreds
      of thousands of times in a single run.
    - Prefer the code object's `co_qualname` (O(1)) whenever it is trustworthy.
    - Only fall back to an MRO scan (reflection) when we truly need to.

    Naming policy
    - If `co_qualname` is available and does *not* contain '<locals>', we return
      it directly. This covers normal methods including "_PrivateClass.method"
      and avoids the expensive MRO walk.
    - If the qualname contains '<locals>', we return the suffix after the last
      '<locals>.' segment. This keeps nested local functions as simple names
      (e.g. 'inner') while preserving the class prefix for local classes
      (e.g. 'CustomCoord.prop').
    """

    code = frame.f_code
    func_name = code.co_name

    # Fast path (Python 3.11+): co_qualname already encodes the defining owner.
    qn = getattr(code, "co_qualname", "")
    if qn and not qn.startswith("<"):
        if "<locals>" in qn:
            # e.g. "outer.<locals>.inner" -> "inner"
            # e.g. "make.<locals>.CustomCoord.prop" -> "CustomCoord.prop"
            if "<locals>." in qn:
                return qn.split("<locals>.")[-1]
            return func_name
        return qn

    owner = None
    def _sanitize_owner(name: str) -> str:
        if "<locals>." in name:
            return name.split("<locals>.")[-1]
        return name

    def _find_owner_in_mro(obj_class):
        """Walk MRO to find which class actually defines this code object."""
        try:
            for cls in getattr(obj_class, "__mro__", ()):
                func = cls.__dict__.get(func_name)
                # func might be a function/descriptor (staticmethod/classmethod/function)
                code_obj = getattr(func, "__code__", getattr(getattr(func, "__func__", None), "__code__", None))
                if code_obj is code:
                    return _sanitize_owner(getattr(cls, "__qualname__", cls.__name__))

                # property: inspect accessor functions.
                if isinstance(func, property):
                    for accessor in (func.fget, func.fset, func.fdel):
                        if accessor is None:
                            continue
                        accessor_code = getattr(accessor, "__code__", None)
                        if accessor_code is code:
                            return _sanitize_owner(getattr(cls, "__qualname__", cls.__name__))
                        wrapped = getattr(accessor, "__wrapped__", None)
                        wrapped_code = getattr(wrapped, "__code__", None) if wrapped else None
                        if wrapped_code is code:
                            return _sanitize_owner(getattr(cls, "__qualname__", cls.__name__))

                # Decorated functions: best-effort check for __wrapped__.
                wrapped = getattr(func, "__wrapped__", None)
                wrapped_code = getattr(wrapped, "__code__", None) if wrapped else None
                if wrapped_code is code:
                    return _sanitize_owner(getattr(cls, "__qualname__", cls.__name__))
        except Exception:
            pass
        return None

    # Bound method: try to resolve defining class via MRO.
    self_obj = frame.f_locals.get("self")
    if self_obj is not None:
        try:
            found = _find_owner_in_mro(type(self_obj))
            if found:
                owner = found
        except Exception:
            pass

    # Classmethod: `cls` might be present.
    if owner is None:
        cls_obj = frame.f_locals.get("cls")
        if cls_obj is not None:
            found = _find_owner_in_mro(cls_obj)
            if found:
                owner = found

    return f"{owner}.{func_name}" if owner else func_name

