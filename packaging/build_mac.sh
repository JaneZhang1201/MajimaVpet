#!/bin/sh
# Build the macOS download: dist/MajimaVpet-<version>-mac-<arm64|x86_64>.zip (+ its SHA-256)
# Run on a Mac from anywhere:  sh packaging/build_mac.sh
# The app is built for this Mac's chip (Apple Silicon = arm64, Intel = x86_64).
set -e
cd "$(dirname "$0")/.."

PY=program/venv/bin/python
if [ ! -x "$PY" ]; then
    echo 'program/venv not found. Set it up first (see README, "Setup").'
    exit 1
fi

VERSION=$("$PY" -c "import sys; sys.path.insert(0, 'program'); from version import APP_VERSION; print(APP_VERSION)")
ARCH=$(uname -m)
echo "Building MajimaVpet $VERSION for macOS ($ARCH)"

"$PY" -m pip install --quiet -r program/requirements.txt -r packaging/requirements-build.txt

rm -rf build/mac-dist build/mac-work build/mac-release
"$PY" packaging/setup_mac.py py2app --dist-dir build/mac-dist --bdist-base build/mac-work

# No paid Apple signature: sign "ad hoc" so it runs on Apple Silicon at all.
# Users still see the "Open Anyway" step the first time (explained in READ ME FIRST.txt).
codesign --force --deep --sign - build/mac-dist/MajimaVpet.app

mkdir -p build/mac-release/MajimaVpet dist
cp -R build/mac-dist/MajimaVpet.app build/mac-release/MajimaVpet/
cp "packaging/READ ME FIRST.txt" build/mac-release/MajimaVpet/

ZIP="dist/MajimaVpet-$VERSION-mac-$ARCH.zip"
rm -f "$ZIP"
ditto -c -k --keepParent build/mac-release/MajimaVpet "$ZIP"  # ditto keeps the app bundle intact (plain zip can break it)
HASH=$(shasum -a 256 "$ZIP" | cut -d' ' -f1)
echo "$HASH  $(basename "$ZIP")" > "$ZIP.sha256"

echo
echo "Done: $ZIP ($(du -h "$ZIP" | cut -f1))"
echo "SHA-256: $HASH"
echo "Test it: unzip it somewhere else, open MajimaVpet.app, and check the menu bar icon appears."
