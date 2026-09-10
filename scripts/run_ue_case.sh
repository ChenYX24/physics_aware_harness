#!/bin/zsh
set -euo pipefail

readonly HARNESS_REPO=/Users/laplace/phyawareharness/physics_aware_harness

export SIM_HARNESS_WORKSPACE=/Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness
export SIM_STUDIO_UE_PROJECT=/Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness/ue/SimulatorWorkspace.uproject
export SIM_STUDIO_UE_EXECUTABLE=/Volumes/TiPlus7100s/UnrealEngine/UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd
export SIM_STUDIO_UE_ACTOR_CLASS=/Script/Engine.StaticMeshActor
export SIM_STUDIO_UE_MAP=${SIM_STUDIO_UE_MAP:-/Game/HarnessGenerated/Maps/BallBoardSmoke}
export SIM_STUDIO_ASSET_REGISTRY=/Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness/catalog/local_generated/asset_registry.local.json
export SIM_STUDIO_UE_CONTACT_EXPORT=1
export SIM_HARNESS_IGNORE_PROVENANCE_AND_RELEASE_GATES=1
export SIM_STUDIO_UE_RUNNER_CMD="/Users/laplace/miniforge3/bin/python3.13 ${HARNESS_REPO}/scripts/harness_local_ue_runner.py"

cd "${HARNESS_REPO}"
exec /Users/laplace/miniforge3/bin/python scripts/harness_run_case.py "$@"
