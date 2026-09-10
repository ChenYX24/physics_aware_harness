#!/bin/zsh
set -euo pipefail

readonly HARNESS_REPO=/Users/laplace/phyawareharness/physics_aware_harness
readonly HARNESS_WORKSPACE=/Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness

if [[ "${1:-}" == "--meshy-api-key" ]]; then
  if [[ $# -lt 3 ]]; then
    print -u2 "--meshy-api-key requires a value and an asset-pipeline driver"
    exit 2
  fi
  export SIM_HARNESS_MESHY_API_KEY=$2
  shift 2
fi
if [[ $# -lt 1 ]]; then
  print -u2 "usage: $0 [--meshy-api-key KEY] <asset-pipeline-driver.py> [args...]"
  exit 2
fi
if [[ -z "${SIM_HARNESS_MESHY_API_KEY:-}" ]]; then
  print -u2 "SIM_HARNESS_MESHY_API_KEY is required"
  exit 2
fi

readonly DRIVER=${1:A}
shift
case "${DRIVER}" in
  ${HARNESS_REPO}/scripts/*.py|/private/tmp/*.py) ;;
  *)
    print -u2 "asset pipeline driver must be under ${HARNESS_REPO}/scripts or /private/tmp"
    exit 2
    ;;
esac

export SIM_HARNESS_WORKSPACE=${HARNESS_WORKSPACE}
export SIM_HARNESS_ASSET_CATALOG=${HARNESS_WORKSPACE}/catalog/assets/catalog.sqlite
export SIM_HARNESS_UE_ASSET_IMPORTER_CMD="/Users/laplace/miniforge3/bin/python3.13 ${HARNESS_REPO}/scripts/harness_ue_asset_importer.py"
export SIM_STUDIO_UE_EXECUTABLE=/Volumes/TiPlus7100s/UnrealEngine/UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd
export SIM_STUDIO_UE_PROJECT=${HARNESS_WORKSPACE}/ue/SimulatorWorkspace.uproject

cd "${HARNESS_REPO}"
exec /Users/laplace/miniforge3/bin/python "${DRIVER}" "$@"
