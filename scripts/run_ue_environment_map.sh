#!/bin/zsh
set -euo pipefail

readonly HARNESS_REPO=/Users/laplace/phyawareharness/physics_aware_harness
readonly HARNESS_WORKSPACE=/Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness
readonly ASSET_SOURCE=/Volumes/TiPlus7100s/PhysicsAssetSource
readonly PYTHON=/Users/laplace/miniforge3/bin/python
readonly UE=/Volumes/TiPlus7100s/UnrealEngine/UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd
readonly PROJECT=${HARNESS_WORKSPACE}/ue/SimulatorWorkspace.uproject

case "${1:-}" in
  build)
    if [[ $# -ne 4 ]]; then
      print -u2 "usage: $0 build <driver.py> <source.gltf> <result.json>"
      exit 2
    fi
    readonly DRIVER=${2:A}
    readonly SOURCE=${3:A}
    readonly RESULT=${4:A}
    case "${DRIVER}" in
      ${HARNESS_REPO}/scripts/*.py|/private/tmp/*.py) ;;
      *) print -u2 "driver must be under ${HARNESS_REPO}/scripts or /private/tmp"; exit 2 ;;
    esac
    case "${SOURCE}" in
      ${ASSET_SOURCE}/*) ;;
      *) print -u2 "source must be under ${ASSET_SOURCE}"; exit 2 ;;
    esac
    case "${RESULT}" in
      ${HARNESS_WORKSPACE}/*|/private/tmp/*) ;;
      *) print -u2 "result must be under the Harness workspace or /private/tmp"; exit 2 ;;
    esac
    export SIM_HARNESS_ENVIRONMENT_SOURCE=${SOURCE}
    export SIM_HARNESS_ENVIRONMENT_RESULT=${RESULT}
    exec "${UE}" "${PROJECT}" -RenderOffScreen -unattended -nosplash -NoScreenMessages -stdout -FullStdOutLogOutput "-ExecutePythonScript=${DRIVER}"
    ;;
  smoke)
    if [[ $# -ne 3 ]]; then
      print -u2 "usage: $0 smoke <prepared-map-registration.json> <result.json>"
      exit 2
    fi
    exec "${PYTHON}" "${HARNESS_REPO}/scripts/harness_prepared_map.py" smoke \
      --registration "${2:A}" \
      --result "${3:A}" \
      --ue-executable "${UE}"
    ;;
  *)
    print -u2 "usage: $0 {build|smoke} ..."
    exit 2
    ;;
esac
