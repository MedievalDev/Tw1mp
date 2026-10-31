#!/bin/sh
# Fetch the DirectPlay8 headers the capture shim builds against.
#
# They are not committed: the repo is CC0, these headers are LGPL (Wine).
# For the eventual redistributable replacement, swap to a permissively
# licensed header (mingw-w64) or a self-written declaration.
set -e
dir="$(dirname "$0")/include"
mkdir -p "$dir"
base="https://raw.githubusercontent.com/wine-mirror/wine/master/include"
for h in dplay8.h dpaddr.h; do
    curl -fsSL "$base/$h" -o "$dir/$h"
    echo "fetched $h"
done
