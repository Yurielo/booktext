#!/usr/bin/env bash
set -euo pipefail
repo_root=$(cd "$(dirname "$0")/.." && pwd)
build_dir="$repo_root/BookTextApp/.build/release"
app_dir="${1:-$repo_root/BookTextApp/build/BookText.app}"
if [[ "$app_dir" != *.app ]]; then
  echo "Refusing to assemble a non-.app bundle path: $app_dir" >&2
  exit 2
fi
swift build --package-path "$repo_root/BookTextApp" -c release
swift run --package-path "$repo_root/BookTextApp" BookTextProtocolTests
# Assemble every bundle file before signing.  The Swift executable may have an
# ad-hoc linker signature, but it is not a signature for the enclosing app.
# A final bundle signature binds Info.plist and all resources together.
rm -rf "$app_dir"
mkdir -p "$app_dir/Contents/MacOS" "$app_dir/Contents/Resources"
cp "$repo_root/BookTextApp/Info.plist" "$app_dir/Contents/Info.plist"
cp "$build_dir/BookText" "$app_dir/Contents/MacOS/BookText"
cp "$repo_root/BookTextApp/Resources/BookText.icns" "$app_dir/Contents/Resources/BookText.icns"
# Local-only BookText.app uses macOS's supported ad-hoc identity.  This is
# deliberately after all Contents mutations and verification is mandatory.
# Finder/iCloud may attach FinderInfo to a newly created .app directory even
# when no source resource carries it. Remove only these signature-incompatible
# metadata fields from the just-assembled owned bundle, never quarantine or
# system/user directories. Signature verification below remains fail-closed.
xattr -dr com.apple.FinderInfo "$app_dir" 2>/dev/null || true
xattr -dr com.apple.ResourceFork "$app_dir" 2>/dev/null || true
codesign --force --deep --sign - --timestamp=none "$app_dir"
codesign --verify --deep --strict --verbose=4 "$app_dir"
echo "$app_dir"
