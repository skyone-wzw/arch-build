#!/usr/bin/env bash
set -euo pipefail

log() {
  echo "[chatgpt-desktop] $*"
}

URL="https://persistent.oaistatic.com/codex-app-prod/linux/deb/dists/stable/main/binary-amd64/Packages"
PKGBUILD="PKGBUILD"

meta=$(curl -fsSL "$URL")

new_ver=$(awk '/^Package: chatgpt$/,/^$/ { if ($1 == "Version:") print $2 }' <<< "$meta")
new_sha=$(awk '/^Package: chatgpt$/,/^$/ { if ($1 == "SHA256:") print $2 }' <<< "$meta")

[[ "$new_ver" =~ ^[0-9]+(\.[0-9]+)+$ ]]
[[ "$new_sha" =~ ^[0-9a-f]{64}$ ]]

cur_ver=$(sed -n 's/^pkgver=//p' "$PKGBUILD")
[[ -n "$cur_ver" ]]

if [[ "$cur_ver" == "$new_ver" ]]; then
  log "Version unchanged"
  exit 0
fi

sed -i -E \
  -e "s/^pkgver=.*/pkgver=${new_ver}/" \
  -e "s/^pkgrel=.*/pkgrel=1/" \
  -e "/^sha256sums=\(/,/^\)/ s/'[0-9a-f]{64}'/'${new_sha}'/" \
  "$PKGBUILD"

grep -q "^pkgver=${new_ver}$" "$PKGBUILD"
grep -q "^pkgrel=1$" "$PKGBUILD"
grep -q "'${new_sha}'" "$PKGBUILD"

log "Updated: ${new_ver} (${new_sha})"
