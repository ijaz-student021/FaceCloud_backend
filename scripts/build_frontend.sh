#!/bin/bash
# Build the FaceCloud frontend in API mode (in a CLEAN copy, so local .env files cannot change it) and pack it for Hostinger.
#   bash scripts/build_frontend.sh
# Refuses to make the zip unless the bundle really talks to the backend (the demo-mode mistake of 25 Nov 2026).
set -euo pipefail
SRC="${FRONTEND_SRC:-$HOME/Downloads/FYP}"   # the frontend checkout
API="${FRONTEND_API_URL:-https://api.facecloudai.com}"
ZIP="$HOME/Downloads/facecloud-frontend-dist.zip"
WORK="/tmp/fyp-build"
echo "Source: $(git -C "$SRC" rev-parse --short HEAD), $(git -C "$SRC" status --short | grep -v '^??' | wc -l | tr -d ' ') uncommitted changed file(s)"
rm -rf "$WORK" && mkdir "$WORK"
rsync -a --exclude node_modules --exclude dist --exclude '.env' --exclude '.env.local' --exclude aigocy --exclude 'aigocy.zip' --exclude qa --exclude reference-templates --exclude .git "$SRC/" "$WORK/"
ln -s "$SRC/node_modules" "$WORK/node_modules"
cd "$WORK"
VITE_USE_API=true VITE_API_URL="$API" npm run build >/tmp/frontend-build.log 2>&1 || { tail -20 /tmp/frontend-build.log; echo "BUILD FAILED"; exit 1; }
JS="$(ls dist/assets/index-*.js | head -1)"
grep -q "api.facecloudai.com" "$JS" && grep -q "auth/register" "$JS" || { echo "STOP: this bundle is in DEMO mode (no backend address). Not making a zip."; exit 1; }
[ -f dist/.htaccess ] || { echo "STOP: dist/.htaccess missing"; exit 1; }
rm -f "$ZIP"; (cd dist && zip -qr "$ZIP" . )
echo "OK: API-mode bundle $(basename "$JS")  ->  $ZIP ($(du -h "$ZIP" | cut -f1))"
echo "Upload: public_html -> delete old 'assets' + index.html -> upload zip -> Extract here -> delete zip."
echo "Check:  curl -s https://www.facecloudai.com/ | grep -o 'assets/index-[^\"]*\\.js'   (must show $(basename "$JS"))"
