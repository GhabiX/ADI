import json
import os
from pathlib import Path
from typing import Optional, Dict, Any
import argparse


def extract_patch_info(json_file: str) -> Optional[Dict[str, Any]]:
    """
    Extract patch information from a JSON log file.
    
    Args:
        json_file: Path to the JSON log file
        
    Returns:
        Dictionary containing instance_id, model_name and patch info, or None if extraction fails
    """
    try:
        with open(json_file) as f:
            data = json.load(f)


        task_report = data.get('task_report')
        if task_report is None:
            return None
        # Extract diff info from task report
        diff_info = task_report.get('task_modify_files_diff', {})
        if not diff_info:
            return None
            
        valid_diffs = {k: v for k, v in diff_info.items() if v != "No changes detected by git"}
        if not valid_diffs:
            return None
            
        # Remove 'dbg_log_' prefix from instance_id
        instance_id = Path(json_file).stem
        if instance_id.startswith('dbg_log_'):
            instance_id = instance_id[8:]  # Remove 'dbg_log_' prefix
        combined_patch = "\n".join(valid_diffs.values())
        return {
            "instance_id": instance_id,
            "model_name_or_path": "dbg_agent",
            "model_patch": combined_patch,
            "task_analysis": task_report.get('task_analysis', ""),
            "task_resolve_success": task_report.get('task_resolve_success', False)
        }
    except Exception as e:
        print(f"Error processing {json_file}: {e}")
        return None


def generate_patches_jsonl(log_dir: str, output_file: str) -> None:
    """
    Process all JSON log files and generate a JSONL file with patch information.
    
    Args:
        log_dir: Directory containing JSON log files
        output_file: Path to output JSONL file
    """
    # Create output directory if needed
    Path(output_file).parent.mkdir(parents=True, exist_ok=True)
    
    # Process all JSON files and write results
    with open(output_file, 'w') as f:
        for json_file in Path(log_dir).glob("*.json"):
            if patch_info := extract_patch_info(str(json_file)):
                f.write(f"{json.dumps(patch_info)}\n")


def main():
    """Main function to process log files and generate patch information."""
    # Parse command-line argument for experiment directory
    parser = argparse.ArgumentParser(description="Generate JSONL patches from experiment logs")
    parser.add_argument("exp_dir", help="Path to the experiment directory (containing 'out' and where output JSONL will be saved)")
    args = parser.parse_args()

    exp_dir = args.exp_dir
    result_dir =  f"{exp_dir}/out"
    output_file = f"{exp_dir}/dbg_patches.jsonl"
    
    generate_patches_jsonl(result_dir, output_file)
    print(f"Processing complete. Output saved to: {output_file}")


if __name__ == "__main__":
    main()
