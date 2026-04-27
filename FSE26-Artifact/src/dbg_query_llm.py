import os
import json
from typing import Dict, List
from openai import OpenAI

from dbg_prompt import dbg_system_prompt
from dbg_utils import clean_response_for_history, log_truncation_info


# Configuration
BASE_URL = ""
API_KEY = ""


if not API_KEY:
    raise ValueError("TOKEN environment variable is required")

# Global session store
_sessions: Dict[str, List[Dict[str, str]]] = {}

# Global truncation tracking
_truncation_stats: Dict[str, List[Dict]] = {}

# Pricing (USD per MTok)
_MODEL_PRICING_USD_PER_MTOK = {
    # Keys are canonical model families; use resolver below to map real model names
    "claude-sonnet-3.7": {"input": 3.0, "output": 15},
    "claude-sonnet-3.5": {"input": 3.0, "output": 15},
    "claude-opus-4": {"input": 15.0, "output": 75},
    "claude-sonnet-4": {"input": 3.0, "output": 15},
    "gpt-4o": {"input": 2.5, "output": 10}
}


def _resolve_model_family(model_name: str) -> str:
    """Map provider-specific model name to a canonical pricing family key.

    Attempts to be robust to different naming styles, e.g. "claude-3-7-sonnet-20250219".
    Returns empty string if unknown.
    """
    name = (model_name or "").lower()

    # Claude Sonnet 3.7
    if ("sonnet" in name) and ("3-7" in name or "3.7" in name):
        return "claude-sonnet-3.7"

    # Claude Sonnet 3.5
    if ("sonnet" in name) and ("3-5" in name or "3.5" in name):
        return "claude-sonnet-3.5"

    # Claude Opus 4
    if ("opus" in name) and ("4" in name):
        return "claude-opus-4"

    # Claude Sonnet 4
    if ("sonnet" in name) and ("4" in name) and ("3-" not in name and "3." not in name):
        return "claude-sonnet-4"
    
    if ("gpt" in name) and ("4o" in name):
        return "gpt-4o"
    return ""


def _get_model_pricing(model_name: str) -> Dict[str, float]:
    """Return pricing dict {input, output} in USD per MTok for the model.

    Unknown models default to zero cost to avoid raising during inference.
    """
    family = _resolve_model_family(model_name)
    return _MODEL_PRICING_USD_PER_MTOK.get(family, {"input": 0.0, "output": 0.0})


class SessionHistory:
    """Session history wrapper for compatibility"""
    
    def __init__(self, messages: List[Dict[str, str]]):
        self.messages = messages
    
    def model_dump_json(self) -> str:
        return json.dumps({"messages": self.messages})


def get_session_history(session_id: str) -> SessionHistory:
    """Get or create session history"""
    if session_id not in _sessions:
        _sessions[session_id] = []
    return SessionHistory(_sessions[session_id])


def get_truncation_stats(session_id: str) -> List[Dict]:
    """Get truncation statistics for a session"""
    return _truncation_stats.get(session_id, [])


def init_runnable(model_name: str) -> OpenAI:
    """Initialize OpenAI client"""
    client = OpenAI(
        api_key=API_KEY,
        base_url=BASE_URL, 
        timeout=120,
        max_retries=3,
    )
    # Store model name for later use
    client._model_name = model_name
    return client


def query_llm(client: OpenAI, prompt: str, session_id: str = "default", round_count: int = 0) -> str:
    """Query LLM with session history and truncation tracking"""
    # debug
    # client = init_runnable("claude-sonnet-4-20250514")
    # with open('/DBGAgent/log/dbg_log_astropy__astropy-12907.log', 'r') as f:
    #     prompt = f.read()
    # session_id = "default"
    # round_count = 0

    session = get_session_history(session_id)
    messages = session.messages
    
    # Initialize with system prompt if first message
    if not messages:
        messages.append({
            "role": "system",
            "content": dbg_system_prompt
        })
    
    # Add user message
    user_msg = {"role": "user", "content": prompt}
    messages.append(user_msg)
    

    try:
        # print(messages)
        response = client.chat.completions.create(
            model=client._model_name,
            messages=messages,
            temperature=0.7,
        )
        # print("\n"*5)
        # print(response)
        # Extract and validate response
        
        content = response.choices[0].message.content or ""
        input_tokens = response.usage.prompt_tokens
        output_tokens = response.usage.completion_tokens

        # Compute cost based on pricing
        pricing = _get_model_pricing(getattr(client, "_model_name", ""))
        # USD per token (prices are per 1,000,000 tokens)
        per_token_input = pricing["input"] / 1_000_000.0
        per_token_output = pricing["output"] / 1_000_000.0
        input_cost_usd = input_tokens * per_token_input
        output_cost_usd = output_tokens * per_token_output
        total_cost_usd = input_cost_usd + output_cost_usd

        # Clean the response for history storage and get truncation info
        cleaned_content, truncation_info = clean_response_for_history(content)
        
        # Log truncation information if any
        if truncation_info:
            log_truncation_info(truncation_info, session_id, round_count)
            
            # Store truncation stats for JSON export
            if session_id not in _truncation_stats:
                _truncation_stats[session_id] = []
            
            truncation_record = {
                "round": round_count,
                "original_function_calls": truncation_info.original_function_calls,
                "kept_function_calls": truncation_info.kept_function_calls,
                "removed_chars": truncation_info.removed_chars,
                "kept_function_summary": truncation_info.kept_function_summary,
                "removed_function_summaries": truncation_info.removed_function_summaries,
                "removed_content": truncation_info.removed_content
            }
            _truncation_stats[session_id].append(truncation_record)
        
        # Add cleaned response to history
        messages.append({
            "role": "assistant",
            "content": cleaned_content,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_cost_usd": total_cost_usd
        })
        
        # Return the original complete response for immediate use
        return content, total_cost_usd
        
    except Exception as e:
        # Remove failed user message
        if messages and messages[-1] == user_msg:
            messages.pop()
        raise


