#!/usr/bin/env bash
# Fetch the whisper.cpp ggml-small multilingual model into data/models/ and
# record its SHA-256 to a sidecar that server.transcribe.verify_model checks.
#
# Truth gate: the checksum is COMPUTED from the downloaded bytes, never invented.
# One-time operator step — the model is NOT installed by the app, and nothing
# here is committed (data/ is gitignored).
#
# Usage: scripts/fetch_whisper_model.sh /path/to/whisper.cpp
#   (or set WHISPER_CPP_DIR). The directory must contain
#   models/download-ggml-model.sh from a whisper.cpp checkout.
set -euo pipefail

MODEL="small"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST_DIR="$ROOT/data/models"
DEST="$DEST_DIR/ggml-${MODEL}.bin"
WHISPER_CPP_DIR="${1:-${WHISPER_CPP_DIR:-}}"

if [[ -z "$WHISPER_CPP_DIR" || ! -f "$WHISPER_CPP_DIR/models/download-ggml-model.sh" ]]; then
  echo "error: pass the whisper.cpp checkout dir (contains models/download-ggml-model.sh)" >&2
  echo "usage: $0 /path/to/whisper.cpp   (or set WHISPER_CPP_DIR)" >&2
  exit 1
fi

mkdir -p "$DEST_DIR"
# download-ggml-model.sh writes ggml-<model>.bin into the checkout's models/ dir.
( cd "$WHISPER_CPP_DIR" && bash models/download-ggml-model.sh "$MODEL" )
cp "$WHISPER_CPP_DIR/models/ggml-${MODEL}.bin" "$DEST"

# Record the checksum computed from the bytes we just downloaded.
if command -v sha256sum >/dev/null 2>&1; then
  SHA="$(sha256sum "$DEST" | awk '{print $1}')"
else
  SHA="$(shasum -a 256 "$DEST" | awk '{print $1}')"
fi
printf '%s\n' "$SHA" > "$DEST.sha256"
echo "installed $DEST"
echo "recorded  $DEST.sha256 ($SHA)"
