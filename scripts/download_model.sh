#!/usr/bin/env bash
# Download a GGUF model into ./models so the `llm` container can serve it.
#
# Presets (first argument):
#   tiny              SmolLM2-135M-Instruct Q4_K_M, 101MiB — the default. Smallest
#                     model that still answers coherently; fastest on CPU.
#   tinyllama-1.1b    TinyLlama-1.1B-Chat v1.0 Q4_K_M, 638MiB — noticeably better
#                     answers, roughly 3x slower on CPU.
#   <repo>            any public GGUF repository; pair it with a filename.
# A gated repo works too when HF_TOKEN is exported.
#
# Usage:
#   scripts/download_model.sh                          # default model
#   scripts/download_model.sh tinyllama-1.1b           # preset
#   scripts/download_model.sh <repo> <filename>          # any other public GGUF
#
# On Windows use scripts/download_model.ps1, which does the same thing without bash.
#
# The filename written here MUST match MODEL_PATH in your .env, because the
# `llm` container mounts ./models read-only and reads that exact path.

set -euo pipefail

PRESET_REPO="bartowski/SmolLM2-135M-Instruct-GGUF"
PRESET_FILE="SmolLM2-135M-Instruct-Q4_K_M.gguf"

case "${1:-}" in
    ""|tiny)
        REPO="$PRESET_REPO"
        FILENAME="$PRESET_FILE"
        ;;
    tinyllama-1.1b|tinyllama)
        REPO="TheBloke/TinyLlama-1.1B-Chat-v1.0-GGUF"
        FILENAME="tinyllama-1.1b-chat-v1.0.Q4_K_M.gguf"
        ;;
    *)
        REPO="$1"
        FILENAME="${2:-}"
        if [[ -z "$FILENAME" ]]; then
            echo "ERROR: <repo> must be followed by a <filename>." >&2
            echo "       e.g. scripts/download_model.sh <repo> model.Q4_K_M.gguf" >&2
            exit 2
        fi
        ;;
esac

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
MODELS_DIR="${PROJECT_ROOT}/models"

mkdir -p "${MODELS_DIR}"
TARGET="${MODELS_DIR}/${FILENAME}"

if [[ -f "${TARGET}" ]]; then
    SIZE=$(du -h "${TARGET}" | cut -f1)
    echo "Model already present: ${TARGET} (${SIZE})"
    echo "Set MODEL_PATH=/models/${FILENAME} in your .env"
    echo "Other presets: tiny | tinyllama-1.1b"
    exit 0
fi

URL="https://huggingface.co/${REPO}/resolve/main/${FILENAME}"

# -C - resumes from whatever is already in the .part, so re-running continues
# an interrupted transfer instead of re-fetching the whole file.
CURL_ARGS=(-fL --progress-bar --retry 3 --retry-delay 5 -C - -o "${TARGET}.part")
if [[ -n "${HF_TOKEN:-}" ]]; then
    echo "Using HF_TOKEN for authenticated download."
    CURL_ARGS+=(-H "Authorization: Bearer ${HF_TOKEN}")
else
    echo "No HF_TOKEN set — this only works for public repositories."
fi

echo "Downloading ${REPO}/${FILENAME}"
echo "  -> ${TARGET}.part"
if ! curl "${CURL_ARGS[@]}" "${URL}"; then
    # The .part is kept on purpose: the next run resumes from it.
    echo "Download interrupted." >&2
    echo "Re-run this script to resume from where it stopped." >&2
    exit 1
fi

# Guard against the LFS pointer file that is served instead of the real blob.
if [[ ! -s "${TARGET}.part" ]] || head -c 20 "${TARGET}.part" | grep -q 'version https://git-lfs'; then
    rm -f "${TARGET}.part"
    echo "ERROR: download did not produce a GGUF file." >&2
    echo "       The repository is probably gated — export HF_TOKEN and retry." >&2
    exit 1
fi

mv "${TARGET}.part" "${TARGET}"
echo "Done: ${TARGET} ($(du -h "${TARGET}" | cut -f1))"
echo "Set MODEL_PATH=/models/${FILENAME} in your .env"
echo "Then: docker compose up -d llm"