import os

def get_runner_resources(runner_label):
    """
    Returns the resources required for a runner based on the runner label.
    CPU count and memory in GB, tmpdisk in bytes.
    Based on https://github.com/WATonomous/infra-config/blob/b604376f4ee9fa3336b11dc084ba90b962ec7ee1/kubernetes/github-arc/get-config.py#L120-L142
    """
    if runner_label == "slurm-runner-small":
        return {"cpu" : 1, "mem-per-cpu" : "2G", "tmpdisk" : 4096, "time" : "00:30:00"}
    elif runner_label == "slurm-runner-medium":
        return {"cpu" : 2, "mem-per-cpu" : "2G", "tmpdisk" : 16384, "time" : "00:30:00"}
    elif runner_label == "slurm-runner-large":
        return {"cpu" : 4, "mem-per-cpu" : "2G", "tmpdisk" : 16384, "time" : "00:30:00"}
    elif runner_label == "slurm-runner-xlarge":
        return {"cpu" : 16, "mem-per-cpu" : "2G", "tmpdisk" : 16384, "time" : "00:30:00"}
    elif runner_label == "slurm-runner-medium-long-running":
        return {"cpu" : 4, "mem-per-cpu" : "2G", "tmpdisk" : 16384, "time" : "06:00:00"}
    elif runner_label.startswith("slurm-runner-"):
        # format like slurm-runner-4cpu-2mempercpu-30:00time-16384tmpdisk
        # default values if not found
        parts = runner_label.split("-")
        resources = parts[2:] # remove slurm-runner-
        cpu, mem_per_cpu, time, tmpdisk = 2, 2, "30:00", 16384
        for part in resources:
            if part.endswith("mempercpu"):
                mem_per_cpu = int(part[:-9])
            elif part.endswith("cpu"):
                cpu = int(part[:-3])
            elif part.endswith("time"):
                time = part[:-4]
            elif part.endswith("tmpdisk"):
                tmpdisk = int(part[:-7])
            else:
                raise ValueError(f"Unknown resource type {part} in runner label {runner_label}.")
        return {"cpu" : cpu, "mem-per-cpu" : f"{mem_per_cpu}G", "tmpdisk" : tmpdisk, "time" : f"{time}"}
    else:
        raise ValueError(f"Runner label {runner_label} not found.")
