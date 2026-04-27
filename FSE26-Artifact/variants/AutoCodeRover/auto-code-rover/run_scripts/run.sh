#!/bin/bash

DATE=$(date '+%Y-%m-%d_%H:%M:%S')

cd /swe-bench/swe-final/auto-code-rover

source ~/miniconda3/bin/activate

conda activate auto-code-rover

export OPENAI_KEY=""
export OPENAI_BASE_URL=
MODEL="claude-3-7-sonnet-20250219"

OUTPUT="exp/Verified_${MODEL}_${DATE}"

export LOG_LEVEL=info
export LOG_DIR='logs'
export APP_NAME='swe'

PYTHONPATH=. python app/main.py swe-bench \
    --model $MODEL \
    --setup-map /swe-bench/SWE-bench/setup_result/setup_map.json \
    --tasks-map /swe-bench/SWE-bench/setup_result/tasks_map.json \
    --output-dir $OUTPUT \
    --reproduce-and-review \
    --task-list-file "/swe-bench/swe-final/auto-code-rover/data/swe_verified_tasks.txt" \
    --no-print