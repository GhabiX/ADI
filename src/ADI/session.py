"""Session management for ADI MCP Server"""

from typing import Dict, List, Optional
import uuid


class TraceStep:
    """Single step in execution trace"""
    def __init__(self, lineno: int, stmt: str,
                 new_vars: Dict[str, str] = None,
                 modified_vars: Dict[str, str] = None,
                 callee_frame_id: Optional[str] = None,
                 iter_num: Optional[int] = None,
                 skipped_before: Optional[int] = None):
        self.lineno = lineno
        self.stmt = stmt
        self.new_vars = new_vars if new_vars is not None else {}
        self.modified_vars = modified_vars if modified_vars is not None else {}
        self.callee_frame_id = callee_frame_id
        self.iter_num = iter_num
        self.skipped_before = skipped_before  # Number of iterations skipped before this step


class FLT:
    """Frame Lifetime Trace"""
    def __init__(self, frame_id: str, caller_frame_id: Optional[str],
                 args: Dict[str, str], return_value: Optional[str],
                 trace: List[TraceStep],
                 skipped_iterations: Optional[int] = None,
                 cached_iter_vars: List[tuple] = None):
        self.frame_id = frame_id
        self.caller_frame_id = caller_frame_id
        self.args = args
        self.return_value = return_value
        self.trace = trace
        self.skipped_iterations = skipped_iterations
        self.cached_iter_vars = cached_iter_vars if cached_iter_vars is not None else []


class Session:
    """Debug session state"""
    def __init__(self, session_id: str, cmd: str,
                 bp_func: Optional[str] = None,
                 bp_index: int = 1,
                 bp_condition: Optional[str] = None,
                 bp_condition_when: str = "entry",
                 bp_if_eval_lineno: Optional[int] = None,
                 bp_loop: int = 2,
                 bp_loop_index: Optional[int] = None,
                 bp_full_watch: Optional[List[str]] = None,
                 bp_timeout: int = 60,
                 bp_count_total_calls: bool = True,
                 bp_allow_external_target: bool = False,
                 curr_frame_id: Optional[str] = None,
                 parent_frame_id: Optional[str] = None,
                 candidate_methods: List[str] = None):
        self.session_id = session_id
        self.cmd = cmd
        self.bp_func = bp_func
        self.bp_index = bp_index
        self.bp_condition = bp_condition
        self.bp_condition_when = bp_condition_when
        self.bp_if_eval_lineno = bp_if_eval_lineno
        self.bp_loop = bp_loop
        self.bp_loop_index = bp_loop_index
        self.bp_full_watch = bp_full_watch
        self.bp_timeout = bp_timeout
        self.bp_count_total_calls = bp_count_total_calls
        self.bp_allow_external_target = bp_allow_external_target
        self.curr_frame_id = curr_frame_id
        self.parent_frame_id = parent_frame_id
        self.candidate_methods = candidate_methods if candidate_methods is not None else []


class SessionManager:
    """Manages multiple debug sessions"""

    def __init__(self):
        self._sessions: Dict[str, Session] = {}

    def create_session(self, cmd: str) -> Session:
        """Create a new debug session"""
        session_id = uuid.uuid4().hex[:8]
        session = Session(session_id=session_id, cmd=cmd)
        self._sessions[session_id] = session
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        """Get session by ID"""
        return self._sessions.get(session_id)

    def close_session(self, session_id: str) -> bool:
        """Close and remove a session"""
        if session_id in self._sessions:
            del self._sessions[session_id]
            return True
        return False

    def list_sessions(self) -> List[str]:
        """List all session IDs"""
        return list(self._sessions.keys())
