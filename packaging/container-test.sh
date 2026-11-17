#!/bin/bash
# Contract test for the Desk image (constitution container-image VI), run by
# container.yml against the pushed digest in $IMAGE. Starts the image as
# production does (read-only root, /data on a volume), waits for /healthz,
# mints an admin key against /data with ashigaru-desk mint-key, and proves the
# key authenticates. Also: not root, and HEALTHCHECK present.
set -euo pipefail

: "${IMAGE:?IMAGE is the digest under test}"
name=ashigaru-contract-$$
volume=$name-data
work=$(mktemp -d)
trap 'podman rm -f "$name" >/dev/null 2>&1 || true; podman volume rm -f "$volume" >/dev/null 2>&1 || true; rm -rf "$work"' EXIT

user=$(podman image inspect "$IMAGE" --format '{{.Config.User}}')
case "$user" in "" | 0 | root | 0:*) echo "image runs as root ($user)" >&2; exit 1 ;; esac
[ -n "$(podman image inspect "$IMAGE" --format '{{.Config.Healthcheck}}')" ] || {
    echo "image carries no HEALTHCHECK" >&2
    exit 1
}

podman volume create "$volume" >/dev/null
podman run -d --name "$name" --read-only -v "$volume:/data" -p 127.0.0.1::8080 "$IMAGE" >/dev/null
port=$(podman port "$name" 8080/tcp | sed 's/.*://')
base="http://127.0.0.1:$port"

for _ in $(seq 1 30); do
    curl -fsS "$base/healthz" >/dev/null 2>&1 && break
    sleep 1
done
curl -fsS "$base/healthz" | grep -q '"ok":true' || {
    echo "/healthz did not answer" >&2
    podman logs "$name" >&2
    exit 1
}
podman exec "$name" ashigaru-desk health

podman exec "$name" ashigaru-desk mint-key contract --role admin --db /data/desk.db --out /data/contract.key
podman cp "$name:/data/contract.key" "$work/contract.key"
status=$(curl -s -o "$work/me.json" -w '%{http_code}' \
    -H "Authorization: Bearer $(tr -d '\n' < "$work/contract.key")" "$base/v1/me")
[ "$status" = 200 ] && grep -q '"name":"contract"' "$work/me.json" || {
    echo "minted key did not authenticate: HTTP $status" >&2
    exit 1
}
[ "$(curl -s -o /dev/null -w '%{http_code}' "$base/v1/me")" = 401 ] || {
    echo "/v1/me answered without a key" >&2
    exit 1
}
echo "contract: boots read-only as $user, /healthz answers, a minted key authenticates"
