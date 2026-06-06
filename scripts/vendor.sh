#!/usr/bin/env bash
# scripts/vendor.sh
# Downloads all frontend dependencies into public/vendor/
# Run once after cloning: bash scripts/vendor.sh

set -e

VENDOR="public/vendor"
mkdir -p "$VENDOR"

echo "→ Socket.IO 4.7.5"
curl -sL https://cdn.socket.io/4.7.5/socket.io.min.js -o "$VENDOR/socket.io.min.js"

echo "→ Alpine.js 3.14.1"
curl -sL https://cdn.jsdelivr.net/npm/alpinejs@3.14.1/dist/cdn.min.js -o "$VENDOR/alpine.min.js"

echo "→ uPlot 1.6.31"
curl -sL https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/uPlot.iife.min.js -o "$VENDOR/uplot.min.js"
curl -sL https://cdn.jsdelivr.net/npm/uplot@1.6.31/dist/uPlot.min.css     -o "$VENDOR/uplot.min.css"

echo "✓ All vendor files saved to $VENDOR"
