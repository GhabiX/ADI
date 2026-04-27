import ast
import os
from typing import Optional, Tuple


def find_function_at_line(file_path: str, line_num: int) -> Optional[str]:
    """Find the function containing a specific line number.

    Returns the function name (e.g., 'func' or 'ClassName.method') or None if
    the line is not inside any function.
    """
    file_path = os.path.abspath(file_path)
    if not os.path.exists(file_path):
        return None

    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return None

    if line_num < 1 or line_num > len(lines):
        return None

    try:
        source = "".join(lines)
        tree = ast.parse(source, filename=file_path)
    except SyntaxError:
        return None

    functions = []

    def _node_end_lineno(node: ast.AST) -> Optional[int]:
        end = getattr(node, "end_lineno", None)
        if isinstance(end, int) and end >= 1:
            return end
        lineno = getattr(node, "lineno", None)
        if isinstance(lineno, int) and lineno >= 1:
            return lineno
        return None

    def _fallback_end_lineno(node: ast.AST) -> int:
        # Python 3.6 AST nodes don't carry end_lineno; approximate the function
        # extent using the maximum lineno observed among its descendants.
        max_lineno = _node_end_lineno(node) or 0
        for child in ast.walk(node):
            end = _node_end_lineno(child)
            if end and end > max_lineno:
                max_lineno = end
        if max_lineno <= 0:
            max_lineno = getattr(node, "lineno", 0) or 0
        if max_lineno > len(lines):
            max_lineno = len(lines)
        return max_lineno

    class FunctionVisitor(ast.NodeVisitor):
        def __init__(self):
            self.class_stack = []
            self.function_stack = []

        def visit_ClassDef(self, node):
            self.class_stack.append(node.name)
            self.generic_visit(node)
            self.class_stack.pop()

        def visit_FunctionDef(self, node):
            # If we're inside another function, this is a nested local function.
            # For ADI frame IDs we intentionally avoid prefixing local functions
            # with the surrounding class name, because tracer's runtime naming
            # for <locals> frames is just the simple function name.
            is_nested_function = bool(self.function_stack)
            class_name = (
                ".".join(self.class_stack) if self.class_stack and not is_nested_function else None
            )
            end_line = getattr(node, "end_lineno", None)
            if not isinstance(end_line, int) or end_line < node.lineno:
                end_line = _fallback_end_lineno(node)
            functions.append((node.lineno, end_line, node.name, class_name))
            self.function_stack.append(node.name)
            self.generic_visit(node)
            self.function_stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

    visitor = FunctionVisitor()
    visitor.visit(tree)

    containing_func = None
    for start, end, name, class_name in functions:
        if start <= line_num <= end:
            if containing_func is None or (end - start) < (containing_func[1] - containing_func[0]):
                containing_func = (start, end, name, class_name)

    if containing_func:
        _, _, name, class_name = containing_func
        return f"{class_name}.{name}" if class_name else name
    return None


def resolve_frame_id(frame_id: str) -> Tuple[str, bool, Optional[str]]:
    """Resolve frame_id, supporting both formats:
    1. file:func#N (original format, returned as-is)
    2. file:line or file:line#N (converted to file:func#N)

    Returns (resolved_frame_id, resolved_from_line_number, error_msg).
    """
    if ":" not in frame_id:
        return frame_id, False, None

    file_part, rest = frame_id.rsplit(":", 1)

    # file:line
    if rest.isdigit():
        line_num = int(rest)
        func_name = find_function_at_line(file_part, line_num)
        if not func_name:
            return frame_id, True, f"Line {line_num} is not inside any function in {file_part}"
        return f"{file_part}:{func_name}#1", True, None

    # file:line#N
    if "#" in rest:
        left, call_index = rest.split("#", 1)
        if left.isdigit():
            line_num = int(left)
            func_name = find_function_at_line(file_part, line_num)
            if not func_name:
                return frame_id, True, f"Line {line_num} is not inside any function in {file_part}"
            return f"{file_part}:{func_name}#{call_index}", True, None

    # If no #N suffix, default to #1 (CLI/server expect # for dbgtool.parse_frame_id).
    if "#" not in rest:
        return f"{frame_id}#1", False, None

    return frame_id, False, None
