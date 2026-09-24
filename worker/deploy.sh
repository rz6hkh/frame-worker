#!/bin/bash
# Развернуть/обновить frame_* рядом с Immich. Запуск из WSL от root:
#   bash /mnt/d/frame_backup/worker/deploy.sh
# Immich-сервисы не трогает (--no-deps): пересоздаются только наши три.
set -e
SRC=/mnt/d/frame_backup/worker
DST=/root/immich
cd "$DST"

cp "$SRC/frame.yml" .

# Папки-точки подключения должны существовать до запуска: если их нет,
# docker откажется даже перезапустить контейнер.
if grep -q '^ARHIV_HOST=' .env 2>/dev/null; then
    . <(grep -E '^(ARHIV_HOST|QUARANTINE_HOST|OUT_HOST)=' .env)
    mkdir -p "$ARHIV_HOST" "$QUARANTINE_HOST" "${OUT_HOST:-/mnt/c/scripts/immich/out}"
fi
rm -rf frame_worker frame_proxy
cp -r "$SRC/frame_worker" "$SRC/frame_proxy" .
cp /mnt/c/scripts/immich/database/immich-key.txt frame_worker/immich_key.txt
chmod 600 frame_worker/immich_key.txt

if ! grep -q FRAME_DB_PASSWORD .env; then
    pw=$(tr -dc A-Za-z0-9 </dev/urandom | head -c 24)
    mkdir -p "$DST/frame_tmp/arhiv" "$DST/frame_tmp/quarantine"
    cat >> .env <<EOF

# frame (17.09.2026): своя база и воркер рамки, см. frame.yml
FRAME_DB_LOCATION=/root/frame_postgres
FRAME_DB_PASSWORD=$pw
ARHIV_HOST=$DST/frame_tmp/arhiv
QUARANTINE_HOST=$DST/frame_tmp/quarantine
STATE_HOST=/mnt/c/scripts/immich/database
EOF
fi

docker compose -f docker-compose.yml -f frame.yml config --services
docker compose -f docker-compose.yml -f frame.yml up -d --build --no-deps frame_postgres frame_worker frame_proxy
docker ps --format '{{.Names}} | {{.Status}}' | grep -E 'frame_|immich_server'
