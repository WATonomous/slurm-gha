# run-gha-on-slurm

Runs GitHub Actions jobs on our Slurm cluster, each on an ephemeral runner in its own Slurm job.

## What it does

Every cycle (a few seconds apart), for each repository in `config.REPOS_TO_MONITOR`, the daemon (`main.py`)
reads our Slurm jobs (`squeue`) and our runner registrations (GitHub's runners list), then the GitHub jobs
waiting for a runner. Per size label (`slurm-runner-medium` and so on) it compares the waiting jobs with our
runners that can still take one and submits a Slurm job for each runner missing. It never matches a runner to
a job: GitHub gives each waiting job to whichever of our runners asks first.

Each Slurm job runs `allocation_scripts/apptainer.sh`: rootless Docker for the job's containers, then one
just-in-time (JIT) runner in an apptainer container. A runner that has taken no job by its idle deadline
(`IDLE_ALLOWANCE_S`, 10 minutes) stops itself; a runner never runs a second job. The daemon never ends a
running Slurm job; it only cancels pending ones that can never start and deletes the registration of a runner
whose Slurm job has ended.

Where each size may go is the node rule, in `node_rule.py`, one line each:
1. A size is not sent to a node where its newest runner failed to launch; it is used there again once a runner
   of that size takes a job there.
2. Such a node gets one extra runner pinned to it 1 minute after the failure, then 2, 4, 8 and at most 10
   minutes after each further failure there.
3. A size with no node known to work (after a restart, or failing everywhere) gets one runner at a time.
4. A runner pending in Slurm's queue never keeps excluding a node a new runner of its size may use.
5. A failure that belongs to no node (Slurm refused the submission, or the failed job shows no node) makes the
   size's next runner wait 1, 2, 4, 8, at most 10 minutes.

`plan.py` says what our runners are doing (pending, booting, idle, took a job, failed to launch); `supply.py`
talks to Slurm and to GitHub's runner endpoints, `demand.py` reads the waiting jobs, `github_client.py`
paces and retries GitHub requests. Everything the daemon remembers is a cache: after a restart it reads the
state again and is careful (one runner per size) until it sees where each size works.

## Operating it

**Pause CI:** `kubectl -n gha-slurm scale deployment gha-slurm --replicas=0`, and `--replicas=1` to resume.
Whatever applies the deployment's configuration may set it back to 1. Holding runner jobs in Slurm does
**not** pause CI any more: the daemon cancels held pending runner jobs, which can never start, and submits new
ones. Runners already running finish their job or stop at their idle deadline.

**Keep runners off a node:** drain it, `scontrol update NodeName=<node> State=DRAIN Reason=<why>`. The daemon
reads no node list and needs no change. A runner pinned to a drained node stays pending and holds nothing; it is
cancelled once GitHub drops its unused registration, about a day after it was submitted.

**Log lines** (each starts with the repository; all but the outage lines then name the size label):
- `out on <node>: Slurm job N <how it failed> (its log: <path> on <node>); next pinned runner there in N s`
  (WARNING): runners of that size skip the node. How it failed points at where to look: *ended without
  taking a job* (the image, Docker, the runner), *was still offline 120 s after it started* (a slow boot, CVMFS,
  a hang), *stayed idle while a job of its size waited* (GitHub not giving it jobs). Start with the runner's
  log, `/var/log/slurm-ci/slurm-ci-<job id>.out` on that node.
- `still out on <node>: ...; 2nd failed retry; next pinned runner there in 240 s` (WARNING): the pinned retry
  failed too. A node that keeps going out for every size is a node problem: drain it and look at it.
- `usable again on <node> (Slurm job N took a job)` (INFO).
- `one runner at a time: <why>` (WARNING): no node is known to work for the size, or Slurm refused a
  submission, or a runner failed with no node. `works on some node again; every waiting job gets a runner`
  (INFO) when that ends.
- `Slurm refused <runner> (<sbatch's message>)...` and `sbatch failed and squeue shows no <runner>...`
  (WARNING): sbatch failed; the line says what the daemon concluded.
- `narrowed the exclusion of N pending runners from <nodes> to <nodes>` (INFO): pending runners released onto
  nodes that came back.
- `cycles stopped, Slurm|GitHub cannot be read: ...` (WARNING), then `cycles still stopped after N s and M
  cycles: ...` every 10 minutes while it lasts (or `cycles still stopped, now GitHub|Slurm cannot be read`),
  and `cycles recovered after N s and M failed cycles; the last: ...` (INFO). Nothing is submitted or
  cancelled while a read fails.

## Configuration

`config.py` holds every setting, each with the reason for its value. `REPOS_TO_MONITOR` lists the
repositories served; a runner or Slurm job is ours only if its name starts with the repository's
`runner_name_prefix`, and no prefix may start another (the daemon refuses to start otherwise).

`GITHUB_ACCESS_TOKEN` (environment) must be able to administer the repository's self-hosted runners: create
JIT configs, list runners and delete them. The daemon removes it from its environment at start, so it never
reaches a runner job.

## Development

- Tests: `python3 -m unittest discover -s tests`. The batch-script tests run only on Linux (they read
  `/proc`) and are skipped elsewhere; CI runs them.
- Lint: `scripts/lint.sh` (ruff check and format), and `shellcheck allocation_scripts/apptainer.sh start.sh
  scripts/lint.sh`.
