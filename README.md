# run-gha-on-slurm

The purpose of this project is to run GitHub Actions on prem via our Slurm cluster.

# Overview
1. The Allocator polls the GitHub API for queued jobs
2. Whenever a job is queued, it allocates an ephemeral action runner on the Slurm cluster
3. Once the job is complete, the runner and Slurm resources are de-allocated

### Basic diagram of the system
```mermaid
flowchart LR
    GitHubAPI[("GitHub API")]
    ActionsRunners[("Allocator")]
    Slurm[("Slurm Compute Resources")]

    ActionsRunners --> | Poll Queued Jobs | GitHubAPI 
    ActionsRunners -->| Allocate Actions Runner| Slurm 
```

## Enabling Docker Within CI Jobs

Our CI job runner is a container, [actions-runner-image](https://github.com/WATonomous/actions-runner-image), and we are running Docker containers in our jobs, we need to run Docker within Docker.


```mermaid
graph TD
    A[Docker Rootless Daemon] -->|Creates| B[Docker Rootless Socket]
    C[Actions Runner Image] -->|Mounts| B
    C -->|Creates| E[CI Helper Containers]
    E -->|Mounts| B
```

Since CI Docker commands will use the same filesystem, as they have the same Docker socket, we must configure the working directory of your runners accordingly. Normally it would be a security risk to mount the Docker socket into a container, but since we are using [Docker Rootless](https://docs.docker.com/engine/security/rootless/), we are able to mitigate this risk.

## Speeding up the start time for our Actions Runner Image

After we were able to run the actions runner image in as Slurm job using [sbatch](https://slurm.schedmd.com/sbatch.html) and custom script we ran into the issue of having to pull the actions runner image for every job. From the time the script allocated resources to the time the job began was ~ 2 minutes. When you are running 70+ jobs in a workflow, with jobs depending on others, this time adds up quickly.

Unfortunately, caching the image in our filesystem was not an elegant solution because this would require mounting the filesystem directory to the Slurm job. This means we would need to have multiple directories if we wanted to support multiple concurrent runners. This would require creating a system to manage these directories and would introduce problems such as starvation and dead locks. 

This led us to investigate several options:
- [Docker pull through cache](https://docs.docker.com/docker-hub/mirror/)
- [Stargz Snapshotter](https://github.com/containerd/stargz-snapshotter)
- [Apptainer](https://apptainer.org/docs/user/main/index.html)

We decided to go with Apptainer as it was the most straightforward solution. Apptainer is a tool that allows you to create a snapshot of a container image and store it in a tarball. This tarball can be loaded into the containerd runtime and used as a cache. This allows us to pull the image once (per machine) and use it for all subsequent jobs. 

## CVMFS for caching our Provisioner image

For many of our jobs we use a container we call the provisioner. This container is a tool that is used to provision a service. This container is large, over 1.5GB, and needs to be rebuilt with every CI run. This new container is then used to run the subsequent dependent jobs. 

### Previous Solution
Previously after the provisioner was built, we would cache in S3, via [rgw](https://docs.ceph.com/en/latest/man/8/radosgw/), within our Kubernetes cluster. This worked well as we previously ran [actions-runner-controller](https://github.com/actions/actions-runner-controller) within the same cluster. Previously, it would only take 1 hop when there was a cache miss. Now that we are running the actions runner on our Slurm cluster, it would take at least 1 hop, 2 when there was a cache miss. 

```mermaid
flowchart TD
    SLURM[SLURM Job Node] -->|1 hop| Kubernetes[Kubernetes Node]
    Kubernetes -->|1 hop 0 if cached| RGW[RGW Container Local]
```

## Deployment to Kubernetes

## Next Steps

### References
1. [Docker Rootless](https://docs.docker.com/engine/security/rootless/)
2. [Custom Actions Runner Image](https://github.com/WATonomous/actions-runner-image)
3. [Apptainer](https://apptainer.org/docs/user/main/index.html)
4. [Stargz Snapshotter](https://github.com/containerd/stargz-snapshotter)
5. [CVMFS](https://cvmfs.readthedocs.io/en/stable/)
6. [CVMFS Stratum 0](https://github.com/WATonomous/cvmfs-ephemeral/)


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

