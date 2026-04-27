"""
Core PDB session management for interactive Python debugging.
Provides PTY-based communication with Python debugger.
"""

import os
import pty
import fcntl
import termios
import subprocess
import shlex
import re
import time
import errno
from dataclasses import dataclass
from typing import Optional


@dataclass
class PDBResult:
    """Result of a PDB command execution."""
    success: bool
    output: str
    error: Optional[str] = None


class PDBSession:
    """
    Manages a Python debugger (PDB) session using pseudo-terminal (PTY).
    Simple and robust PDB communication for interactive debugging.
    """
    
    def __init__(self, working_dir: str, debug_command: str = "python -m pdb"):
        """
        Initialize PDB session.
        
        Args:
            working_dir: Directory where debugging takes place
            debug_command: Command to start PDB (default: "python -m pdb")
        """
        self.working_dir = os.path.abspath(working_dir)
        self.debug_command = debug_command
        self.process = None
        self.master_fd = None
        self._session_active = False
        
    def start(self, script_path: Optional[str] = None) -> PDBResult:
        """
        Start PDB session.
        
        Args:
            script_path: Optional Python script to debug
            
        Returns:
            PDBResult with startup information
        """
        try:
            # Check file existence
            if script_path and not os.path.exists(script_path):
                return PDBResult(
                    success=False,
                    output="",
                    error=f"Script file does not exist: {script_path}"
                )
            
            # Build debug command
            cmd = self.debug_command
            if script_path:
                cmd = f"{self.debug_command} {script_path}"
            
            # Create PTY pair
            master, slave = pty.openpty()
            self.master_fd = master
            
            # Configure PTY for non-blocking reads
            flags = fcntl.fcntl(master, fcntl.F_GETFL)
            fcntl.fcntl(master, fcntl.F_SETFL, flags | os.O_NONBLOCK)
            
            # Disable echo on slave terminal
            attrs = termios.tcgetattr(slave)
            attrs[3] = attrs[3] & ~termios.ECHO
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
            
            # Start PDB process
            self.process = subprocess.Popen(
                shlex.split(cmd),
                cwd=self.working_dir,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                text=True,
                start_new_session=True
            )
            
            # Close slave fd in parent process
            os.close(slave)
            
            # Wait for PDB prompt
            initial_output = self._read_until_prompt(timeout=10)
            
            # Simple startup validation
            if "(Pdb)" not in initial_output:
                self._cleanup()
                return PDBResult(
                    success=False,
                    output=initial_output,
                    error="PDB failed to start properly"
                )
            
            self._session_active = True
            
            return PDBResult(
                success=True,
                output=self._clean_output(initial_output)
            )
            
        except Exception as e:
            self._cleanup()
            return PDBResult(
                success=False,
                output="",
                error=f"Failed to start PDB: {str(e)}"
            )
    
    def execute(self, command: str, timeout: int = 30) -> PDBResult:
        """
        Execute a PDB command.
        
        Args:
            command: PDB command to execute
            timeout: Timeout in seconds
            
        Returns:
            PDBResult with command output
        """
        if not self.is_active():
            return PDBResult(
                success=False,
                output="",
                error="PDB session not active"
            )
        
        if not command.strip():
            return PDBResult(
                success=False,
                output="",
                error="Empty command"
            )
        
        try:
            # Send command to PDB
            self._write_command(command)
            
            # Read response
            output = self._read_until_prompt(timeout)
            
            # Check if program ended
            if self._is_program_ended(output):
                self._session_active = False
                return PDBResult(
                    success=True,
                    output=self._clean_output(output),
                    error="Program execution completed"
                )
            
            return PDBResult(
                success=True,
                output=self._clean_output(output)
            )
            
        except TimeoutError:
            return PDBResult(
                success=False,
                output="",
                error=f"Command timed out after {timeout} seconds"
            )
        except Exception as e:
            return PDBResult(
                success=False,
                output="",
                error=f"Command execution failed: {str(e)}"
            )
    
    def is_active(self) -> bool:
        """Check if PDB session is active."""
        return (
            self._session_active and 
            self.process is not None and 
            self.process.poll() is None and
            self.master_fd is not None
        )
    
    def close(self) -> None:
        """Close PDB session and cleanup resources."""
        self._cleanup()
    
    def _write_command(self, command: str) -> None:
        """Write command to PDB process."""
        if self.master_fd is None:
            raise RuntimeError("No active PDB session")
        
        cmd_bytes = (command + "\n").encode("utf-8")
        os.write(self.master_fd, cmd_bytes)
    
    def _read_until_prompt(self, timeout: int = 30) -> str:
        """Read output until PDB prompt appears."""
        if self.master_fd is None:
            raise RuntimeError("No active PDB session")
        
        output = ""
        start_time = time.time()
        
        while time.time() - start_time < timeout:
            try:
                data = os.read(self.master_fd, 4096).decode("utf-8", errors="ignore")
                if data:
                    output += data
                    if "(Pdb)" in output:
                        break
                else:
                    time.sleep(0.01)
            except OSError as e:
                if e.errno == errno.EAGAIN or e.errno == errno.EWOULDBLOCK:
                    time.sleep(0.1)
                    continue
                elif e.errno == errno.EIO:
                    break
                else:
                    raise
        else:
            raise TimeoutError(f"No PDB prompt received within {timeout} seconds")
        
        return output
    
    def _clean_output(self, output: str) -> str:
        """Clean PDB output by removing ANSI codes and prompt."""
        # Remove ANSI escape sequences
        clean = re.sub(r'\x1b\[[0-9;]*[mGKHF]', '', output)
        # Remove PDB prompt
        clean = clean.replace("(Pdb)", "").strip()
        # Remove extra whitespace
        clean = re.sub(r'\n\s*\n', '\n', clean).strip()
        return clean
    
    def _is_program_ended(self, output: str) -> bool:
        """Check if program execution has completed."""
        end_indicators = [
            "The program finished",
            "The program exited",
            "--Return--",
            "Post mortem debugger finished"
        ]
        return any(indicator in output for indicator in end_indicators)
    
    def _cleanup(self) -> None:
        """Clean up session resources."""
        self._session_active = False
        
        if self.process:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except (subprocess.TimeoutExpired, ProcessLookupError):
                try:
                    self.process.kill()
                    self.process.wait(timeout=2)
                except (subprocess.TimeoutExpired, ProcessLookupError):
                    pass
            self.process = None
        
        if self.master_fd:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        self.close()
    
    def __del__(self):
        """Destructor - ensure cleanup."""
        self._cleanup()


# Global session for maintaining state across API calls
_global_session = None


def get_pdb_session(working_dir: str) -> PDBSession:
    """
    Get or create a global PDB session.
    
    Args:
        working_dir: Working directory for the session
        
    Returns:
        Active PDBSession instance
    """
    global _global_session
    
    if _global_session is None or not _global_session.is_active():
        _global_session = PDBSession(working_dir)
    
    return _global_session


def close_pdb_session() -> None:
    """Close the global PDB session."""
    global _global_session
    
    if _global_session is not None:
        _global_session.close()
        _global_session = None


def api_pdb_interactive(params: dict) -> str:
    """
    Interactive PDB API for step-by-step debugging.
    
    Parameters:
    - command: PDB command to execute
    - script_path: Path to Python script (for starting session)
    - working_dir: Working directory (default: /testbed)
    
    Returns:
        Formatted execution result
    """
    command = params.get("command", "")
    script_path = params.get("script_path", "")
    working_dir = params.get("working_dir", "/testbed")
    
    if not command:
        return "EXECUTION RESULT of [pdb_interactive]:\nError: 'command' parameter is required."
    
    try:
        session = get_pdb_session(working_dir)
        
        # Start session if script_path provided and session not active
        if script_path and not session.is_active():
            start_result = session.start(script_path)
            if not start_result.success:
                return f"EXECUTION RESULT of [pdb_interactive]:\nError starting session: {start_result.error}"
        
        # Execute command
        result = session.execute(command)
        
        if result.success:
            output = f"PDB Command: {command}\n"
            if result.output:
                output += f"Output:\n{result.output}"
            if result.error:
                output += f"\nNote: {result.error}"
            return f"EXECUTION RESULT of [pdb_interactive]:\n{output}"
        else:
            return f"EXECUTION RESULT of [pdb_interactive]:\nError: {result.error}"
            
    except Exception as e:
        return f"EXECUTION RESULT of [pdb_interactive]:\nError: {str(e)}" 