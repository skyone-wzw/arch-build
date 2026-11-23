#!/usr/bin/env bash
set -euo pipefail

BUILDER="${ARCH_BUILD_USER:-builder}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "setup-builder.sh must run as root" >&2
  exit 1
fi

pacman -S --noconfirm --needed --noprogressbar --asdeps \
  base-devel \
  ca-certificates \
  curl \
  git \
  github-cli \
  gnupg \
  python \
  uv \
  zstd

# Unprivileged build user without sudo; root installs dependencies beforehand.
if ! id -u "${BUILDER}" >/dev/null 2>&1; then
  useradd --create-home --user-group --shell /bin/bash "${BUILDER}"
fi

# Disable default -debug split packages so local builds stay aligned with PKGBUILD pkgname.
cat > "/home/${BUILDER}/.makepkg.conf" <<'EOF'
OPTIONS+=(!debug)
EOF
chown "${BUILDER}:${BUILDER}" "/home/${BUILDER}/.makepkg.conf"
