# run-gha-on-slurm

This is still a work in progress. The goal is to run GitHub Actions on the Slurm cluster.

# How it works
1. Polls the GitHub API for queued jobs
2. Whenever a job is queued, it allocates an ephemeral action runner on the Slurm cluster
3. Once the job is complete, the runner and Slurm resources are de-allocated

```mermaid
flowchart LR
    GitHubAPI[("GitHub API")]
    ActionsRunners[("Actions Runners")]
    Slurm[("Slurm Compute Resources")]

    ActionsRunners --> | Poll Queued Jobs | GitHubAPI 
    ActionsRunners -->| Allocate Cached Runner| Slurm 
```

# Notes
Custom image: https://github.com/WATonomous/actions-runner-image

# TODO
- Use docker and custom image for actions runner [done]
- Need to track if the runner for a job has been allocated [done]
- Add logic to check if script succeeded or failed [done]
	- remove job id from allocated jobs
- Set up custom cpu/mem for different sized jobs [done]
	- What should these be based off of? https://github.com/WATonomous/infra-config/blob/b604376f4ee9fa3336b11dc084ba90b962ec7ee1/kubernetes/github-arc/get-config.py#L120-L142 
- Look into removing the building step [done]
- Keep data structure of running jobs, and when the are completed report status on commandline [done]
- docker in docker 
	- https://slurm.schedmd.com/scrun.html
	- https://jpetazzo.github.io/2015/09/03/do-not-use-docker-in-docker-for-ci/
	- https://docs.docker.com/engine/security/rootless/#rootless-docker-in-docker
- Add function to stop all sbatch running jobs on startup in case of restart
- Use secrets for the token
- Get access token
- Use for more repos
	- modify to get the repo name dynamically
- Look into security of passing tokens to scripts
- Testing

# TODO After MVP
- Make sure the image is cached 

# Issues
- If script needs to be restart and runners are being built, the script will allocate new runners once its back up 

# Potential issue:
- job1 requires label1, label2
- job2 requires label1
- runner1 is allocated with label1, label2
- runner1 runs job2
- runner2 is allocated with label1
- runner2 CANT RUN job1
Won't be an issue if we use one label (small, medium, large) per job

