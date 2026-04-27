import ast
import json
import sys
from functools import cache
from pathlib import Path


def get_statement_range(file_path, target_lineno):
    with open(file_path, 'r', encoding='utf-8') as f:
        source = f.read()
    tree = ast.parse(source)

    best_node = None
    max_end = -1

    for node in ast.walk(tree):
        if hasattr(node, 'lineno') and hasattr(node, 'end_lineno'):
            if node.lineno == target_lineno and node.lineno <= target_lineno <= node.end_lineno:
                if node.end_lineno > max_end:
                    best_node = node
                    max_end = node.end_lineno

    if best_node:
        return best_node.lineno, best_node.end_lineno
    return None, None




@cache
def method_and_class_ranges_in_file(file: str):
    """
    Find the ranges of all methods and classes in a Python file.

    Result key is the method or class name, value is (start_line, end_line), inclusive.
    """
    class MethodAndClassRangeFinder(ast.NodeVisitor):
        def __init__(self):
            self.range_map = {}
            self.class_stack = []

        def calc_method_id(self, method_name: str) -> str:
            """Calculate the full method or function name, including its class if applicable."""
            full_class_name = ".".join(self.class_stack)
            return full_class_name + '.' + method_name if full_class_name else method_name

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            """Handle class definitions and record their start and end line numbers."""
            self.class_stack.append(node.name)
            full_class_name = ".".join(self.class_stack)
            self.range_map[full_class_name] = {
                'type': 'class',
                'start_line': node.lineno,
                'end_line': node.end_lineno
            }
            super().generic_visit(node)
            self.class_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            """Handle synchronous function definitions."""
            method_id = self.calc_method_id(node.name)
            assert node.end_lineno
            self.range_map[method_id] = {
                'type': 'method',
                'start_line': node.lineno,
                'end_line': node.end_lineno
            }
            self.class_stack.append(node.name)  
            self.generic_visit(node)    
            self.class_stack.pop()


        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            """Handle asynchronous function definitions."""
            method_id = self.calc_method_id(node.name)
            assert node.end_lineno
            self.range_map[method_id] = {
                'type': 'method',
                'start_line': node.lineno,
                'end_line': node.end_lineno
            }
            self.class_stack.append(node.name)
            self.generic_visit(node)
            self.class_stack.pop()

    finder = MethodAndClassRangeFinder()

    if not Path(file).exists():
        # print(f"File {file} does not exist")
        return {}
    source = Path(file).read_text()

    try:
        tree = ast.parse(source, file)
    except SyntaxError:
        # print(f"SyntaxError in {file}")
        return {}

    finder.visit(tree)

    return finder.range_map



def get_belonging_method(file_path: str, lineno: int) -> str:
    """
    Get the method that contains the specified line number in the given file.
    Returns the full method if found, otherwise returns None.
    """
    code_map = method_and_class_ranges_in_file(file_path)
    
    ## find the most inner method
    closest_method_scope = None
    match_method = None
    for name in code_map:
        if code_map[name]['type'] != 'method':
            continue
        start_line = code_map[name]['start_line']
        end_line = code_map[name]['end_line']
        if start_line <= lineno <= end_line:
            if closest_method_scope is None or (start_line >= closest_method_scope['start_line'] and end_line <= closest_method_scope['end_line']):
                closest_method_scope = code_map[name]
                match_method = name
    
    if closest_method_scope:
        return match_method
    else:
        return f'No method found containing line {lineno} in file {file_path}. Please check the file and line number.'

def get_method_range(file_path: str, method_name: str):
    code_map = method_and_class_ranges_in_file(file_path)

    if method_name in code_map:
        return code_map[method_name]['start_line'], code_map[method_name]['end_line']
    else:
        candidate_names = set()
        for name in code_map:
            if method_name.split('.')[-1] == name.split('.')[-1]:
                candidate_names.add(name)
        
        if len(candidate_names) == 0:
            err_msg = (f"Method '{method_name}' not found in file '{file_path}'. Partial match also does not exist.")
        else:
            err_msg = (
                f"Method '{method_name}' not found in the file '{file_path}'\n"
                f"Partial matches method: {', '.join(candidate_names)}\n"
                "Please check the method name and try again."
            )
        return err_msg

    


# CLI entry point: structured input/output
if __name__ == "__main__":
    function_name = sys.argv[1]
    file_path = sys.argv[2]
    param = sys.argv[3]
    if function_name == "get_statement_range":
        start_line, end_line = get_statement_range(file_path, int(param))
        json.dump({"start_line": start_line, "end_line": end_line}, sys.stdout)
    elif function_name == "get_belonging_method":
        result = get_belonging_method(file_path, int(param))
    elif function_name == "get_method_range":
        result = get_method_range(file_path, param)
        
        if isinstance(result, tuple) and len(result) == 2:
            start_line, end_line = result
            json.dump({"start_line": start_line, "end_line": end_line}, sys.stdout)
        else:
            json.dump({"error": result}, sys.stdout)
