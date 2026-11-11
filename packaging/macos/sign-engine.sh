#!/usr/bin/env bash
# Sign every Mach-O file of the frozen engine (build/engine-dist/engine) with the Developer ID
# identity in APPLE_SIGNING_IDENTITY, hardened runtime and a secure timestamp, before `tauri build`
# copies it into the app's Resources. Tauri signs the app bundle itself; notarization rejects any
# nested binary left unsigned. Without APPLE_SIGNING_IDENTITY this does nothing (development build).
set -euo pipefail
ENGINE="${1:-build/engine-dist/engine}"
if [ -z "${APPLE_SIGNING_IDENTITY:-}" ]; then
  echo "APPLE_SIGNING_IDENTITY is not set: the engine stays unsigned (development build)."
  exit 0
fi
ENT="$(cd "$(dirname "$0")" && pwd)/entitlements.plist"
# Libraries first, executables last; deepest paths first so containers are signed after their contents.
find "$ENGINE" -type f -print0 | while IFS= read -r -d '' f; do
  if file -b "$f" | grep -q "Mach-O"; then echo "$f"; fi
done | awk '{ print length($0) "\t" $0 }' | sort -rn | cut -f2- | while IFS= read -r f; do
  codesign --force --timestamp --options runtime --entitlements "$ENT" --sign "$APPLE_SIGNING_IDENTITY" "$f"
done
codesign --verify --strict --verbose=2 "$ENGINE/rm-engine"
