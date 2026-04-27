import re
import json
from loguru import logger
import os
from typing import Dict, Optional, List
from dataclasses import dataclass, asdict

DEBUG_STD_OUT = False

@dataclass
class TruncationInfo:
    """Information about response truncation"""
    truncated: bool
    original_function_calls: int
    kept_function_calls: int
    removed_content: str
    removed_chars: int
    kept_function_summary: str
    removed_function_summaries: List[str]

def extract_code_blocks(text: str):
    pattern = re.compile(r"<function_call>(.*?)</function_call>", re.DOTALL)
    blocks = [block.strip() for block in pattern.findall(text)]
    
    # Process each block to remove language identifiers
    cleaned_blocks = []
    for block in blocks:
        # Check if the block starts with a language identifier (e.g., python, bash, rust)
        lines = block.split('\n', 1)
        if len(lines) > 1 and re.match(r'^(python|bash|rust|js|javascript|typescript|ts|go|java|c|cpp|csharp|cs|ruby|php|swift|kotlin|scala|perl|r|shell|powershell|sql|html|css|xml|yaml|json|markdown|md)$', lines[0].strip()):
            # Remove the language identifier line and add only the code content
            cleaned_blocks.append(lines[1].strip())
        else:
            cleaned_blocks.append(block.strip())
    
    # Filter to only include function blocks that contain <function> tag
    function_blocks = []
    for block in cleaned_blocks:
        # Check if this block contains a <function> tag (XML format)
        if '<function>' in block:
            function_blocks.append(block)
    
    return function_blocks

def _extract_function_summary(func_call_content: str) -> str:
    """Extract a brief summary of a function call for logging"""
    lines = func_call_content.strip().split('\n')
    if not lines:
        return "unknown"
    
    # Try to extract function name from XML format first
    func_match = re.search(r'<function>(.*?)</function>', func_call_content)
    if func_match:
        func_name = func_match.group(1).strip()
        
        # Try to get the first parameter for context
        # Look for any XML tag that might contain parameters
        param_match = re.search(r'<(cmd|file_path|action)>(.*?)</\1>', func_call_content)
        if param_match:
            param_name = param_match.group(1)
            param_value = param_match.group(2).strip()
            # Limit parameter value length for readability
            if len(param_value) > 30:
                param_value = param_value[:27] + "..."
            return f"{func_name} ({param_name}:{param_value})"
        
        return func_name
    
    # If no XML format found, return unknown
    return "unknown"


def clean_response_for_history(response: str) -> tuple[str, Optional[TruncationInfo]]:
    """
    Clean LLM response to keep only the first function call and preceding text.
    This prevents hallucinated content or extra function calls from being saved to conversation history.
    
    Args:
        response: Complete LLM response
        
    Returns:
        Tuple of (cleaned_response, truncation_info)
        - cleaned_response: Response containing only the first function call
        - truncation_info: Details about what was truncated (None if no truncation)
    """
    # Find all function call blocks
    pattern = re.compile(r"<function_call>(.*?)</function_call>", re.DOTALL)
    matches = list(pattern.finditer(response))
    
    if not matches:
        # If no function call found, return original response
        return response, None
    
    if len(matches) == 1:
        # Only one function call, no truncation needed
        return response, None
    
    # Multiple function calls detected, truncation needed
    first_call_end = matches[0].end()
    
    # Keep only content up to the end of the first function call
    cleaned_response = response[:first_call_end]
    # cleaned_response += "\n\n[Note: Response was truncated to include only the first function call]"
    
    # Extract removed content
    removed_content = response[first_call_end:]
    
    # Extract function summaries
    kept_function_summary = _extract_function_summary(matches[0].group(1))
    removed_function_summaries = []
    
    for match in matches[1:]:
        summary = _extract_function_summary(match.group(1))
        removed_function_summaries.append(summary)
    
    # Create truncation info
    truncation_info = TruncationInfo(
        truncated=True,
        original_function_calls=len(matches),
        kept_function_calls=1,
        removed_content=removed_content,
        removed_chars=len(removed_content),
        kept_function_summary=kept_function_summary,
        removed_function_summaries=removed_function_summaries
    )
    
    return cleaned_response, truncation_info

def log_truncation_info(truncation_info: TruncationInfo, session_id: str, round_count: int = 0):
    """Log truncation information in a structured way"""
    if not truncation_info or not truncation_info.truncated:
        return
    
    # Log summary information
    removed_funcs_str = ", ".join(truncation_info.removed_function_summaries)
    log_info(f"[TRUNCATION] {session_id} Round {round_count}: "
             f"{truncation_info.original_function_calls}→1 function calls, "
             f"removed {truncation_info.removed_chars} chars")
    log_info(f"  → Kept: {truncation_info.kept_function_summary}")
    log_info(f"  → Removed: {removed_funcs_str}")

def init_logger(log_path: str):
    with open(log_path, 'w') as f:
        pass
    logger.remove()
    logger.add(log_path, level="INFO", encoding="utf-8", enqueue=True, backtrace=True, diagnose=True)
    if DEBUG_STD_OUT:
        logger.add(lambda msg: print(msg, end=""), level="INFO")


def log_info(msg: str):
    logger.info(msg)

def log_error(msg: str):
    logger.error(msg)


def get_instance_info(instance_id: str):
    
    current_dir = os.path.dirname(os.path.abspath(__file__))
    data_file_path = os.path.join(os.path.dirname(current_dir), 'data', 'SWE-bench.jsonl')
    with open(data_file_path, 'r') as f:
        for line in f:
            swe_dataset = json.loads(line)
            if instance_id != swe_dataset['instance_id']:
                continue
            instance_bug_report = swe_dataset['problem_statement']
            return instance_bug_report
    
if __name__ == "__main__":
    print(get_instance_info('astropy__astropy-12907'))
    