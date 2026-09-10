#!/bin/zsh
set -euo pipefail

readonly HARNESS_REPO=/Users/laplace/phyawareharness/physics_aware_harness
readonly HARNESS_WORKSPACE=/Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness

if [[ "${1:-}" == "build-plugin" ]]; then
  shift
  if [[ $# -ne 0 ]]; then
    print -u2 "usage: $0 build-plugin"
    exit 2
  fi
  cd "${HARNESS_REPO}"
  exec /Users/laplace/miniforge3/bin/python scripts/harness_workspace.py \
    --workspace "${HARNESS_WORKSPACE}" build-ue-plugin \
    --ue-executable /Volumes/TiPlus7100s/UnrealEngine/UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd \
    --max-parallel-actions 4
fi

if [[ "${1:-}" == "terminate-stuck-job" ]]; then
  if [[ $# -ne 2 || ! "$2" =~ '^job_[a-z0-9_]+$' ]]; then
    print -u2 "usage: $0 terminate-stuck-job <job_id>"
    exit 2
  fi
  readonly JOB_ROOT=${HARNESS_WORKSPACE}/jobs/$2
  readonly LOCK_PATH=${JOB_ROOT}/.controller.lock
  if [[ ! -d "${JOB_ROOT}" || ! -f "${LOCK_PATH}" ]]; then
    print -u2 "unknown or unlocked job: $2"
    exit 2
  fi
  integer pid
  typeset -a job_pids
  typeset job_process_text
  job_process_text="$({ lsof -t "${LOCK_PATH}"; lsof -t +D "${JOB_ROOT}"; } 2>/dev/null | sort -u || true)"
  job_pids=(${(f)job_process_text})
  if (( ${#job_pids} == 0 )); then
    print "no live process owns job $2"
    exit 0
  fi
  for pid in ${job_pids}; do
    [[ "${pid}" == "$$" ]] && continue
    kill -TERM "${pid}" 2>/dev/null || true
  done
  sleep 2
  for pid in ${job_pids}; do
    [[ "${pid}" == "$$" ]] && continue
    kill -0 "${pid}" 2>/dev/null || continue
    kill -KILL "${pid}" 2>/dev/null || true
  done
  print "terminated stuck processes for $2: ${job_pids[*]}"
  exit 0
fi

if [[ "${1:-}" == "--meshy-api-key" ]]; then
  if [[ $# -lt 3 ]]; then
    print -u2 "--meshy-api-key requires a value and a Controller command"
    exit 2
  fi
  export SIM_HARNESS_MESHY_API_KEY=$2
  shift 2
fi

export SIM_HARNESS_WORKSPACE=${HARNESS_WORKSPACE}
export SIM_STUDIO_UE_PROJECT=${HARNESS_WORKSPACE}/ue/SimulatorWorkspace.uproject
export SIM_STUDIO_UE_EXECUTABLE=/Volumes/TiPlus7100s/UnrealEngine/UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd
export SIM_STUDIO_UE_ACTOR_CLASS=/Script/Engine.StaticMeshActor
export SIM_STUDIO_ASSET_REGISTRY=${HARNESS_WORKSPACE}/catalog/local_generated/asset_registry.local.json
export SIM_STUDIO_UE_CONTACT_EXPORT=1
export SIM_HARNESS_IGNORE_PROVENANCE_AND_RELEASE_GATES=1
export SIM_STUDIO_UE_RUNNER_CMD="/Users/laplace/miniforge3/bin/python3.13 ${HARNESS_REPO}/scripts/harness_local_ue_runner.py"

cd "${HARNESS_REPO}"
exec /Users/laplace/miniforge3/bin/python scripts/harness_agent_job.py "$@"
