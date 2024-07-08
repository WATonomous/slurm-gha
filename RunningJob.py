from typing import List

class RunningJob:
    def __init__(self, job_id: int, slurm_job_id: int, workflow_name: str, job_name: str, labels: List[str]):
        """Class to represent a running Github Actions Job on Slurm."""
        self.job_id = job_id
        self.slurm_job_id = slurm_job_id
        self.workflow_name = workflow_name
        self.job_name = job_name
        self.labels = labels

    def __str__(self) -> str:
        return (f"RunningJob(job_id = {self.job_id}, slurm_job_id = {self.slurm_job_id}, "
                f"workflow_name = {self.workflow_name}, "
                f"job_name = {self.job_name}, labels = {self.labels})")
    
    def __repr__(self) -> str:
        return self.__str__()