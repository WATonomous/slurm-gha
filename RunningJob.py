from typing import List, Optional
from datetime import datetime

class RunningJob:
    def __init__(
        self, 
        job_id: int, 
        slurm_job_id: Optional[int], 
        workflow_name: str, 
        job_name: str, 
        labels: List[str],
        start_time: Optional[datetime] = None
    ):
        """
        Class to represent a running GitHub Actions Job on Slurm.
        
        :param job_id: The GitHub Actions job ID.
        :param slurm_job_id: The corresponding SLURM job ID (if allocated).
        :param workflow_name: Name of the workflow that launched this job.
        :param job_name: Name of this specific job.
        :param labels: Labels associated with the job (e.g. 'slurm-runner-medium').
        :param start_time: When the job began, if known. Defaults to None.
        """
        self.job_id = job_id
        self.slurm_job_id = slurm_job_id
        self.workflow_name = workflow_name
        self.job_name = job_name
        self.labels = labels
        self.start_time = start_time

    def __str__(self) -> str:
        return (f"RunningJob(job_id={self.job_id}, slurm_job_id={self.slurm_job_id}, "
                f"workflow_name={self.workflow_name}, job_name={self.job_name}, "
                f"labels={self.labels}, start_time={self.start_time})")

    def __repr__(self) -> str:
        return self.__str__()
