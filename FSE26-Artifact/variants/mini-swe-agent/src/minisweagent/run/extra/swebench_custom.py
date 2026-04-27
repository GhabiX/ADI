"""
自定义SWE-bench运行脚本，支持挂载本地Python包
基于原始swebench.py修改
"""
import concurrent.futures
import json
import logging
import time
import traceback
from pathlib import Path

import typer
import yaml
from datasets import load_dataset
from rich.live import Live

from minisweagent.agents.default import DefaultAgent
from minisweagent.config import builtin_config_dir, get_config_path
from minisweagent.environments import get_environment
from minisweagent.models import get_model
from minisweagent.run.extra.swebench import (
    DATASET_MAPPING, 
    ProgressTrackingAgent, 
    RunBatchProgressManager,
    filter_instances,
    get_swebench_docker_image_name,
    update_preds_file,
    remove_from_preds_file
)
from minisweagent.run.utils import add_file_handler, save_traj

logger = logging.getLogger("minisweagent")

app = typer.Typer()


def get_custom_sb_environment(config: dict, instance: dict, local_package_path: str = "") -> any:
    """获取自定义的SWE-bench环境，支持挂载本地Python包"""
    image_name = get_swebench_docker_image_name(instance)
    env_config = config.setdefault("environment", {})
    
    if env_config.get("environment_class") == "singularity":
        image_name = "docker://" + image_name
    
    env_config["image"] = image_name
    
    # 如果指定了本地包路径，使用自定义Docker环境
    if local_package_path:
        env_config["environment_class"] = "minisweagent.environments.custom_docker.CustomDockerEnvironment"
        env_config["local_package_path"] = local_package_path
        env_config["install_package"] = True
    
    return get_environment(env_config, default_type="docker")


def process_custom_instance(
    instance: dict,
    output_dir: Path,
    config: dict,
    progress_manager: RunBatchProgressManager,
    local_package_path: str = "",
) -> None:
    """处理单个SWE-bench实例，支持自定义Python包"""
    instance_id = instance["instance_id"]
    instance_dir = output_dir / instance_id
    remove_from_preds_file(output_dir / "preds.json", instance_id)
    (instance_dir / f"{instance_id}.traj.json").unlink(missing_ok=True)
    model = get_model(config=config.get("model", {}))
    task = instance["problem_statement"]

    progress_manager.on_instance_start(instance_id)
    progress_manager.update_instance_status(instance_id, "Pulling/starting docker")

    agent = None
    extra_info = None

    try:
        env = get_custom_sb_environment(config, instance, local_package_path)
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


@app.command()
def main(
    subset: str = typer.Option("lite", "--subset", help="SWEBench subset to use or path to a dataset"),
    split: str = typer.Option("dev", "--split", help="Dataset split"),
    slice_spec: str = typer.Option("", "--slice", help="Slice specification (e.g., '0:5' for first 5 instances)"),
    filter_spec: str = typer.Option("", "--filter", help="Filter instance IDs by regex"),
    shuffle: bool = typer.Option(False, "--shuffle", help="Shuffle instances"),
    output: str = typer.Option("", "-o", "--output", help="Output directory"),
    workers: int = typer.Option(1, "-w", "--workers", help="Number of worker threads for parallel processing"),
    model: str | None = typer.Option(None, "-m", "--model", help="Model to use"),
    model_class: str | None = typer.Option(None, "-c", "--model-class", help="Model class to use"),
    redo_existing: bool = typer.Option(False, "--redo-existing", help="Redo existing instances"),
    config_spec: Path = typer.Option(builtin_config_dir / "extra" / "swebench.yaml", "-c", "--config", help="Path to a config file"),
    environment_class: str | None = typer.Option(None, "--environment-class", help="Environment type to use"),
    local_package_path: str = typer.Option("", "--local-package", help="本地Python包路径，将挂载到容器中并安装"),
) -> None:
    """运行SWE-bench实例，支持挂载本地Python包"""
    
    if local_package_path and not Path(local_package_path).exists():
        typer.echo(f"错误：指定的本地包路径不存在: {local_package_path}")
        raise typer.Exit(1)
    
    output_path = Path(output)
    output_path.mkdir(parents=True, exist_ok=True)
    logger.info(f"Results will be saved to {output_path}")
    add_file_handler(output_path / "minisweagent.log")

    dataset_path = DATASET_MAPPING.get(subset, subset)
    logger.info(f"Loading dataset {dataset_path}, split {split}...")
    instances = list(load_dataset(dataset_path, split=split))

    instances = filter_instances(instances, filter_spec=filter_spec, slice_spec=slice_spec, shuffle=shuffle)
    if not redo_existing and (output_path / "preds.json").exists():
        existing_instances = list(json.loads((output_path / "preds.json").read_text()).keys())
        logger.info(f"Skipping {len(existing_instances)} existing instances")
        instances = [instance for instance in instances if instance["instance_id"] not in existing_instances]
    logger.info(f"Running on {len(instances)} instances...")

    if local_package_path:
        logger.info(f"Will mount local package from: {local_package_path}")

    config = yaml.safe_load(get_config_path(config_spec).read_text())
    if environment_class is not None:
        config.setdefault("environment", {})["environment_class"] = environment_class
    if model is not None:
        config.setdefault("model", {})["model_name"] = model
    if model_class is not None:
        config.setdefault("model", {})["model_class"] = model_class

    progress_manager = RunBatchProgressManager(len(instances), output_path / f"exit_statuses_{time.time()}.yaml")

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

    with Live(progress_manager.render_group, refresh_per_second=4):
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for instance in instances:
                future = executor.submit(
                    process_custom_instance, 
                    instance, 
                    output_path, 
                    config, 
                    progress_manager,
                    local_package_path
                )
                futures[future] = instance["instance_id"]
            process_futures(futures)


if __name__ == "__main__":
    app() 