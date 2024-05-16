def get_runner_resources(runner_label):
    """
    Returns the resources required for a runner based on the runner label.
    CPU count and memory in GB.
    Based on https://github.com/WATonomous/infra-config/blob/b604376f4ee9fa3336b11dc084ba90b962ec7ee1/kubernetes/github-arc/get-config.py#L120-L142
    """
    if runner_label == "gh-arc-runners-small":
        return {"cpu" : 1, "memory" : 2}
    elif runner_label == "gh-arc-runners-medium":
        return {"cpu" : 2, "memory" : 4}
    elif runner_label == "gh-arc-runners-large":
        return {"cpu" : 4, "memory" : 8}
    elif runner_label == "gh-arc-runners-xlarge":
        return {"cpu" : 8, "memory" : 16}
    else:
        raise ValueError(f"Runner label {runner_label} not found.")