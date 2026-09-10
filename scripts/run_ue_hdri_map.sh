#!/bin/zsh
set -euo pipefail

readonly HARNESS_REPO=/Users/laplace/phyawareharness/physics_aware_harness
readonly HARNESS_WORKSPACE=/Volumes/TiPlus7100s/SimulatorWorkspace/physics_aware_harness
readonly HDRI_ROOT=/Volumes/TiPlus7100s/PhysicsAssetSource/polyhaven/hdris/interiors
readonly UE_PROJECT=${HARNESS_WORKSPACE}/ue/SimulatorWorkspace.uproject
readonly UE_EXECUTABLE=/Volumes/TiPlus7100s/UnrealEngine/UE_5.7/Engine/Binaries/Mac/UnrealEditor-Cmd
readonly PYTHON=/Users/laplace/miniforge3/bin/python

if [[ $# -ne 1 ]]; then
  print -u2 "usage: $0 <local-hdri-asset-id|all>"
  exit 2
fi
if [[ "$1" != all && ! "$1" =~ '^[a-z0-9_]+$' ]]; then
  print -u2 "invalid HDRI asset id: $1"
  exit 2
fi
readonly ASSET_ID=$1
if [[ "${ASSET_ID}" != all ]] && ! jq -e --arg asset_id "${ASSET_ID}" '.assets[] | select(.source_asset_id == $asset_id)' "${HDRI_ROOT}/manifest.json" >/dev/null; then
  print -u2 "HDRI asset is not present in the fixed local pool: ${ASSET_ID}"
  exit 2
fi

readonly QUALIFICATION_ROOT=${HARNESS_WORKSPACE}/experiments/iclr_core100_v1/map_qualifications/${ASSET_ID}_current
mkdir -p "${QUALIFICATION_ROOT}"

export SIM_HARNESS_HDRI_POOL_MANIFEST=${HDRI_ROOT}/manifest.json
export SIM_HARNESS_HDRI_MAP_RESULT=${QUALIFICATION_ROOT}/hdri_map_build.json
if [[ "${ASSET_ID}" == all ]]; then
  export SIM_HARNESS_HDRI_ASSET_IDS=$(jq -r '[.assets[].source_asset_id] | join(",")' "${HDRI_ROOT}/manifest.json")
else
  export SIM_HARNESS_HDRI_ASSET_IDS=${ASSET_ID}
fi

cd "${HARNESS_REPO}"
"${UE_EXECUTABLE}" "${UE_PROJECT}" \
  -RenderOffScreen -unattended -nosplash -NoScreenMessages -stdout -FullStdOutLogOutput \
  -ExecutePythonScript=${HARNESS_REPO}/scripts/native_ue_hdri_map_builder.py

if [[ "${ASSET_ID}" == all ]]; then
  jq '{schema_version,status,asset_count,assets:[.assets[]|{source_asset_id,texture_object_path,map_package,texture_imported,map_created}]}' "${SIM_HARNESS_HDRI_MAP_RESULT}"
  exit 0
fi

"${PYTHON}" scripts/harness_prepared_map.py prepare \
  --source-content-root "${HDRI_ROOT}/ue_content" \
  --map-package "/Game/PolyHavenHDRI/Maps/${ASSET_ID}" \
  --ue-project "${UE_PROJECT}" \
  --registration-root "${QUALIFICATION_ROOT}" \
  --catalog "${HARNESS_WORKSPACE}/catalog/assets/catalog.sqlite" \
  --source-uri "https://polyhaven.com/a/${ASSET_ID}" \
  --license CC0 \
  --license-tier reference

"${PYTHON}" scripts/harness_prepared_map.py smoke \
  --registration "${QUALIFICATION_ROOT}/prepared_map_registration.json" \
  --result "${QUALIFICATION_ROOT}/ue_map_qualification.json" \
  --ue-executable "${UE_EXECUTABLE}" \
  --timeout 600

"${PYTHON}" scripts/harness_prepared_map.py qualify \
  --registration "${QUALIFICATION_ROOT}/prepared_map_registration.json" \
  --qualification "${QUALIFICATION_ROOT}/ue_map_qualification.json" \
  --catalog "${HARNESS_WORKSPACE}/catalog/assets/catalog.sqlite"
