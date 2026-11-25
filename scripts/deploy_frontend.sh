#!/bin/bash
# Deploy the FaceCloud frontend to Hostinger shared hosting (facecloudai.com only).
#   bash scripts/deploy_frontend.sh          -> builds, then only SHOWS what would change (dry run)
#   bash scripts/deploy_frontend.sh --go     -> really uploads
# Safety: only domains/facecloudai.com/public_html is touched (the account also hosts other sites); a backup of the
# current index.html + assets is saved on this Mac first; assets go up before index.html; old assets are removed last;
# .htaccess and the other folders (gallery, branding, demo, videos, ...) are never deleted.
set -euo pipefail
: "${FRONTEND_SSH:?set FRONTEND_SSH, e.g. u123456789@203.0.113.10 (Hostinger hPanel > Advanced > SSH Access)}"
HOST="$FRONTEND_SSH"
PORT="${FRONTEND_SSH_PORT:-65002}"
: "${FRONTEND_REMOTE:?set FRONTEND_REMOTE, e.g. domains/facecloudai.com/public_html (only this folder is touched)}"
REMOTE="$FRONTEND_REMOTE"
GO=0; [ "${1:-}" = "--go" ] && GO=1
SOCKET="/tmp/facecloud-web-ssh-$$"
SSH_OPTS=(-p "$PORT" -o ControlMaster=auto -o ControlPath="$SOCKET" -o ControlPersist=300 -o ServerAliveInterval=30)
trap 'ssh "${SSH_OPTS[@]}" -O exit "$HOST" >/dev/null 2>&1 || true' EXIT
RS=(rsync -rltzc --exclude .DS_Store -e "ssh ${SSH_OPTS[*]}")
[ "$GO" = 1 ] || RS+=(-n -i)   # -n dry run, -i itemize changes

BUILD_OUT="$(bash "$(dirname "$0")/build_frontend.sh")"
echo "$BUILD_OUT" | sed -n '1,2p'
DIST="/tmp/fyp-build/dist"
NEW="$(basename "$(ls "$DIST"/assets/index-*.js | head -1)")"

echo "== the server is asked once for the SSH password"
ssh "${SSH_OPTS[@]}" "$HOST" "test -d $REMOTE && echo 'remote folder ok: $REMOTE'"

if [ "$GO" = 1 ]; then
  B="${FRONTEND_BACKUP_DIR:-$HOME/FaceCloud_ops/frontend_backups}/$(date +%F-%H%M)"; mkdir -p "$B"
  echo "== backup of the current site files -> $B"
  rsync -rlt -e "ssh ${SSH_OPTS[*]}" --exclude .DS_Store "$HOST:$REMOTE/index.html" "$HOST:$REMOTE/assets" "$HOST:$REMOTE/.htaccess" "$B/" || true
fi

echo "== 1. assets (new files first, nothing deleted yet)"
"${RS[@]}" "$DIST/assets/" "$HOST:$REMOTE/assets/"
echo "== 2. other files (not index.html, not .htaccess, not assets)"
"${RS[@]}" --exclude /assets --exclude /index.html --exclude /.htaccess "$DIST/" "$HOST:$REMOTE/"
echo "== 3. index.html (this is the moment the new version goes live)"
"${RS[@]}" "$DIST/index.html" "$HOST:$REMOTE/index.html"
echo "== 4. remove old assets that the new index.html no longer uses (inside assets/ only)"
"${RS[@]}" --delete "$DIST/assets/" "$HOST:$REMOTE/assets/"

if [ "$GO" = 1 ]; then
  echo "== check"; sleep 2
  LIVE="$(curl -s https://www.facecloudai.com/ | grep -o 'assets/index-[^"]*\.js' | head -1)"
  echo "live: $LIVE   expected: assets/$NEW"
  [ "$LIVE" = "assets/$NEW" ] && echo "DONE: the new frontend is live. Hard-refresh the browser (Cmd+Shift+R)." || echo "MISMATCH: wait a minute and check again; if it stays, tell Claude."
  echo "Rollback: the backup is in $B (copy index.html and assets back into $REMOTE)."
else
  echo; echo "That was a DRY RUN (nothing changed on the server). To really deploy: bash scripts/deploy_frontend.sh --go"
fi
