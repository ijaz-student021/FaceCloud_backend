#!/bin/bash
# Deploy the current `main` of the backend to the VPS (api.facecloudai.com).
#
#   bash ~/FaceCloud_ops/deploy_vps.sh
#
# What it does (nothing is changed on the server until the archive has arrived):
#   1. updates ~/Projects/FaceCloud_backend to origin/main (fast-forward only) and packs the tracked files (no .env, no database, no keys),
#   2. copies the archive and a small remote script to the server,
#   3. on the server: backs up the current code, replaces it (keeping .runtime/models and the media), installs requirements, runs
#      check --deploy, migrate and collectstatic, then restarts web, worker and beat and prints a smoke test.
# The `deploy` password is asked once for ssh (the connection is reused) and once for sudo. Rollback: the last lines it prints.
set -euo pipefail

HOST="deploy@2.24.154.198"
REPO="$HOME/Projects/FaceCloud_backend"
SOCKET="/tmp/facecloud-vps-ssh-$$"
SSH_OPTS=(-o ControlMaster=auto -o ControlPath="$SOCKET" -o ControlPersist=120 -o ServerAliveInterval=30)
trap 'ssh "${SSH_OPTS[@]}" -O exit "$HOST" >/dev/null 2>&1 || true' EXIT

cd "$REPO"
git fetch -q origin
git checkout -q main
git pull -q --ff-only origin main
REL="$(git rev-parse --short HEAD)"
echo "Deploying main @ $REL ($(git log -1 --format=%s | cut -c1-70))"
git archive --format=tar.gz --prefix=app/ -o "/tmp/facecloud-$REL.tgz" HEAD

REMOTE_SCRIPT="$(mktemp /tmp/facecloud-remote-XXXXXX.sh)"
cat > "$REMOTE_SCRIPT" <<'REMOTE'
#!/bin/bash
set -euo pipefail
REL="$1"
RUN() { sudo -u facecloud bash -c "set -a; . /etc/facecloud/facecloud.env; set +a; cd /srv/facecloud/app && $1"; }
PY=/srv/facecloud/venv/bin/python

sudo -v                                  # asks for the sudo password once; it is remembered for the next minutes
echo "== 1. backup of the current code (and of the database if the backup script exists)"
sudo tar -czf "/var/backups/facecloud/app-before-$REL-$(date +%F-%H%M).tgz" -C /srv/facecloud --exclude=app/.runtime app
if sudo test -x /usr/local/sbin/facecloud-backup; then sudo /usr/local/sbin/facecloud-backup; else echo "   (no database backup script installed yet)"; fi

echo "== 2. replace the code (keeps .runtime/models, .runtime/licenses and the media)"
rm -rf "$HOME/release-new" && mkdir "$HOME/release-new"
tar -xzf "$HOME/facecloud-release.tgz" -C "$HOME/release-new"
sudo rsync -a --delete --exclude='.runtime/' --exclude='__pycache__/' "$HOME/release-new/app/" /srv/facecloud/app/
sudo chown -R facecloud:facecloud /srv/facecloud/app
rm -rf "$HOME/release-new" "$HOME/facecloud-release.tgz"

echo "== 3. requirements"
sudo -u facecloud /srv/facecloud/venv/bin/pip install -q -r /srv/facecloud/app/requirements.txt

echo "== 4. production check, migrations, static files"
RUN "$PY manage.py check --deploy --fail-level ERROR"
RUN "$PY manage.py migrate --noinput" | tail -5
RUN "$PY manage.py collectstatic --noinput" | tail -1

echo "== 5. restart"
sudo systemctl restart facecloud-web facecloud-worker facecloud-beat
sleep 6
systemctl is-active facecloud-web facecloud-worker facecloud-beat
echo "== 6. smoke test"
sudo curl -s -o /dev/null -w "docs through gunicorn: %{http_code}\n" --unix-socket /run/facecloud/gunicorn.sock -H "Host: api.facecloudai.com" -H "X-Forwarded-Proto: https" http://localhost/api/docs/
echo
echo "Done: release $REL."
echo "Rollback: sudo tar -xzf /var/backups/facecloud/app-before-$REL-*.tgz -C /srv/facecloud && sudo systemctl restart facecloud-web facecloud-worker facecloud-beat"
REMOTE

scp -q "${SSH_OPTS[@]}" "/tmp/facecloud-$REL.tgz" "$HOST:~/facecloud-release.tgz"
scp -q "${SSH_OPTS[@]}" "$REMOTE_SCRIPT" "$HOST:~/facecloud-remote-deploy.sh"
rm -f "$REMOTE_SCRIPT"
ssh -t "${SSH_OPTS[@]}" "$HOST" "bash ~/facecloud-remote-deploy.sh $REL; rm -f ~/facecloud-remote-deploy.sh"
curl -s -o /dev/null -w "public check https://api.facecloudai.com/api/docs/ -> %{http_code}\n" https://api.facecloudai.com/api/docs/
