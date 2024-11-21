import os

def get_runner_resources(runner_label):
    """
    Returns the resources required for a runner based on the runner label.
    CPU count and memory in GB, tmpdisk in bytes.
    Based on https://github.com/WATonomous/infra-config/blob/b604376f4ee9fa3336b11dc084ba90b962ec7ee1/kubernetes/github-arc/get-config.py#L120-L142
    """
    if runner_label == "gh-arc-runners-small":
        return {"cpu" : 1, "mem-per-cpu" : "2G", "tmpdisk" : 4096, "time" : "00:15:00"}
    elif runner_label == "gh-arc-runners-medium":
        return {"cpu" : 2, "mem-per-cpu" : "2G", "tmpdisk" : 16384, "time" : "00:20:00"}
    elif runner_label == "gh-arc-runners-large":
        return {"cpu" : 4, "mem-per-cpu" : "2G", "tmpdisk" : 16384, "time" : "00:25:00"}
    elif runner_label == "gh-arc-runners-xlarge":
        return {"cpu" : 16, "mem-per-cpu" : "2G", "tmpdisk" : 16384, "time" : "00:30:00"}
    else:
        raise ValueError(f"Runner label {runner_label} not found.")