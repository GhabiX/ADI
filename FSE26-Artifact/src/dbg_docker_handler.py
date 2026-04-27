import sys
import json
import docker
import os
from docker.errors import ImageNotFound, APIError, DockerException
from typing import List, Dict, Optional, Union, Any
import concurrent.futures
import threading
from colorama import Fore, Style, init
from rich.console import Console
from rich.progress import Progress, TextColumn, BarColumn, TaskProgressColumn, TimeElapsedColumn, TimeRemainingColumn
from datetime import datetime

# Initialize colorama for cross-platform color support
init()
console = Console()
print_lock = threading.Lock()  # Global lock for thread-safe printing

EXEC_CMD = """

echo "Starting setup script..."
apt-get update && \
apt-get install -y python3.11-venv && \
python3.11 -m venv /dbg-venv && \
/dbg-venv/bin/python -m pip install --upgrade pip setuptools wheel && \
/dbg-venv/bin/pip install openai loguru


cp -r /DBGAgent/tools/dbgtool /dbgtool
source ~/.bashrc && python -m pip install -e /dbgtool

echo "Starting DBGAgent..."

source ~/.bashrc  && /dbg-venv/bin/python3 /DBGAgent/src/dbg_main.py --instance {instance_id} --model {model_name}

echo "DBGAgent completed."

"""


ContainerObject = docker.models.containers.Container

def run_command_in_container(
    client: docker.DockerClient,
    image_name: str,
    command: Optional[Union[str, List[str]]] = None, # Optional: uses image's default CMD if None
    container_name: Optional[str] = None,
    privileged: bool = False,
    network_mode: Optional[str] = None,
    volumes: Optional[Dict[str, Dict[str, str]]] = None,
    environment: Optional[Dict[str, str]] = None,
    ports: Optional[Dict[str, Optional[Union[int, str, List[Union[int, str]]]]]] = None,
    stdin_open: bool = False,
    tty: bool = False,
    remove: bool = True,
    detach: bool = False,
    pull_image_if_not_found: bool = True
) -> Union[bytes, ContainerObject, None]:
    """
    Pulls an image (if specified and not found), then creates and runs a container
    with the specified command and parameters.

    This function focuses on running a command in a new container from an image,
    similar to 'docker run'. If "build container" implies building an image from a
    Dockerfile, you would use client.images.build() separately before calling this function.

    Args:
        client: Initialized DockerClient instance.
        image_name: Name of the Docker image (e.g., 'ubuntu:latest').
        command: Command to run inside the container (string or list of strings).
                 If None, the image's default command (CMD) is used. (Default: None)
        container_name: Optional name for the container. If None, Docker generates a name. (Default: None)
        privileged: Run container in privileged mode. (Default: False)
        network_mode: Docker network mode (e.g., 'host', 'bridge'). (Default: None, uses Docker default)
        volumes: Volume mappings. Keys are host paths, values are dicts {'bind': container_path, 'mode': 'rw'/'ro'}.
                 Example: {'/host/path': {'bind': '/container/path', 'mode': 'rw'}} (Default: None)
        environment: Dictionary of environment variables to set in the container. Example: {'VAR1': 'value1'} (Default: None)
        ports: Port mappings. Example: {'2222/tcp': 3333} (container port: host port) (Default: None)
        stdin_open: Keep STDIN open even if not attached (corresponds to -i). (Default: False)
        tty: Allocate a pseudo-TTY (corresponds to -t). (Default: False)
        remove: Automatically remove the container when it exits. (Default: True)
        detach: Run container in the background. If True, returns a Container object.
                If False, waits for command to complete and returns logs. (Default: False)
        pull_image_if_not_found: If True, tries to pull the image if not found locally. (Default: True)

    Returns:
        - If detach is False: The combined stdout and stderr logs from the container as bytes.
        - If detach is True: The docker.models.containers.Container object.
        - None if a critical error occurs (e.g., image pull fails and not ignored, or API error).
    """
    try:
        # Pull image if needed
        if pull_image_if_not_found:
            try:
                client.images.get(image_name)
            except ImageNotFound:
                with print_lock:
                    console.print(f"[yellow]Pulling image: {image_name}[/yellow]")
                client.images.pull(image_name)

        # Configure run parameters
        run_kwargs: Dict[str, Any] = {
            'image': image_name,
            'command': command,
            'name': container_name,
            'privileged': privileged,
            'network_mode': network_mode,
            'volumes': volumes,
            'environment': environment,
            'ports': ports,
            'stdin_open': stdin_open,
            'tty': tty,
            'remove': remove,
            'detach': detach
        }
        
        # Run container
        if detach:
            container = client.containers.run(**run_kwargs)
            return container
        else:
            logs = client.containers.run(**run_kwargs)
            return logs

    except ImageNotFound:
        with print_lock:
            console.print(f"[red]Error: Image '{image_name}' not found[/red]")
        return None
    except APIError as e:
        with print_lock:
            console.print(f"[red]Docker API error: {e}[/red]")
            if container_name and "Conflict" in str(e) and "already in use" in str(e):
                console.print(f"[yellow]Note: Container name '{container_name}' may already exist[/yellow]")
        return None
    except Exception as e:
        with print_lock:
            console.print(f"[red]Unexpected error: {e}[/red]")
        return None

def process_instance(instance_id, model_name):
    """Process a single instance Docker container run"""
    instance_image_name = f'swebench/sweb.eval.x86_64.{instance_id}:latest'.replace('__', '_1776_')
    instance_container_name = f'dbg-{model_name}-{instance_id}'
    
    try:
        docker_client = docker.from_env(timeout=18000) 
        docker_client.ping() # Check connection
    except DockerException as e:
        with print_lock:
            console.print(f"[red]Error: Cannot connect to Docker daemon for {instance_id}. Details: {e}[/red]")
        return False

    # Setup container parameters
    is_privileged = True
    network = 'host'
    
    # Separate volume mounts for different access requirements
    # host_base_path = '..'
    host_base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    container_base_path = '/DBGAgent'
    
    volumes_map = {
        # Source code directory - read-only
        f'{host_base_path}/src': {
            'bind': f'{container_base_path}/src', 
            'mode': 'ro'
        },
        # Log directory - read-write  
        f'{host_base_path}/log': {
            'bind': f'{container_base_path}/log', 
            'mode': 'rw'
        },
        f'{host_base_path}/tools': {
            'bind': f'{container_base_path}/tools', 
            'mode': 'ro'
        },
        # Data directory - read-only (needed for SWE-bench.jsonl)
        f'{host_base_path}/data': {
            'bind': f'{container_base_path}/data', 
            'mode': 'ro'
        }
    }

    setup_cmd = EXEC_CMD.format(instance_id=instance_id, model_name=model_name)
    container_command = ['/bin/bash', '-c', setup_cmd]
    
    # Clean up existing container if any
    try:
        existing_container = docker_client.containers.get(instance_container_name)
        existing_container.remove(force=True)
    except docker.errors.NotFound:
        pass
    except APIError as e:
        with print_lock:
            console.print(f"[yellow]Warning: Error removing existing container {instance_id}: {e}[/yellow]")

    # Run container with command
    output_logs = run_command_in_container(
        client=docker_client,
        image_name=instance_image_name,
        command=container_command,
        container_name=instance_container_name,
        privileged=is_privileged,
        network_mode=network,
        volumes=volumes_map,
        stdin_open=True,
        tty=True,
        remove=True,
        detach=False
    )

    success = output_logs is not None
    
    # Save container output to log file instead of printing to terminal
    if output_logs:
        log_file_path = f'{host_base_path}/log/exec.log'
        try:
            output_str = output_logs.decode('utf-8') if isinstance(output_logs, bytes) else str(output_logs)
            
            # Create log directory if it doesn't exist
            os.makedirs(os.path.dirname(log_file_path), exist_ok=True)
            
            # Append output to log file with instance information
            with open(log_file_path, 'a', encoding='utf-8') as log_file:
                log_file.write(f"\n{'='*60}\n")
                log_file.write(f"Instance: {instance_id}\n")
                log_file.write(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                log_file.write(f"{'='*60}\n")
                log_file.write(output_str)
                log_file.write(f"\n{'='*60}\n\n")
            
            with print_lock:
                console.print(f"[green]Container output for {instance_id} saved to {log_file_path}[/green]")
                
        except Exception as e:
            with print_lock:
                console.print(f"[red]Error saving log for {instance_id}: {e}[/red]")
    else:
        with print_lock:
            console.print(f"[red]No output received from container {instance_id}[/red]")
    
    return success

def load_instances(json_path, filter_id=None, skip_existing_logs=False):
    """Load instance information from JSON file
    
    Args:
        json_path: Path to the JSON file containing instance list
        filter_id: Optional list of instance IDs to filter by
        skip_existing_logs: If True, skip instances that already have log files
        
    Returns:
        List of instance IDs to process, filtered based on the provided criteria
    """

    base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    with open(json_path, 'r') as f:
        instances = json.load(f)
    
    # Skip instances with existing log files if requested
    if skip_existing_logs:
        filtered_instances = []
        for instance in instances:
            log_filename = f"dbg_log_{instance}.json"
            log_path = f"{base_path}/log/{log_filename}"
            if not os.path.exists(log_path):
                filtered_instances.append(instance)
        instances = filtered_instances
    
    # Filter instances if filter_id list is provided
    if filter_id:
        instances = [instance for instance in instances if instance in filter_id]
    
    return instances
    

# --- Main execution logic ---
if __name__ == "__main__":
    model_name = "claude-3-7-sonnet-20250219"
    worker = 1
    base_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # instances = load_instances('../data/verified_500_instances.json', skip_existing_logs=True)
    instances = load_instances(f'{base_path}/data/verified_500_instances.json', ['astropy__astropy-12907'])
    console.print(f"[green]Starting processing of {len(instances)} instances with {worker} workers[/green]")
    
    # Track success and failures
    success_count = 0
    failed_instances = []
    
    # Use rich Progress with better configuration
    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
        expand=True,
        refresh_per_second=1
    ) as progress:
        task = progress.add_task("[cyan]Processing instances...", total=len(instances))
        
        # Process instances with thread pool
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker) as executor:
            futures = {executor.submit(process_instance, instance, model_name): instance
                    for instance in instances}
            
            for future in concurrent.futures.as_completed(futures):
                instance_id = futures[future]
                try:
                    success = future.result()
                    if success:
                        success_count += 1
                        # Only update progress, minimize console output during processing
                    else:
                        failed_instances.append(instance_id)
                except Exception as e:
                    failed_instances.append(instance_id)
                    with print_lock:
                        console.print(f"[red]Exception in {instance_id}: {e}[/red]")
                finally:
                    progress.update(task, advance=1)
    
    # Print detailed results after progress bar is complete
    console.print(f"\n[green]All processing completed[/green]")
    console.print("[cyan]Summary:[/cyan]")
    console.print(f"  - Total: {len(instances)}")
    console.print(f"  - Success: {success_count}")
    console.print(f"  - Failed: {len(failed_instances)}")
    
    # Show success details
    if success_count > 0:
        console.print(f"[green]Successfully completed {success_count} instances[/green]")
    
    # Show failure details
    if failed_instances:
        console.print("[red]Failed instances:[/red]")
        for instance in failed_instances:
            console.print(f"  - [red]{instance}[/red]")