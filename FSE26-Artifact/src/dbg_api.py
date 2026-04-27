import subprocess
import os
import re
import ast
import io
import tokenize
import json
import textwrap
import html

# Try to import pdb_session with proper error handling
try:
    from .pdb_session import api_pdb_interactive
except ImportError:
    try:
        from pdb_session import api_pdb_interactive
    except ImportError:
        # Fallback if module is not available
        def api_pdb_interactive(params):
            return "EXECUTION RESULT of [pdb_interactive]:\nError: PDB session module not available."

def search_def(def_name: str, file_path: str) -> str:
    """
    Searches for function, class, or variable definitions in a Python file using hierarchical naming.
    
    Supports:
    - Top-level definitions: 'func_name', 'ClassName', 'var_name'
    - Class members: 'ClassName.method_name', 'ClassName.attr_name'
    - Nested definitions: 'outer_func.inner_func'
    - Special methods: 'ClassName.__init__'
    
    Args:
        def_name: The name to find (e.g., 'func' or 'MyClass.method')
        file_path: The absolute path to the Python file to search in.
    
    Returns:
        A string containing the definition with line numbers, or an error message.
    """
    result_prefix = f"EXECUTION RESULT of [search_def(def='{def_name}', file_path='{file_path}')]:\n"
    
    try:
        source_code = _read_file_content(file_path)
        lines = source_code.splitlines()
        tree = ast.parse(source_code, filename=file_path)
        
        # Parse the definition name to extract path components
        name_parts = def_name.split('.')
        target_name = name_parts[-1]  # The actual definition name
        path_context = name_parts[:-1]  # The containing context (e.g., ['ClassName'])
        
        found_nodes = []
        
        def collect_definitions(node, current_path=[]):
            """Recursively collect all definitions with their paths."""
            # Check if current node matches our target
            if _is_definition_node(node):
                node_name = _get_node_name(node)
                if node_name == target_name:
                    # Check if the path context matches
                    if not path_context or current_path == path_context:
                        found_nodes.append((node, current_path + [node_name]))
            
            # Recursively search in child nodes with proper path tracking
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                node_name = node.name
                new_path = current_path + [node_name]
                # Search within the body of functions and classes
                for child in node.body:
                    collect_definitions(child, new_path)
            else:
                # For other nodes, continue searching without changing path
                for child in ast.iter_child_nodes(node):
                    collect_definitions(child, current_path)
        
        # Start collection from the root
        collect_definitions(tree)
        
        if not found_nodes:
            if path_context:
                return f"{result_prefix}Error: Definition '{def_name}' not found in {file_path}."
            else:
                return f"{result_prefix}Error: Definition '{target_name}' not found in {file_path}."
        
        # If multiple matches found and no specific path given, show all options
        if len(found_nodes) > 1 and not path_context:
            options = ['.'.join(path) for _, path in found_nodes]
            return f"{result_prefix}Error: Multiple definitions found for '{target_name}'. Please specify: {', '.join(options)}"
        
        # Use the first (or only) match
        found_node, full_path = found_nodes[0]
        
        # Extract the code block
        start_lineno = found_node.lineno
        end_lineno = getattr(found_node, 'end_lineno', start_lineno)
        
        code_block_lines = lines[start_lineno - 1 : end_lineno]
        
        # Apply 300-line limit
        truncated = False
        if len(code_block_lines) > 300:
            code_block_lines = code_block_lines[:300]
            truncated = True
        
        # Format with line numbers
        formatted_lines = [f"{start_lineno + i}: {line}" for i, line in enumerate(code_block_lines)]
        result_content = "\n".join(formatted_lines)
        
        if truncated:
            result_content += "\n[stderr] Output too long, only the first 300 lines are kept."
        
        return f"{result_prefix}{result_content}"
        
    except FileNotFoundError:
        return f"{result_prefix}Error: File not found: {file_path}"
    except SyntaxError as e:
        return f"{result_prefix}Error: Could not parse file {file_path} due to a syntax error on line {e.lineno}."
    except Exception as e:
        return f"{result_prefix}An unexpected error occurred: {str(e)}"


def _is_definition_node(node) -> bool:
    """Check if a node represents a definition we can search for."""
    return isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Assign))


def _get_node_name(node) -> str:
    """Extract the name from a definition node."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name
    elif isinstance(node, ast.Assign):
        # For assignments, try to get the first target name
        for target in node.targets:
            if isinstance(target, ast.Name):
                return target.id
    return None

def parse_api_call(call_string: str) -> dict:
    """
    Parses XML format API call string into a function name and parameters dictionary using regex.
    This approach is more robust for handling complex content with special characters.

    The XML format:
    <function>function_name</function>
    <param1>value1</param1>
    <param2>value2</param2>
    ...

    Args:
        call_string: The raw string representing the API call in XML format.

    Returns:
        A dictionary with "function_name" and "parameters" keys.

    Raises:
        ValueError: If the call_string is malformed or required parameters are missing.
    """
    # Clean up the call string
    call_string = call_string.strip()
    
    if not call_string:
        raise ValueError("API call string cannot be empty.")
    
    try:
        # Extract function name using regex
        function_pattern = r'<function>(.*?)</function>'
        function_match = re.search(function_pattern, call_string, re.DOTALL)
        
        if not function_match:
            raise ValueError("Missing <function> element in API call.")
        
        function_name = function_match.group(1).strip()
        if not function_name:
            raise ValueError("Function name cannot be empty.")
        
        # Extract all parameter tags using regex
        # This pattern captures tag name and content, handling multiline content
        param_pattern = r'<(\w+)>(.*?)</\1>'
        param_matches = re.findall(param_pattern, call_string, re.DOTALL)
        
        parameters = {}
        for tag_name, content in param_matches:
            if tag_name != 'function':  # Skip the function tag
                # Clean and unescape the content
                param_value = content.strip() if content else ""
                
                # Unescape HTML entities that might have been auto-escaped
                param_value = html.unescape(param_value)
                
                # Handle parameter name mapping for backward compatibility
                mapped_name = tag_name
                if tag_name == "content":
                    mapped_name = "new_str"  # For new_file API
                elif tag_name == "definition":
                    mapped_name = "def"  # For search_def API

                parameters[mapped_name] = param_value
        
        return {"function_name": function_name, "parameters": parameters}
    
    except Exception as e:
        # If regex parsing fails, provide detailed error information
        if "Missing <function> element" in str(e) or "Function name cannot be empty" in str(e):
            raise ValueError(str(e))
    else:
            raise ValueError(f"Error parsing API call with regex: {str(e)}")


def _ensure_dir_exists(file_path: str):
    """
    Ensures the directory for the given file_path exists, creating it if necessary.
    """
    dir_name = os.path.dirname(file_path)
    if dir_name: # Only attempt to create if there's a directory part
        os.makedirs(dir_name, exist_ok=True)

def _read_file_content(file_path: str) -> str:
    """
    Reads file content.
    Raises FileNotFoundError if file does not exist.
    Raises IsADirectoryError if path is a directory.
    Raises IOError for other read errors.
    """
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"File not found: {file_path}")
    if not os.path.isfile(file_path):
        raise IsADirectoryError(f"Path is a directory, not a file: {file_path}")
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return f.read()
    except Exception as e:
        raise IOError(f"Error reading file {file_path}: {str(e)}")

def _write_file_content(file_path: str, content: str):
    """
    Writes content to a file, overwriting if it exists, creating if it doesn't.
    Raises IOError for write errors.
    """
    try:
        _ensure_dir_exists(file_path)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
    except Exception as e:
        raise IOError(f"Error writing to file {file_path}: {str(e)}")

def _get_context_lines(file_path: str, target_str: str, context_lines=10) -> str:
    """
    Returns the target string with context lines before and after in a file.
    
    Args:
        file_path: Path to the file
        target_str: The string to find in the file
        context_lines: Number of lines of context to include before and after
    
    Returns:
        String containing the target with context, or empty string if not found
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            file_lines = f.readlines()
        
        content = ''.join(file_lines)
        if target_str not in content:
            return ""
            
        # Find the line numbers for the target string
        start_pos = content.find(target_str)
        if start_pos == -1:
            return ""
            
        # Count lines to determine start_line
        start_line = content[:start_pos].count('\n') + 1
        end_line = start_line + target_str.count('\n')
        
        # Calculate context boundaries
        context_start = max(1, start_line - context_lines)
        context_end = min(len(file_lines), end_line + context_lines)
        
        # Extract the lines with context
        context_content = ''.join(file_lines[context_start-1:context_end])
        
        return f"Code context (lines {context_start}-{context_end}):\n{context_content}"
    except Exception as e:
        return f"Error getting context: {str(e)}"

def get_file_diff(file_path):
    """
    Get git diff for a file with robust handling of different scenarios.
    
    Args:
        file_path: Path to the file to get diff for
        
    Returns:
        String containing the git diff output or error message
    """
    if not os.path.exists(file_path):
        return "File does not exist"
    
    try:
        # Get absolute path and directory of the file
        abs_file_path = os.path.abspath(file_path)
        file_dir = os.path.dirname(abs_file_path)
        
        # Find git repository root directory
        git_root = file_dir
        while git_root != "/" and not os.path.exists(os.path.join(git_root, ".git")):
            git_root = os.path.dirname(git_root)
        
        if git_root == "/" or not os.path.exists(os.path.join(git_root, ".git")):
            # Not in a git repository, use git diff --no-index
            diff_result = subprocess.run(
                f"git diff --no-index /dev/null {abs_file_path}",
                shell=True, capture_output=True, text=True, check=False
            )
            if diff_result.stdout:
                return diff_result.stdout
            else:
                # If git diff --no-index also fails, read file content directly
                try:
                    with open(abs_file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    return f"New file (no git repository):\n{content}"
                except Exception:
                    return "New file but cannot read content"
        
        # In git repository, get relative path
        rel_path = os.path.relpath(abs_file_path, git_root)
        
        # Execute git status in git repository root directory
        status_result = subprocess.run(
            f"cd {git_root} && git status --porcelain -- {rel_path}",
            shell=True, capture_output=True, text=True, check=False
        )
        
        if status_result.returncode != 0:
            return f"Error running git status: {status_result.stderr}"
        
        status = status_result.stdout.strip()
        if not status:
            # File is tracked but has no changes
            return "No changes detected by git (file is tracked but unchanged)"
        elif status.startswith("??"):
            # New file (untracked)
            diff_result = subprocess.run(
                f"cd {git_root} && git diff --no-index /dev/null {rel_path}",
                shell=True, capture_output=True, text=True, check=False
            )
            if diff_result.stdout:
                return diff_result.stdout
            else:
                # Alternative: read file content directly
                try:
                    with open(abs_file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                    return f"New file created:\n{content}"
                except Exception:
                    return "New file but cannot read content"
        else:
            # Tracked file with changes, use regular git diff
            diff_result = subprocess.run(
                f"cd {git_root} && git diff -- {rel_path}",
                shell=True, capture_output=True, text=True, check=False
            )
            if diff_result.stdout:
                return diff_result.stdout
            else:
                return "No changes detected by git"
                
    except Exception as e:
        return f"Error obtaining diff: {str(e)}"

def _format_multiple_matches(file_path: str, matches: list, max_matches=3, context_lines=2) -> str:
    """
    Unified function to format multiple match errors with context.
    All matches use the same format: {'start_line': int, 'end_line': int, 'content': str}
    
    Args:
        file_path: Path to the file
        matches: List of match dictionaries with 'start_line', 'end_line', 'content'
        max_matches: Maximum matches to show details for
        context_lines: Context lines around each match
    
    Returns:
        Formatted error message with match locations and context
    """
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            file_lines = f.readlines()
        
        total_matches = len(matches)
        
        # Format result header
        result = f"Error: 'old_str' found {total_matches} times in file {file_path}. "
        if total_matches > max_matches:
            result += f"First {max_matches} matches:\n\n"
        else:
            result += f"All matches:\n\n"
        
        # Show each match with context
        for i, match in enumerate(matches[:max_matches]):
            start_line = match['start_line'] + 1  # Convert to 1-based for display
            end_line = match['end_line']  # This is 0-based exclusive, convert to 1-based inclusive
            
            # Get location hint
            location_hint = _get_location_hint(file_lines, start_line)
            
            # Calculate context boundaries  
            context_start = max(1, start_line - context_lines)
            context_end = min(len(file_lines), end_line + context_lines)
            
            # Format match header
            result += f"{i+1}. Line {start_line}"
            if end_line > start_line:
                result += f"-{end_line}"  # end_line is already 1-based inclusive now
            result += f": {location_hint}\n"
            result += f"   Context (lines {context_start}-{context_end}):\n"
            
            # Show context with match highlighting
            for line_num in range(context_start, context_end + 1):
                if line_num <= len(file_lines):
                    line_content = file_lines[line_num - 1].rstrip('\n')
                    
                    # Mark matching lines with >>> (using 1-based comparison)
                    if start_line <= line_num <= end_line:
                        prefix = ">>>"
                    else:
                        prefix = "   "
                    
                    result += f"   {prefix} {line_num:3d}: {line_content}\n"
            
            result += "\n"
        
        # Add additional matches info
        if total_matches > max_matches:
            result += f"... and {total_matches - max_matches} more matches.\n\n"
        
        # Add helpful message
        result += "To proceed, expand 'old_str' to include more unique context from one of the above locations."
        
        return result
        
    except Exception as e:
        return f"Error analyzing matches: {str(e)}"


def _get_location_hint(file_lines: list, target_line: int) -> str:
    """
    Try to determine what function or class a line is inside.
    
    Args:
        file_lines: List of file lines
        target_line: 1-based line number to analyze
    
    Returns:
        A hint about the location (e.g., "inside function foo()" or "at module level")
    """
    try:
        # Look backwards from target line to find function/class definitions
        for i in range(target_line - 1, -1, -1):
            line = file_lines[i].strip()
            
            # Check for function definition
            if line.startswith('def ') and ':' in line:
                func_name = line.split('(')[0].replace('def ', '').strip()
                return f"inside function {func_name}()"
            
            # Check for async function definition
            if line.startswith('async def ') and ':' in line:
                func_name = line.split('(')[0].replace('async def ', '').strip()
                return f"inside async function {func_name}()"
            
            # Check for class definition
            if line.startswith('class ') and ':' in line:
                class_name = line.split('(')[0].replace('class ', '').strip().rstrip(':')
                return f"inside class {class_name}"
        
        return "at module level"
    
    except Exception:
        return "unknown location"

# --- API Implementations ---

def api_execute_bash(command: str) -> str:
    """
    Executes a bash command and returns its output.

    Args:
        command: The bash command to execute.

    Returns:
        A string detailing the execution result, including stdout, stderr, and exit code.
    """
    try:
        current_working_dir = os.getcwd()
        try:
            process = subprocess.run(
                command,
                shell=True,  # Be cautious with shell=True if commands come from untrusted sources
                cwd=current_working_dir,
                capture_output=True,
                text=True,
                check=False,  # Do not raise an exception for non-zero exit codes
                timeout=600  # Add timeout (seconds)
            )
            timeout_occurred = False
        except subprocess.TimeoutExpired as e:
            # Handle timeout: collect available output
            process = e
            timeout_occurred = True

        result_str = f"EXECUTION RESULT of [execute_bash]:\n"
        stdout = getattr(process, "stdout", None)
        stderr = getattr(process, "stderr", None)
        
        # Fix: Ensure stdout and stderr are strings, not bytes
        if isinstance(stdout, bytes):
            stdout = stdout.decode('utf-8', errors='replace')
        if isinstance(stderr, bytes):
            stderr = stderr.decode('utf-8', errors='replace')
        
        # Collect stdout and stderr lines
        stdout_lines = stdout.splitlines(keepends=True) if stdout else []
        stderr_lines = stderr.splitlines(keepends=True) if stderr else []
        # Combine both for total line count
        combined_lines = stdout_lines + ([f"stderr:\n"] if stderr_lines else []) + stderr_lines
        # Truncate if total lines exceed 300
        truncated = False
        if len(combined_lines) > 300:
            combined_lines = combined_lines[:300]
            truncated = True
        # Reconstruct result_str
        result_str += ''.join(combined_lines)
        if not result_str.endswith('\n'):
            result_str += '\n'
        # Add truncation notice if needed
        if truncated:
            result_str += "[stderr] Output too long, only the first 300 lines are kept.\n"
        if timeout_occurred:
            result_str += "[The process was killed due to timeout (600 seconds).]\n"
            result_str += f"[Current working directory: {current_working_dir}]\n"
            result_str += "[Command finished with exit code: timeout]"
        else:
            result_str += f"[Current working directory: {current_working_dir}]\n"
            result_str += f"[Command finished with exit code {process.returncode}]"
        return result_str

    except Exception as e:
        return (
            f"EXECUTION RESULT of [execute_bash]:\n"
            f"Python Exception during command execution: {str(e)}\n"
            f"[The command failed to execute due to an internal error.]"
        )


def api_str_replace(file_path: str, old_str: str, new_str: str) -> str:
    """
    Replace string segments with smart indentation matching using textwrap.dedent.
    First tries exact match, then relative indentation matching.
    Uses unified match processing for both paths.
    """
    result_prefix = "EXECUTION RESULT of [str_replace]:\n"
    try:
        content = _read_file_content(file_path)
        
        # Step 1: Try exact string match
        if old_str in content:
            exact_matches = _find_exact_matches(content, old_str)
            if len(exact_matches) > 1:
                multiple_matches_info = _format_multiple_matches(file_path, exact_matches)
                return f"{result_prefix}{multiple_matches_info}"
            
            # Single exact match - safe to replace
            new_content = content.replace(old_str, new_str, 1)
            _write_file_content(file_path, new_content)
            after_context = _get_context_lines(file_path, new_str)
            return f"{result_prefix}Successfully replaced string in file: {file_path}\n\nModified code with context:\n{after_context}"
        
        # Step 2: Try relative indentation matching using textwrap.dedent
        old_normalized = textwrap.dedent(old_str).strip()
        if not old_normalized:
            return f"{result_prefix}Error: No valid content found in old_str."
        
        # Find all matching positions using sliding window
        dedent_matches = _find_dedent_matches(content, old_normalized)
        
        if len(dedent_matches) == 0:
            return f"{result_prefix}Error: 'old_str' not found in file {file_path}. \n \
                Hint: To avoid `not found` errors, be as specific as possible with `old_str`. If a simple string fails, use surrounding lines of code to make the `old_str` block unique. If it still fails, use `execute_bash` API with `sed -n '...'` or `grep -C 5 '...'` to get the exact text before trying again."
        elif len(dedent_matches) > 1:
            multiple_matches_info = _format_multiple_matches(file_path, dedent_matches)
            return f"{result_prefix}{multiple_matches_info}"
        
        # Single match found - perform replacement
        match_info = dedent_matches[0]
        original_block = match_info['content']
        base_indent = _get_base_indent(original_block)
        
        # Apply same indentation to new_str
        new_normalized = textwrap.dedent(new_str).strip()
        new_indented = _apply_indent(new_normalized, base_indent)
        
        # Perform replacement
        new_content = content.replace(original_block, new_indented, 1)
        _write_file_content(file_path, new_content)
        
        # Get context after making changes
        after_context = _get_context_lines(file_path, new_indented)
        return f"{result_prefix}Successfully replaced string in file: {file_path}\n\nModified code with context:\n{after_context}"
        
    except (FileNotFoundError, IsADirectoryError, IOError) as e:
        return f"{result_prefix}Error processing file {file_path}: {str(e)}"
    except Exception as e:
        return f"{result_prefix}An unexpected error occurred: {str(e)}"

def _find_exact_matches(content: str, target_str: str) -> list:
    """
    Find all exact string matches and return unified match format.
    
    Returns:
        List of {'start_line': int, 'end_line': int, 'content': str}
    """
    matches = []
    lines = content.splitlines()
    start = 0
    
    while True:
        pos = content.find(target_str, start)
        if pos == -1:
            break
        
        # Calculate line numbers (0-based)
        start_line = content[:pos].count('\n')
        end_line = start_line + target_str.count('\n') + 1  # +1 for exclusive end
        
        # Extract original content
        match_content = '\n'.join(lines[start_line:end_line-1])
        
        matches.append({
            'start_line': start_line,
            'end_line': end_line,
            'content': match_content
        })
        
        start = pos + len(target_str)
    
    return matches

def _find_dedent_matches(content: str, normalized_pattern: str) -> list:
    """
    Find all positions where dedented pattern matches using sliding window.
    Returns unified match format.
    
    Returns:
        List of {'start_line': int, 'end_line': int, 'content': str}
    """
    matches = []
    lines = content.splitlines()
    pattern_lines = normalized_pattern.splitlines()
    
    if not pattern_lines:
        return matches
    
    # Sliding window search
    for i in range(len(lines) - len(pattern_lines) + 1):
        window = lines[i:i + len(pattern_lines)]
        window_dedented = textwrap.dedent('\n'.join(window)).strip()
        
        if window_dedented == normalized_pattern:
            matches.append({
                'start_line': i,
                'end_line': i + len(pattern_lines),
                'content': '\n'.join(window)
            })
    
    return matches

def _get_base_indent(text: str) -> str:
    """Get base indentation from first non-empty line."""
    lines = text.splitlines()
    for line in lines:
        if line.strip():
            return line[:len(line) - len(line.lstrip())]
    return ""

def _apply_indent(text: str, base_indent: str) -> str:
    """Apply base indentation to text."""
    if not text:
        return text
        
    lines = text.splitlines()
    result_lines = []
    
    for line in lines:
        if line.strip():  # Non-empty line
            result_lines.append(base_indent + line)
        else:  # Empty line
            result_lines.append("")
    
    return '\n'.join(result_lines)

def api_new_file(file_path: str, new_str: str) -> str:
    """
    Creates a new file with specified content or overwrites an existing file.
    """
    result_prefix = "EXECUTION RESULT of [new_file]:\n"
    try:
        # Determine if it's a create or overwrite for the message
        action = "created"
        if os.path.exists(file_path):
            action = "overwritten"
            
        _write_file_content(file_path, new_str)
        return f"{result_prefix}File {action} successfully at: {file_path}"
    except (IOError, OSError) as e:
        return f"{result_prefix}Error operating on file {file_path}: {str(e)}"
    except Exception as e:
        return f"{result_prefix}An unexpected error occurred: {str(e)}"


def api_dbg(params: dict) -> str:
    """
    Executes dbgtool commands for function-level debugging.
    
    Args:
        params: Dictionary containing dbg parameters including:
            - action: The dbgtool command (break, step-into, step-out, continue, prev, call-graph, execute)
            - cmd: Python command to execute and debug (e.g., 'python debug.py', 'pytest -s test.py')
            - id: Frame ID in format "file_path:method_name#frame_index" (required for some commands)
            - condition: Boolean expression for conditional breakpoints (optional, break command only)
            - stmt: Python statement to execute (required for execute command except reset mode)
            - lineno: Line number where to execute (required for execute command except reset mode)
            - execution-index: Execution index (which time the line is executed to trigger execution) (required for execute command except reset mode)
            - mode: Execution mode (temp, persistent, reset) (optional for execute command, defaults to temp)
    
    Returns:
        A string containing the dbgtool execution result.
        
    Notes:
        - Frame ID is passed directly to dbgtool without parsing
        - For conditional breakpoints, dbgtool selects the last matching frame
    """
    result_prefix = "EXECUTION RESULT of [dbg]:\n"
    
    try:
        action = params.get("action", "")
        cmd = params.get("cmd", "")
        frame_id = params.get("id", "")
        condition = params.get("condition", "")
        stmt = params.get("stmt", "")
        lineno = params.get("lineno", "")
        execution_index = params.get("execution-index", "1")
        mode = params.get("mode", "temp")
        clear = params.get("clear", "False")
        
        if not action:
            return f"{result_prefix}Error: 'action' parameter is required."
        
        if not cmd:
            return f"{result_prefix}Error: 'cmd' parameter is required."
        
        # Build dbgtool command based on action
        if action == "break":
            if clear == "True":
                dbgtool_cmd = f"dbgtool break --cmd '{cmd}' --clear"
            else:
                if not frame_id:
                    return f"{result_prefix}Error: 'id' parameter is required for break command."
            
                dbgtool_cmd = f"dbgtool break --cmd '{cmd}' --id {frame_id}"
                if condition:
                    dbgtool_cmd += f" --condition '{condition}'"
                
        elif action == "step-into":
            if not frame_id:
                return f"{result_prefix}Error: 'id' parameter is required for step-into command."
            dbgtool_cmd = f"dbgtool step-into --cmd '{cmd}' --id {frame_id}"
            
        elif action == "step-out":
            dbgtool_cmd = f"dbgtool step-out --cmd '{cmd}'"
            
        elif action == "continue":
            dbgtool_cmd = f"dbgtool continue --cmd '{cmd}'"
            
        elif action == "prev":
            dbgtool_cmd = f"dbgtool prev --cmd '{cmd}'"
            
        elif action == "call-graph":
            dbgtool_cmd = f"dbgtool call-graph --cmd '{cmd}'"
            if frame_id:
                dbgtool_cmd += f" --id {frame_id}"
                
        elif action == "execute":
            # Validate mode parameter
            if mode not in ["temp", "persistent", "reset"]:
                return f"{result_prefix}Error: 'mode' parameter must be one of: temp, persistent, reset."
            
            # For reset mode, only cmd and mode are required
            if mode == "reset":
                dbgtool_cmd = f"dbgtool execute --cmd '{cmd}' --mode reset"
            else:
                # For temp and persistent modes, validate all required parameters
                if not frame_id:
                    return f"{result_prefix}Error: 'id' parameter is required for execute command."
                if not stmt:
                    return f"{result_prefix}Error: 'stmt' parameter is required for execute command."
                if not lineno:
                    return f"{result_prefix}Error: 'lineno' parameter is required for execute command."
                if not execution_index:
                    return f"{result_prefix}Error: 'execution-index' parameter is required for execute command."
                
                dbgtool_cmd = f"dbgtool execute --cmd '{cmd}' --id {frame_id} --stmt '{stmt}' --lineno {lineno} --execution-index {execution_index} --mode {mode}"
                
        else:
            return f"{result_prefix}Error: Unknown action '{action}'. Supported actions: break, step-into, step-out, continue, prev, call-graph, execute."
        
        # Execute the dbgtool command
        process = subprocess.run(
            dbgtool_cmd,
            shell=True,
            capture_output=True,
            text=True,
            check=False,
            timeout=60  # Set timeout for dbgtool commands
        )
        
        # Format the result
        result_content = ""
        if process.stdout:
            result_content += process.stdout
        if process.stderr:
            if result_content:
                result_content += "\nstderr:\n"
            result_content += process.stderr
        
        if process.returncode != 0:
            result_content += f"\n[Command exited with code {process.returncode}]"
        
        return f"{result_prefix}{result_content}"
        
    except subprocess.TimeoutExpired:
        return f"{result_prefix}Error: dbgtool command timed out after 60 seconds."
    except Exception as e:
        return f"{result_prefix}An unexpected error occurred: {str(e)}"


def api_task_report(params: dict) -> dict:
    """
    Generates a task report with git diff information for modified files.
    
    Args:
        params: Dictionary containing task report parameters including:
            - task_modify_files: Path of modified files (comma-separated if multiple)
            - task_analysis: Analysis of the changes made
            - task_resolve_success: Boolean indicating if the task was successfully resolved
    
    Returns:
        The task_report dictionary containing the task information and diffs.
    """
    # Extract task parameters from input
    task_modify_files = params.get("task_modify_files", "")
    task_analysis = params.get("task_analysis", "")
    task_resolve_success = params.get("task_resolve_success", "False")
    
    # Create dictionary for final report
    task_report = {
        "task_modify_files": task_modify_files,
        "task_analysis": task_analysis,
        "task_resolve_success": task_resolve_success,
        "task_modify_files_diff": {}
    }
    
    # Get git diff for each modified file using the robust function
    if task_modify_files:
        modified_files = [f.strip() for f in task_modify_files.split(",")]
        for file_path in modified_files:
            task_report["task_modify_files_diff"][file_path] = get_file_diff(file_path)
    
    # Return the report dictionary
    return task_report

# --- Main Dispatcher ---
def handle_api_call(call_string: str) -> str:
    """
    Parses an API call string and dispatches to the appropriate API function.
    """
    # call_string = escape_backslashes_in_quoted_strings(call_string)
    try:
        parsed_call = parse_api_call(call_string)
        func_name = parsed_call["function_name"]
        params = parsed_call["parameters"]

        if func_name == "execute_bash":
            if "cmd" not in params:
                return "EXECUTION RESULT of [execute_bash]:\nError: 'cmd' parameter is missing."
            return api_execute_bash(params["cmd"])
        
        elif func_name == "str_replace":
            required_params = ["file_path", "old_str", "new_str"]
            for p_name in required_params:
                if p_name not in params:
                    return f"EXECUTION RESULT of [str_replace]:\nError: '{p_name}' parameter is missing."
            return api_str_replace(params["file_path"], params["old_str"], params["new_str"])
            
        elif func_name == "new_file":
            required_params = ["file_path", "new_str"]
            for p_name in required_params:
                if p_name not in params:
                    return f"EXECUTION RESULT of [new_file]:\nError: '{p_name}' parameter is missing."
            return api_new_file(params["file_path"], params["new_str"])
        
        elif func_name == "search_def":
            required_params = ["file_path", "def"]
            for p_name in required_params:
                if p_name not in params:
                    return f"EXECUTION RESULT of [search_def]:\nError: '{p_name}' parameter is missing."
            return search_def(params["def"], params["file_path"])
        
        elif func_name == "task_report":
            report = api_task_report(params)
            return json.dumps(report)
        
        elif func_name == "pdb_interactive":
            # Validate required parameter
            if "command" not in params:
                return "EXECUTION RESULT of [pdb_interactive]:\nError: 'command' parameter is missing."
            
            # Set default working_dir if not provided
            if "working_dir" not in params:
                params["working_dir"] = "/testbed"
            
            # Call the PDB interactive API
            return api_pdb_interactive(params)
        
        elif func_name == "dbg":
            # Validate required parameters
            if "action" not in params:
                return "EXECUTION RESULT of [dbg]:\nError: 'action' parameter is missing."
            if "cmd" not in params:
                return "EXECUTION RESULT of [dbg]:\nError: 'cmd' parameter is missing."
            
            # Call the dbg API
            return api_dbg(params)
        
        else:
            # This case should ideally be caught by parse_api_call, but as a fallback:
            return f"EXECUTION RESULT of [unknown_function]:\nError: Unknown function '{func_name}'."

    except ValueError as e: # Catch parsing errors
        return f"API PARSING ERROR:\n{str(e)}"
    except Exception as e: # Catch any other unexpected errors during dispatch
        return f"UNEXPECTED DISPATCH ERROR:\n{str(e)}"



if __name__ == "__main__":
    os.system("clear")
    cmd = '''
<function>execute_bash</function>
<cmd>pytest /testbed/xarray/tests</cmd>
    '''
    print(handle_api_call(cmd))
    
  
    