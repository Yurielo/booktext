#!/usr/bin/env bash
set -euo pipefail
repo_root=$(cd "$(dirname "$0")/.." && pwd)
build_dir="$repo_root/BookTextApp/.build/release"
app_dir="${1:-$repo_root/BookTextApp/build/BookText.app}"
swift build --package-path "$repo_root/BookTextApp" -c release
swift run --package-path "$repo_root/BookTextApp" BookTextProtocolTests
mkdir -p "$app_dir/Contents/MacOS" "$app_dir/Contents/Resources"
cp "$repo_root/BookTextApp/Info.plist" "$app_dir/Contents/Info.plist"
cp "$build_dir/BookText" "$app_dir/Contents/MacOS/BookText"
echo "$app_dir"
