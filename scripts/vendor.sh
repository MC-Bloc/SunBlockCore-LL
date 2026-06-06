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

echo "→ Plotly.js 2.35.3 (basic)"
curl -sL https://cdn.jsdelivr.net/npm/plotly.js-basic-dist-min@2.35.3/plotly-basic.min.js -o "$VENDOR/plotly.min.js"

echo "✓ All vendor files saved to $VENDOR"
