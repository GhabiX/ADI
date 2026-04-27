#!/usr/bin/env python3
"""
Run mini-SWE-agent on a custom list of SWE-bench instances.
"""

import concurrent.futures
import json
import traceback
import time
from pathlib import Path
from typing import List

import yaml
from datasets import load_dataset
from rich.live import Live
from rich.console import Console

# 导入 mini-swe-agent 的核心库
from minisweagent.agents.default import DefaultAgent
from minisweagent.config import get_config_path
from minisweagent.models import get_model
from minisweagent.run.extra.swebench import (
    DATASET_MAPPING, 
    get_sb_environment,
    ProgressTrackingAgent,
    update_preds_file,
    remove_from_preds_file
)
from minisweagent.run.extra.utils.batch_progress import RunBatchProgressManager
from minisweagent.run.utils.save import save_traj
from minisweagent.utils.log import add_file_handler, logger

console = Console()

def process_instance(
    instance: dict,
    output_dir: Path,
    config: dict,
    progress_manager: RunBatchProgressManager,
) -> None:
    """处理单个 SWE-bench 实例，复制自原始代码"""
    instance_id = instance["instance_id"]
    instance_dir = output_dir / instance_id
    
    # 清理之前的文件避免不一致状态
    remove_from_preds_file(output_dir / "preds.json", instance_id)
    (instance_dir / f"{instance_id}.traj.json").unlink(missing_ok=True)
    
    model = get_model(config=config.get("model", {}))
    task = instance["problem_statement"]

    progress_manager.on_instance_start(instance_id)
    progress_manager.update_instance_status(instance_id, "Pulling/starting docker")

    agent = None
    extra_info = None

    try:
        env = get_sb_environment(config, instance)
        agent = ProgressTrackingAgent(
            model,
            env,
            progress_manager=progress_manager,
            instance_id=instance_id,
            **config.get("agent", {}),
        )
        exit_status, result = agent.run(task)
    except Exception as e:
        logger.error(f"Error processing instance {instance_id}: {e}", exc_info=True)
        exit_status, result = type(e).__name__, str(e)
        extra_info = {"traceback": traceback.format_exc()}
    finally:
        save_traj(
            agent,
            instance_dir / f"{instance_id}.traj.json",
            exit_status=exit_status,
            result=result,
            extra_info=extra_info,
            instance_id=instance_id,
            print_fct=logger.info,
        )
        update_preds_file(output_dir / "preds.json", instance_id, model.config.model_name, result)
        progress_manager.on_instance_end(instance_id, exit_status)


def run_custom_instances(
    instance_ids: List[str],
    config_path: str = "./custom_swebench_config.yaml",
    output_dir: str = "./results_custom",
    workers: int = 4,
    subset: str = "verified",
    split: str = "test"
):
    """
    运行自定义的 instance 列表
    
    Args:
        instance_ids: 要运行的实例 ID 列表
        config_path: 配置文件路径
        output_dir: 输出目录
        workers: 并行工作线程数
        subset: SWE-bench 子集
        split: 数据集分割
    """
    
    # 创建输出目录
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    # 设置日志
    add_file_handler(output_path / "minisweagent.log")
    logger.info(f"Results will be saved to {output_path}")
    logger.info(f"Running {len(instance_ids)} custom instances...")
    
    # 加载数据集
    dataset_path = DATASET_MAPPING.get(subset, subset)
    logger.info(f"Loading dataset {dataset_path}, split {split}...")
    
    # 创建实例ID到实例的映射
    all_instances = {
        inst["instance_id"]: inst 
        for inst in load_dataset(dataset_path, split=split)
    }
    
    # 过滤出我们要的实例
    instances = []
    missing_instances = []
    for instance_id in instance_ids:
        if instance_id in all_instances:
            instances.append(all_instances[instance_id])
        else:
            missing_instances.append(instance_id)
    
    if missing_instances:
        logger.warning(f"Missing instances: {missing_instances}")
    
    logger.info(f"Found {len(instances)} instances to run")
    
    # 加载配置
    config = yaml.safe_load(Path(config_path).read_text())
    
    # 创建进度管理器
    progress_manager = RunBatchProgressManager(
        len(instances), 
        output_path / f"exit_statuses_{time.time()}.yaml"
    )
    
    def process_futures(futures: dict[concurrent.futures.Future, str]):
        for future in concurrent.futures.as_completed(futures):
            try:
                future.result()
            except concurrent.futures.CancelledError:
                pass
            except Exception as e:
                instance_id = futures[future]
                logger.error(f"Error in future for instance {instance_id}: {e}", exc_info=True)
                progress_manager.on_uncaught_exception(instance_id, e)

    # 运行实例
    with Live(progress_manager.render_group, refresh_per_second=4):
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(process_instance, instance, output_path, config, progress_manager): instance["instance_id"]
                for instance in instances
            }
            try:
                process_futures(futures)
            except KeyboardInterrupt:
                logger.info("Cancelling all pending jobs. Press ^C again to exit immediately.")
                for future in futures:
                    if not future.running() and not future.done():
                        future.cancel()
                process_futures(futures)
    
    console.print(f"[green]✅ Completed! Results saved to {output_path}[/green]")


if __name__ == "__main__":
    # 示例使用方式
    
    # 方式 1: 直接定义实例列表
    custom_instances = [
        "astropy__astropy-12907",
        "astropy__astropy-13033", 
        "astropy__astropy-13236",
        "django__django-11099",
        "django__django-11133",
        "sympy__sympy-15599",
        # 添加更多实例...
    ]
    
    # 方式 2: 从文件加载实例列表
    # with open("my_instance_list.json", "r") as f:
    #     custom_instances = json.load(f)
    
    # 方式 3: 从 CSV 或文本文件加载
    # with open("my_instance_list.txt", "r") as f:
    #     custom_instances = [line.strip() for line in f if line.strip()]
    
    print(f"准备运行 {len(custom_instances)} 个实例:")
    for i, instance_id in enumerate(custom_instances[:10]):  # 显示前10个
        print(f"  {i+1}. {instance_id}")
    if len(custom_instances) > 10:
        print(f"  ... 还有 {len(custom_instances) - 10} 个实例")
    
    # 运行实例
    run_custom_instances(
        instance_ids=custom_instances,
        config_path="./custom_swebench_config.yaml",  # 使用你的自定义配置
        output_dir="./results_custom_50",
        workers=4,  # 根据你的机器调整
        subset="verified",
        split="test"
    ) 