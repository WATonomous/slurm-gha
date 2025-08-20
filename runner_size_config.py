import re


def get_runner_resources(runner_label):
    """
    Returns the resources required for a runner based on the runner label.
    CPU count and memory in GiB, tmpdisk in MiB.
    Based on https://github.com/WATonomous/infra-config/blob/b604376f4ee9fa3336b11dc084ba90b962ec7ee1/kubernetes/github-arc/get-config.py#L120-L142
    """
    TMPDISK_DEFAULT = 16 * 1045  # 16 GiB

    if runner_label == "slurm-runner-small":
        return {"cpu": 1, "mem-per-cpu": "2G", "tmpdisk": 4096, "time": "00:30:00"}
    elif runner_label == "slurm-runner-medium":
        return {
            "cpu": 2,
            "mem-per-cpu": "2G",
            "tmpdisk": TMPDISK_DEFAULT,
            "time": "00:30:00",
        }
    elif runner_label == "slurm-runner-large":
        return {
            "cpu": 4,
            "mem-per-cpu": "2G",
            "tmpdisk": TMPDISK_DEFAULT,
            "time": "00:30:00",
        }
    elif runner_label == "slurm-runner-xlarge":
        return {
            "cpu": 16,
            "mem-per-cpu": "2G",
            "tmpdisk": TMPDISK_DEFAULT,
            "time": "00:30:00",
        }
    elif runner_label == "slurm-runner-medium-long-running":
        return {
            "cpu": 4,
            "mem-per-cpu": "2G",
            "tmpdisk": TMPDISK_DEFAULT,
            "time": "06:00:00",
        }
    elif runner_label.startswith("slurm-runner-"):
        # expiremental custom sized runners
        # format the label as slurm-runner-4cpu-2mempercpu-30:00time-16tmpdisk
        # The above label would give: 4 CPUs, 2 GiB RAM / CPU, 30 minute time limit, 16 GiB tmpdisk
        try:
            pattern = re.compile(
                r"^slurm-runner-"
                r"(?P<cpu>\d+)cpu-"
                r"(?P<mem_per_cpu>\d+)mempercpu"
                r"(?:-(?P<time>[\d:]+)time)?"
                r"(?:-(?P<tmpdisk>\d+)tmpdisk)?$"
            )
            match = pattern.fullmatch(runner_label)
            if not match:
                raise ValueError("Runner label format did not match expected pattern.")

            cpu = int(match.group("cpu"))
            mem_per_cpu = int(match.group("mem_per_cpu"))
            time = match.group("time") or "30:00"
            tmpdisk = int(match.group("tmpdisk") or TMPDISK_DEFAULT)

            return {
                "cpu": cpu,
                "mem-per-cpu": f"{mem_per_cpu}G",
                "tmpdisk": tmpdisk * 1024,  # GiB to MiB
                "time": time,
            }

        except Exception as e:
            raise ValueError(
                f"Failed to parse runner label {runner_label}. Error: {e}"
            ) from e
        else:
            raise ValueError(f"Runner label {runner_label} not found.")
