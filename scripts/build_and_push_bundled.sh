#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

VERSION=""
HUB=""
IMAGE_NAME="castrel-proxy"
PLATFORMS="linux/amd64,linux/arm64"
DOCKERFILE="Dockerfile.bundled"
STRICT_LATEST="1"
DEBIAN_MIRROR="deb.debian.org"
PUSH_LATEST="1"
LATEST_ONLY="0"
LOAD="0"

usage() {
  cat <<'EOF'
Build and push multi-arch bundled image with Docker Buildx.

Required:
  --hub <hub>           Docker Hub namespace/repository prefix, e.g. castrelai

Optional:
  --version <v>         Image version tag, e.g. 0.1.11
  --image <name>        Image name (default: castrel-proxy)
  --platforms <list>    Buildx platforms (default: linux/amd64,linux/arm64)
  --dockerfile <path>   Dockerfile path relative to open-castrel-proxy (default: Dockerfile.bundled)
  --strict-latest <0|1> Pass build arg MCP_BUNDLE_STRICT_LATEST (default: 1)
  --debian-mirror <origin> Debian archive origin for apt-get inside the image
                        (default: deb.debian.org; e.g. mirrors.aliyun.com).
                        A bare host is upgraded to https://.
  --push-latest <0|1>   Also push :latest tag (default: 1)
  --latest-only <0|1>   Only push :latest tag (default: 0)
  --load <0|1>          Local mode: build single-arch and load into the local
                        docker image store instead of pushing (default: 0).
                        Forces a single host platform; --version optional.
  -h, --help            Show this help

Examples:
  # Local verify (no push): builds castrelai/castrel-proxy:latest into local docker
  ./scripts/build_and_push_bundled.sh --hub castrelai --load 1

  # Publish after local verify passes
  ./scripts/build_and_push_bundled.sh --version 0.1.11 --hub castrelai
  ./scripts/build_and_push_bundled.sh --version 0.1.11 --hub myorg --push-latest 0
  ./scripts/build_and_push_bundled.sh --hub castrelai --latest-only 1

  # Mainland China network: use a domestic Debian mirror for apt-get.
  # The https:// scheme matters: some networks block port 80 outright.
  ./scripts/build_and_push_bundled.sh --version 0.1.16 --hub castrelai \
    --debian-mirror https://mirrors.aliyun.com --push-latest 0
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --version)
      VERSION="${2:-}"
      shift 2
      ;;
    --hub)
      HUB="${2:-}"
      shift 2
      ;;
    --image)
      IMAGE_NAME="${2:-}"
      shift 2
      ;;
    --platforms)
      PLATFORMS="${2:-}"
      shift 2
      ;;
    --dockerfile)
      DOCKERFILE="${2:-}"
      shift 2
      ;;
    --strict-latest)
      STRICT_LATEST="${2:-}"
      shift 2
      ;;
    --debian-mirror)
      DEBIAN_MIRROR="${2:-}"
      shift 2
      ;;
    --push-latest)
      PUSH_LATEST="${2:-}"
      shift 2
      ;;
    --latest-only)
      LATEST_ONLY="${2:-}"
      shift 2
      ;;
    --load)
      LOAD="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 2
      ;;
  esac
done

if [[ -z "${HUB}" ]]; then
  echo "Error: --hub is required." >&2
  usage
  exit 2
fi

if [[ "${STRICT_LATEST}" != "0" && "${STRICT_LATEST}" != "1" ]]; then
  echo "Error: --strict-latest must be 0 or 1." >&2
  exit 2
fi

if [[ "${PUSH_LATEST}" != "0" && "${PUSH_LATEST}" != "1" ]]; then
  echo "Error: --push-latest must be 0 or 1." >&2
  exit 2
fi

if [[ "${LATEST_ONLY}" != "0" && "${LATEST_ONLY}" != "1" ]]; then
  echo "Error: --latest-only must be 0 or 1." >&2
  exit 2
fi

if [[ "${LOAD}" != "0" && "${LOAD}" != "1" ]]; then
  echo "Error: --load must be 0 or 1." >&2
  exit 2
fi

if [[ "${LOAD}" == "1" && -z "${VERSION}" ]]; then
  LATEST_ONLY="1"
fi

if [[ "${LATEST_ONLY}" == "1" ]]; then
  PUSH_LATEST="1"
fi

if [[ "${LOAD}" == "0" && "${LATEST_ONLY}" == "0" && -z "${VERSION}" ]]; then
  echo "Error: --version is required unless --latest-only=1 or --load=1." >&2
  usage
  exit 2
fi

IMAGE_BASE="${HUB}/${IMAGE_NAME}"
LATEST_TAG="${IMAGE_BASE}:latest"
DOCKERFILE_PATH="${PROJECT_ROOT}/${DOCKERFILE}"

if [[ ! -f "${DOCKERFILE_PATH}" ]]; then
  echo "Error: Dockerfile not found: ${DOCKERFILE_PATH}" >&2
  exit 2
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is not installed." >&2
  exit 2
fi

if ! docker buildx version >/dev/null 2>&1; then
  echo "Error: docker buildx is not available." >&2
  exit 2
fi

if ! docker info >/dev/null 2>&1; then
  echo "Error: docker daemon is not running or not accessible." >&2
  exit 2
fi

if [[ "${LOAD}" == "1" ]]; then
  if [[ "${PLATFORMS}" == *","* ]]; then
    HOST_PLATFORM="$(docker version -f '{{.Server.Os}}/{{.Server.Arch}}' 2>/dev/null || true)"
    PLATFORMS="${HOST_PLATFORM:-linux/amd64}"
    echo "Load mode: narrowing to single host platform ${PLATFORMS}"
  fi
  # Use the buildx builder bound to the active docker context (docker driver
  # supports --load). Falls back to the default builder.
  CURRENT_CONTEXT="$(docker context show 2>/dev/null || echo default)"
  docker buildx use "${CURRENT_CONTEXT}" >/dev/null 2>&1 \
    || docker buildx use default >/dev/null
  OUTPUT_FLAG="--load"
else
  if ! docker buildx inspect multiarch-builder >/dev/null 2>&1; then
    docker buildx create --name multiarch-builder --driver docker-container --use >/dev/null
  else
    docker buildx use multiarch-builder >/dev/null
  fi
  OUTPUT_FLAG="--push"
fi

TAGS=()
if [[ "${LATEST_ONLY}" == "0" ]]; then
  VERSION_TAG="${IMAGE_BASE}:${VERSION}"
  TAGS+=("-t" "${VERSION_TAG}")
fi
if [[ "${PUSH_LATEST}" == "1" ]]; then
  TAGS+=("-t" "${LATEST_TAG}")
fi

if [[ ${#TAGS[@]} -eq 0 ]]; then
  echo "Error: no tags configured to push." >&2
  exit 2
fi

if [[ "${LATEST_ONLY}" == "0" ]]; then
  echo "Target image: ${VERSION_TAG}"
fi
if [[ "${PUSH_LATEST}" == "1" ]]; then
  echo "Target image: ${LATEST_TAG}"
fi
echo "Platforms: ${PLATFORMS}"
echo "Dockerfile: ${DOCKERFILE_PATH}"
echo "MCP_BUNDLE_STRICT_LATEST=${STRICT_LATEST}"
echo "DEBIAN_MIRROR=${DEBIAN_MIRROR}"
echo "Output: ${OUTPUT_FLAG}"

docker buildx build \
  --platform "${PLATFORMS}" \
  --file "${DOCKERFILE_PATH}" \
  --build-arg "MCP_BUNDLE_STRICT_LATEST=${STRICT_LATEST}" \
  --build-arg "DEBIAN_MIRROR=${DEBIAN_MIRROR}" \
  "${TAGS[@]}" \
  "${OUTPUT_FLAG}" \
  "${PROJECT_ROOT}"

echo "Done."
