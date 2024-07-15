import os

from config import LIST_OF_RUNNER_LABELS, ALLOCATE_RUNNER_SCRIPT_PATH 


def get_runner_resources(runner_label):
    """
    Returns the resources required for a runner based on the runner label.
    CPU count and memory in GB, tmpdisk in bytes.
    Based on https://github.com/WATonomous/infra-config/blob/b604376f4ee9fa3336b11dc084ba90b962ec7ee1/kubernetes/github-arc/get-config.py#L120-L142
    """
    if runner_label == "gh-arc-runners-small":
        return {"cpu" : 1, "memory" : 2, "tmpdisk" : 4096, "time" : "00:15:00"}
    elif runner_label == "gh-arc-runners-medium":
        return {"cpu" : 2, "memory" : 4, "tmpdisk" : 16384, "time" : "00:20:00"}
    elif runner_label == "gh-arc-runners-large":
        return {"cpu" : 4, "memory" : 8, "tmpdisk" : 16384, "time" : "00:25:00"}
    elif runner_label == "gh-arc-runners-xlarge":
        return {"cpu" : 8, "memory" : 16, "tmpdisk" : 16384, "time" : "00:30:00"}
    else:
        raise ValueError(f"Runner label {runner_label} not found.")

def get_runner_sbatch_config(runner_label):
    """
    Returns the sbatch configuration for a runner based on the runner label.
    """
    
    # get resources 
    resources = get_runner_resources(runner_label)
    
    # get sbatch config
    sbatch_config = f"""
    #!/bin/bash
    #SBATCH --job-name=slurm-gh-actions-runner
    #SBATCH --cpus-per-task={resources["cpu"]}
    #SBATCH --mem={resources["memory"]}G
    #SBATCH --gres tmpdisk:{resources["tmpdisk"]}
    #SBATCH --time=00:30:00
    """
     
    return sbatch_config.replace("    ", "").strip()

def create_runner_sbatch_file(runner_label):
    """
    Creates a sbatch file for a runner based on the runner label.
    """
    
    # get sbatch config
    sbatch_config = get_runner_sbatch_config(runner_label)
    
    # ensure directory exists
    os.makedirs("slurm-runner-scripts", exist_ok=True)
    # write to file
    with open(f"slurm-runner-scripts/runner-{runner_label}.sh", "w") as f:
        f.write(sbatch_config)
        f.write("\n")
        f.write("# The above sbatch configuration is generated dynamically based on the runner label by runner_size_config.py\n")
        open(ALLOCATE_RUNNER_SCRIPT_PATH, "r").seek(0)
        f.write(open(ALLOCATE_RUNNER_SCRIPT_PATH, "r").read())
    
def create_runner_sbatch_files():
    for runner_label in LIST_OF_RUNNER_LABELS:
        create_runner_sbatch_file(runner_label)