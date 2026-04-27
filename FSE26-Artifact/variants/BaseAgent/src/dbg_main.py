import os
import json
import argparse


from dbg_prompt import dbg_user_prompt

from dbg_utils import extract_code_blocks, init_logger, log_info, log_error, get_instance_info
from dbg_api import handle_api_call

from dbg_query_llm import query_llm, init_runnable, get_session_history, get_truncation_stats


def dbg_main_loop(client, prompt_str, session_id):
    round_count = 0
    task_report = None
    dbg_cmd_result = prompt_str
    warning_bar = 5
    round_limit = 65
    # round_limit = 50
    timeout_retry_count = 0  # Add timeout retry counter
    max_timeout_retries = 3  # Maximum timeout retry attempts
    
    cost_limit = 4  # 4 USD
    curr_cost = 0

    while round_count < round_limit and curr_cost < cost_limit:
        try:
            log_info('='*100)
            log_info(f"[API Result]:\n{dbg_cmd_result}")

            response, cost_usd = query_llm(client, dbg_cmd_result, session_id=session_id, round_count=round_count)
            curr_cost += cost_usd
            log_info(f"[LLM Response]:\n{response}")
            code_blocks = extract_code_blocks(response)
            
            if not code_blocks:
                log_error("[Exception]: No API found in response")
                dbg_cmd_result = "Error: Use the provided APIs (format: <function_call>...</function_call>) to complete the task independently."
                round_count += 1
                continue

            dbg_cmd = code_blocks[0]
            dbg_cmd_result = handle_api_call(dbg_cmd)

            if len(dbg_cmd_result) > 20000:
                dbg_cmd_result = (
                    dbg_cmd_result[:20000]
                    + f"\n...[TRUNCATED]\n[Warning]: Output exceeded 30,000 characters (original length={len(dbg_cmd_result)}). Only the first 30,000 characters are shown."
                )


            # Check if this is a task_report result (XML format)
            if "<function>task_report</function>" in dbg_cmd.strip():
                task_report = json.loads(dbg_cmd_result)
                log_info(f"[Task Report Received]: Task completed with success={task_report.get('task_resolve_success', 'False')}")
                break

            if len(code_blocks) > 1:
                dbg_cmd_result += '\n[Warning]: Only **ONE** API call is permitted & executed per turn.'

            # Reset timeout retry counter after successful execution
            timeout_retry_count = 0
            
            round_count += 1
            if round_count > round_limit - warning_bar:
                dbg_cmd_result += f'\n[Warning]: Only {round_limit-round_count} turns left. Please ready to submit task_report'
            
            if curr_cost > cost_limit - 0.5:
                dbg_cmd_result += f'\n[Warning]: The cost is approaching the limit with only {cost_limit - curr_cost:.2f} USD left. Please ready to submit task_report'
            
        except Exception as e:
            error_msg = str(e)
            log_error(f"[Exception]: {error_msg}")
            
            # Check if this is a timeout error
            if "Request timed out" in error_msg:
                timeout_retry_count += 1
                if timeout_retry_count <= max_timeout_retries:
                    log_info(f"[Timeout Retry]: Attempt {timeout_retry_count}/{max_timeout_retries}, continuing...")
                    dbg_cmd_result = f"Error: Request timed out (retry {timeout_retry_count}/{max_timeout_retries}). Please try again."
                    continue
                else:
                    log_error(f"[Timeout Limit Exceeded]: Maximum timeout retries ({max_timeout_retries}) reached, stopping.")
                    break
            else:
                # Other types of exceptions still break directly
                break

    return task_report


if __name__ == "__main__":

    parser = argparse.ArgumentParser(description='DBG Agent Runner')
    parser.add_argument('--model', type=str, default="gpt-4o-2024-08-06",
                      help='Model name to use (default: gpt-4o-2024-08-06)')
    parser.add_argument('--instance', type=str, required=True,
                      help='Instance ID to process (e.g., astropy__astropy-12907)')
    
    args = parser.parse_args()
    model_name = args.model
    instance_id = args.instance

    instance_bug_report= get_instance_info(instance_id)
    
    dbg_base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    log_path = f'{dbg_base_dir}/log/dbg_log_{instance_id}.log'

    init_logger(log_path)
    
    client = init_runnable(model_name)
    prompt = dbg_user_prompt.format(issue_description=instance_bug_report)
    prompt_str = prompt.replace("{", "{{").replace("}", "}}")

    session_id = "dbg_session"
    
    task_report = dbg_main_loop(client, prompt_str, session_id)
    
    # Save conversation history
    history = get_session_history(session_id)
    
    # Get truncation statistics
    truncation_stats = get_truncation_stats(session_id)
    
    # Create output dictionary with history, task_report, and truncation stats
    output_data = {
        "history": json.loads(history.model_dump_json()),
        "task_report": task_report,
        "truncation_stats": {
            "total_truncations": len(truncation_stats),
            "truncation_details": truncation_stats
        }
    }
    
    # Save combined data to JSON file
    with open(f'{dbg_base_dir}/log/dbg_log_{instance_id}.json', 'w', encoding='utf-8') as f:
        json.dump(output_data, indent=2, ensure_ascii=False, fp=f)

    # Log final truncation summary
    if truncation_stats:
        total_removed_chars = sum(stat["removed_chars"] for stat in truncation_stats)
        log_info(f"[TRUNCATION SUMMARY] Total: {len(truncation_stats)} truncations, "
                 f"removed {total_removed_chars} characters total")
    else:
        log_info("[TRUNCATION SUMMARY] No truncations occurred during this session")

        