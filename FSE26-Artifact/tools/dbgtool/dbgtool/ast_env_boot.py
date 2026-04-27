import subprocess
from pathlib import Path
import json

def ensure_ast_env():
    # Always use the absolute path /dbg-venv
    env_dir = Path("/dbg-venv")

    unix_python3 = env_dir / "bin" / "python3"
    unix_python = env_dir / "bin" / "python"
    win_python = env_dir / "Scripts" / "python.exe"

    # If the virtual environment already exists, return its python executable
    if unix_python3.exists():
        return str(unix_python3)
    if unix_python.exists():
        return str(unix_python)
    if win_python.exists():
        return str(win_python)

    print("Virtual environment not found, creating /dbg-venv ...")

    setup_cmd = """
    echo "Starting setup script..."
    apt-get update && \
    apt-get install -y python3.9-venv && \
    python3.9 -m venv /dbg-venv && \
    /dbg-venv/bin/pip install --upgrade pip && \
    /dbg-venv/bin/pip install langchain langchain-openai openai langchain_community loguru
    echo "DBG Agent completed."
    """

    subprocess.run(setup_cmd, shell=True, check=True, executable="/bin/bash")

    # Check again after creating the environment
    if unix_python3.exists():
        return str(unix_python3)
    if unix_python.exists():
        return str(unix_python)
    if win_python.exists():
        return str(win_python)

    raise RuntimeError("Virtual environment creation failed. Please run manually: python3.9 -m venv /dbg-venv")


def run_cmd_in_ast_env(cmd):
    python_path = ensure_ast_env()
    cmd = [python_path] + cmd

    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Command failed: {e.stderr}")


def run_get_statement_range(file_path, lineno):
    script_path = Path(__file__).resolve().parent / "query_code_scope.py"
    cmd = [str(script_path), 'get_statement_range', str(file_path), str(lineno)]
    result = run_cmd_in_ast_env(cmd)
    parsed = json.loads(result)
    return parsed["start_line"], parsed["end_line"]


def run_get_belonging_method(file_path, lineno):
    script_path = Path(__file__).resolve().parent / "query_code_scope.py"
    cmd = [str(script_path), 'get_belonging_method', str(file_path), str(lineno)]
    result = run_cmd_in_ast_env(cmd)
    return result


def run_get_method_range(file_path, method_name):
    script_path = Path(__file__).resolve().parent / "query_code_scope.py"
    cmd = [str(script_path), 'get_method_range', str(file_path), str(method_name)]
    result = run_cmd_in_ast_env(cmd)
    parsed = json.loads(result)
    if "error" in parsed:
        return parsed["error"]
    return parsed["start_line"], parsed["end_line"]
