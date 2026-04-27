# dbgtool/cli.py
import argparse
import json
import sys
from pathlib import Path
from typing import Tuple, Optional, Union
import os
import traceback
import subprocess
import shutil
import tempfile
import re
from .utils import find_first_non_import_line, parse_frame_id
from .render_call_tree import render_call_tree
from .ast_env_boot import run_get_method_range, run_get_statement_range


STATE_PATH = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'state.json'))
INSERT_STMT_PATH = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'insert_stmt.json'))


def init_state_data(cmd: str):
    if STATE_PATH.exists():
        state = load_state()
        if state.get("cmd", None) != cmd:
            # remove
            os.remove(STATE_PATH)
            state = {
                'target_frame_parent_id': None,
                'bp_frame_id': None,
                'curr_frame_id': None,
                'cmd': cmd,
            }
        else:
            state['curr_frame_id'] = None
    
    else:
        state = {
            'target_frame_parent_id': None,
            'bp_frame_id': None,
            'curr_frame_id': None,
            'cmd': cmd,
        }
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=4)
        

def check_frame_execution() -> bool:
    """Check if target frame was executed. Returns True if executed, False if not"""
    if not STATE_PATH.exists():
        return False
    
    with STATE_PATH.open("r", encoding="utf-8") as f:
        state = json.load(f)
    
    curr_frame_id = state.get('curr_frame_id')
    return curr_frame_id is not None


def print_frame_not_reached_warning(method_name, fpath, cmd):
    """Print warning message when target frame was not reached"""

    with STATE_PATH.open("r", encoding="utf-8") as f:
        state = json.load(f)

    candidate_method_names = state.get('candidate_method_names', [])
    if candidate_method_names:
        if not method_name in candidate_method_names:
            print(f"[dbgtool] Error: Method '{method_name}' not found in file '{fpath}'. Partial matched methods: {', '.join(candidate_method_names)}. Please check the method name and try again.")
        else:
            print("[dbgtool] Warning: Target frame was not reached. No dynamic information is collected.")

        state.pop('candidate_method_names')
    else:
        print(f"[dbgtool] Error: Method '{method_name}' not found in file '{fpath}'. Partial match also does not exist.")
    

    if "exception_frame" in state:
        exc_frame = state["exception_frame"]
        print(f"\n[dbgtool] Displaying the exception point frame {exc_frame}:")
        fpath, name, idx = parse_frame_id(exc_frame)
        dbgtool(cmd, fpath, name, idx)
        state.pop("exception_frame")
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=4)
    


def _find_last_python_invocation(cmd: str) -> Optional[Tuple[str, str, str]]:
    """Find the last python invocation in a possibly compound shell command.
    Returns (full_match, script_path, tail_after_script).
    tail_after_script excludes shell operators like &&, |, ;, >, <.
    """
    pattern = r'(?:\S*python3?\b)\s+([^\s]+\.py)([^;&|><]*)'
    matches = list(re.finditer(pattern, cmd))
    if not matches:
        return None
    m = matches[-1]
    return m.group(0), m.group(1), (m.group(2) or '')


def extract_python_script_path(cmd: str) -> Optional[str]:
    """Extract Python script path from the last python invocation in command."""
    found = _find_last_python_invocation(cmd.strip())
    if not found:
        return None
    _, script_path, _ = found
    return script_path if script_path.endswith('.py') else None


def is_simple_python_command(cmd: str) -> bool:
    """Check if the last python invocation is 'python file.py' with no extra args."""
    found = _find_last_python_invocation(cmd.strip())
    if not found:
        return False
    _, _, tail = found
    return tail.strip() == ''




def dbgtool(cmd: str, fpath: str, name: str, idx: int, call_graph_mode = False, condition = None, mute = False):
    # Get method range
    fpath = os.path.abspath(fpath)
    frame_id = f'{fpath}:{name}#{idx}'

    script_path = extract_python_script_path(cmd)
    target_file = os.path.abspath(script_path if script_path else fpath)

    is_python_mode = is_simple_python_command(cmd)
    
    if is_python_mode:
        output_path = None
    else:
        output_path = tempfile.mktemp(suffix='.json')
    
    if not os.path.exists(target_file):
        print(f"Error: File not found: {target_file}")
        return
        
    try:
        # Modify file and inject tracer code
        with open(target_file, 'r', encoding='utf-8') as f:
            original_content = f.read()
        original_lines = original_content.split('\n')
        
        # Find the first non-import line position
        insert_line = find_first_non_import_line(original_lines)

        # Build tracer initialization code
        tracer_params = f"target_frame_id='{frame_id}', call_graph_mode={call_graph_mode}, condition={repr(condition)}"
        if output_path:
            tracer_params += f", output='{output_path}'"
            
        tracer_lines = [
            "from dbgtool import Tracer",
            f"dbgtool_tracer = Tracer({tracer_params})"
        ]
        
        # Insert tracer code
        if insert_line <= len(original_lines):
            # Insert before the first non-import line
            modified_lines = (original_lines[:insert_line-1] + 
                            tracer_lines + 
                            original_lines[insert_line-1:])
        else:
            # File contains only imports/comments, append at end
            modified_lines = original_lines + tracer_lines
        
        # Write modified file
        with open(target_file, 'w', encoding='utf-8') as f:
            f.write('\n'.join(modified_lines))

        # Run command with different output handling
        if (not is_python_mode) or call_graph_mode or mute:
            result = subprocess.run(
                cmd,
                shell=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=None
            )
            
            #  Handle call_graph_mode results
            if mute:
                return
            if call_graph_mode:
                call_graph_data_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'call_graph_data.json')
                if os.path.exists(call_graph_data_path):
                    print(render_call_tree(call_graph_data_path))
                    os.remove(call_graph_data_path)
                else:
                    print("[dbgtool] Warning: call_graph_data.json not found")

            elif os.path.exists(output_path):
                with open(output_path, 'r', encoding='utf-8') as f:
                    output_content = f.read()
                    if output_content.strip():
                        print(output_content)

        else:
            result = subprocess.run(
                cmd,
                shell=True,
                stdout=None,  # Inherit from parent (current terminal)
                stderr=None,  # Inherit from parent (current terminal)
                stdin=None
            )
        
        # if result.returncode != 0:
        #     print(f"[dbgtool] Command execution failed with return code: {result.returncode}")
        


    except Exception as e:
        print(f"Error occurred during command execution: {e}")
        traceback.print_exc()
    
    finally:
        # Restore original file
        try:
            with open(target_file, 'w', encoding='utf-8') as f:
                f.write(original_content)
        except Exception as e:
            print(f"Error: Failed to restore original file: {e}")
        
        if output_path and os.path.exists(output_path):
            try:
                os.remove(output_path)
            except:
                pass




def load_state(state_path: Path = None) -> dict:
    state_path = state_path or STATE_PATH
    if not state_path.exists():
        raise SystemExit(
            f"[state] Not found previous observed frame. Please use `step-into` or `break` first."
        )
    with state_path.open("r", encoding="utf-8") as f:
        return json.load(f)


# -------- Command implementations --------
def run_step_into(cmd: str, frame_id: str):
    fpath, name, idx = parse_frame_id(frame_id)
    if idx == -1:
        dbgtool(cmd, fpath, name, idx, mute = True)
        with STATE_PATH.open("r", encoding="utf-8") as f:
            state = json.load(f)
            if 'last_focus_frame_id' not in state:
                print(f"[break] No frames executed for {fpath}:{name}")
                return
            else:
                frame_id = state['last_focus_frame_id']
                fpath, name, idx = parse_frame_id(frame_id)

    print(f"[step-into] You step into the frame {fpath}:{name}#{idx}")
    
    dbgtool(cmd, fpath, name, idx)
    
    # Check if frame was executed
    if not check_frame_execution():
        print_frame_not_reached_warning(name, fpath, cmd)



def run_step_out(cmd: str):
    state = load_state()
    parent_id = state.get("target_frame_parent_id")
    if not parent_id or '#' not in parent_id or ':' not in parent_id:
        raise SystemExit("[step-out] No caller frame available.")
    fpath, name, idx = parse_frame_id(parent_id)

    print(f"[step-out] You step out to the caller frame {fpath}:{name}#{idx}")

    dbgtool(cmd, fpath, name, idx)
    
    # Check if frame was executed
    if not check_frame_execution():
        print_frame_not_reached_warning(name, fpath, cmd)


def run_break(cmd: str, frame_id: str, condition: Optional[str], clear: bool = False):
    if clear and STATE_PATH.exists():
        state = load_state()
        state['bp_frame_id'] = None
        with STATE_PATH.open("w", encoding="utf-8") as f:
            json.dump(state, f, indent=4)
        print("[break] Existing breakpoint cleared.")
        return
    fpath, name, idx = parse_frame_id(frame_id)

    if idx == -1:
        dbgtool(cmd, fpath, name, idx, mute = True)
        with STATE_PATH.open("r", encoding="utf-8") as f:
            state = json.load(f)
            if 'last_focus_frame_id' not in state:
                print(f"[break] No frames executed for {fpath}:{name}")
                return
            else:
                frame_id = state['last_focus_frame_id']
                fpath, name, idx = parse_frame_id(frame_id)
                

    bp_frame_index = idx

    with STATE_PATH.open("r", encoding="utf-8") as f:
        state = json.load(f)

    state['bp_frame_id'] = frame_id
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=4)

    if condition is not None:
        print(f"[break] Breakpoint set at {fpath}:{name} with condition: {condition}")
    else:
        print(f"[break] Breakpoint and focus frame set at {frame_id}.")
        
    dbgtool(cmd, fpath, name, bp_frame_index, condition=condition)
    
    # Check if frame was executed
    if not check_frame_execution():
        print_frame_not_reached_warning(name, fpath, cmd)


def run_continue(cmd: str):
    state = load_state()
    bp_frame_id = state.get("bp_frame_id", None)
    if not bp_frame_id:
        raise SystemExit("[continue] Missing breakpoint info. Use `break` first.")
    file_path_str, method_name, bp_index = parse_frame_id(bp_frame_id)
    file_path_str = os.path.abspath(file_path_str)

    if not os.path.exists(file_path_str):
        raise SystemExit(f"[continue] File not found: {file_path_str}")

    file_path = Path(os.path.abspath(file_path_str)).resolve()

    target_index = bp_index + 1

    print(f"[continue] You continue to the next breakpoint frame {file_path}:{method_name}#{target_index}")
    
    dbgtool(cmd, file_path, method_name, target_index)
    
    # Check if frame was executed
    if not check_frame_execution():
        print_frame_not_reached_warning(method_name, file_path, cmd)


def run_prev(cmd: str):
    state = load_state()
    bp_frame_id = state.get("bp_frame_id", None)
    if not bp_frame_id:
        raise SystemExit("[continue] Missing breakpoint info. Use `break` first.")
    file_path_str, method_name, bp_index = parse_frame_id(bp_frame_id)
    file_path_str = os.path.abspath(file_path_str)

    file_path = Path(file_path_str).resolve()
    target_index = max(1, bp_index - 1)
    if target_index == bp_index:
        raise SystemExit("[prev] Already at the first breakpoint frame.")

    print(f"[prev] You step back to the previous breakpoint frame {file_path}:{method_name}#{target_index}")
    
    
    dbgtool(cmd, file_path, method_name, target_index)
    
    # Check if frame was executed
    if not check_frame_execution():
        print_frame_not_reached_warning(method_name, file_path, cmd)


def run_call_graph(cmd: str, frame_id: Optional[str] = None):
    if frame_id:
        fpath, name, idx = parse_frame_id(frame_id)
    else:
        state = load_state()
        curr_frame_id = state.get("curr_frame_id")
        if not curr_frame_id:
            raise SystemExit(
                "[call-graph] Missing context. Provide --id or set a breakpoint or step into a frame first."
            )
        fpath, name, idx = parse_frame_id(curr_frame_id)

    print(f'[call-graph] The downstream 3 levels call graph for {fpath}:{name}#{idx} is as follows:\n')

    init_state_data(cmd)
    dbgtool(cmd, fpath, name, idx, call_graph_mode=True)
    
    # Check if frame was executed
    if not check_frame_execution():
        print_frame_not_reached_warning(name, fpath, cmd)


def run_execute(cmd: str, frame_id: str = None, stmt: str = None, lineno: int = None, execution_index: str = "1", mode: str = "temp"):
    """Execute Python statements dynamically with different modes."""
    if mode == "reset":
        # Reset mode: just delete the config file
        if INSERT_STMT_PATH.exists():
            os.remove(INSERT_STMT_PATH)
            print("[execute] All persistent execute statements cleared.")
        else:
            print("[execute] No persistent execute statements to clear.")
        return
    
    fpath, name, idx = parse_frame_id(frame_id)
    fpath = os.path.abspath(fpath)

    # if lineno is empty line or comment line, find the next executable line
    with open(fpath, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if lineno < 1 or lineno > len(lines):
        raise SystemExit(f"[execute] Invalid lineno: {lineno}")
    line_content = lines[lineno - 1].strip()
    if not line_content.strip() or line_content.startswith("#"):
        for i in range(lineno, len(lines)):
            if lines[i].strip() and not lines[i].strip().startswith("#"):
                lineno = i + 1
                break
    
    start_line, end_line = run_get_statement_range(fpath, lineno)


    frame_id_normalized = f"{fpath}:{name}#{idx}"
    
    # Create insert_stmt.json for dynamic statement execution
    insert_data = {
        "stmt": stmt,
        "frame_id": frame_id_normalized,
        "start": start_line,
        "end": end_line,
        "loop_index": execution_index
    }
    
    with INSERT_STMT_PATH.open("w", encoding="utf-8") as f:
        json.dump(insert_data, f, indent=4)
    
    print(f"[execute] Statement injected for {mode} mode:")
    print(f"  Frame: {frame_id_normalized}")
    print(f"  Line: {lineno}")
    print(f"  Statement: {stmt}")

    
    # Execute the frame using step-into logic
    dbgtool(cmd, fpath, name, idx)
    
    # Check if frame was executed
    if not check_frame_execution():
        print_frame_not_reached_warning(name, fpath, cmd)
    
    # Handle cleanup based on mode
    if mode == "temp":
        # For temp mode, remove the config after execution
        if INSERT_STMT_PATH.exists():
            os.remove(INSERT_STMT_PATH)
        print(f"[execute] Temp execution completed and cleaned up.")
    elif mode == "persistent":
        # For persistent mode, keep the config for future executions
        print(f"[execute] Persistent execution configured. Statement will remain active for future runs.")
        print(f"[execute] Use 'dbgtool execute --mode reset --cmd \"{cmd}\"' to clear persistent statements.")

# -------- CLI entry point --------
def main(argv=None):
    argv = argv or sys.argv[1:]

    parser = argparse.ArgumentParser(
        prog="dbgtool",
        description="Function-level debugging tool (frame navigation & call graph).",
    )

    subparsers = parser.add_subparsers(dest="command")

    # step-into
    p_step_into = subparsers.add_parser("step-into", help="Step into a specific frame.")
    p_step_into.add_argument("--id", required=True, help='Frame ID, format: "file_path:method#index"')
    p_step_into.add_argument("--cmd", required=True, help="Command to run the test.")

    # step-out
    p_step_out = subparsers.add_parser("step-out", help="Step out to the caller frame.")
    p_step_out.add_argument("--cmd", required=True, help="Command to run the test.")

    # break (path + method_name; default index = 1)
# --- argparse section ---

    p_break = subparsers.add_parser("break", help="Set a breakpoint and focus index=1.")
    p_break.add_argument("--id", help='Frame ID, format: "file_path:method#index"')
    p_break.add_argument("--condition", required=False,
                        help="Optional boolean expression; breakpoint is active only when it evaluates to True.")
    p_break.add_argument("--cmd", required=True, help="Command to run the test.")
    p_break.add_argument("--clear", action="store_true", help="Clear existing breakpoint before setting a new one.")


    # continue / prev
    p_cont = subparsers.add_parser("continue", help="Jump to next breakpoint frame (bp_index + 1).")
    p_cont.add_argument("--cmd", required=True, help="Command to run the test.")

    p_prev = subparsers.add_parser("prev", help="Jump to previous breakpoint frame (bp_index - 1).")
    p_prev.add_argument("--cmd", required=True, help="Command to run the test.")

    # call-graph
    p_cg = subparsers.add_parser("call-graph", help="Show call graph (3 levels).")
    p_cg.add_argument("--id", help='Optional frame ID, format: "file_path:method#index"')
    p_cg.add_argument("--cmd", required=True, help="Command to run the test.")

    # execute
    p_execute = subparsers.add_parser("execute", help="Execute Python statements dynamically at runtime.")
    p_execute.add_argument("--id", help='Frame ID, format: "file_path:method#index"')
    p_execute.add_argument("--stmt", help="Python statement to execute")
    p_execute.add_argument("--lineno", type=int, help="Line number where to execute")
    p_execute.add_argument("--execution-index", default="1",
                         help="Execution index (which time the line is executed to trigger execution, default: 1)")
    p_execute.add_argument("--mode", choices=["temp", "persistent", "reset"], default="temp",
                         help="Execution mode: temp (one-time), persistent (stays active), reset (clear all persistent executions)")
    p_execute.add_argument("--cmd", required=True, help="Command to run the test.")


    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.error("a subcommand is required")

    cmd = args.cmd.strip()
    
    # Handle insert_stmt.json cleanup when cmd changes
    if INSERT_STMT_PATH.exists():
        try:
            if STATE_PATH.exists():
                state = load_state()
                if state.get("cmd", None) != cmd:
                    os.remove(INSERT_STMT_PATH)
                    print(f"[cleanup] Removed previous insert statement configuration due to command change")
        except (json.JSONDecodeError, KeyError):
            pass
    
    if args.command != "call-graph":
        init_state_data(cmd)


    if args.command == "step-into":
        run_step_into(cmd, args.id)
    elif args.command == "step-out":
        run_step_out(cmd)
    elif args.command == "break":
        if args.clear:
            run_break(cmd, None, None, args.clear)
        else:
            if not args.id:
                parser.error("break: --id is required unless for break command.")
            run_break(cmd, args.id, args.condition)
    elif args.command == "continue":
        run_continue(cmd)
    elif args.command == "prev":
        run_prev(cmd)
    elif args.command == "call-graph":
        run_call_graph(cmd, args.id)
    elif args.command == "execute":  
        # For reset mode, only cmd is required
        if args.mode == "reset":
            run_execute(cmd, mode=args.mode)
        else:
            # For temp and persistent modes, validate required parameters
            if not args.id:
                parser.error("execute: --id is required for temp and persistent modes")
            if not args.stmt:
                parser.error("execute: --stmt is required for temp and persistent modes")
            if args.lineno is None:
                parser.error("execute: --lineno is required for temp and persistent modes")
            
            run_execute(cmd, args.id, args.stmt, args.lineno, args.execution_index, args.mode)
    else:
        parser.error(f"Unknown command: {args.command}")


if __name__ == "__main__":
    main()
