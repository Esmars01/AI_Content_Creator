#!/usr/bin/env bash
# Prints the Phase 0 environment facts (rule 1). Read-only: changes nothing.
set -u

section() { printf '\n== %s\n' "$1"; }

section "OS";        (grep PRETTY_NAME /etc/os-release 2>/dev/null || sw_vers 2>/dev/null); uname -srm
section "CPU";       (nproc 2>/dev/null || sysctl -n hw.ncpu); (lscpu 2>/dev/null | grep -m1 "Model name" || true)
section "RAM";       (free -h 2>/dev/null || vm_stat 2>/dev/null | head -5)
section "Disk";      df -h . | tail -1
section "GPU";       (nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader 2>/dev/null || echo "no NVIDIA GPU / nvidia-smi not found")
section "Docker";    (docker --version 2>&1; docker compose version 2>&1; docker info --format '{{.ServerVersion}} {{.Driver}}' 2>&1 | head -1)
section "Python";    (python3.12 --version 2>&1; python3 --version 2>&1; uv --version 2>&1)
section "Node";      (node --version 2>&1; pnpm --version 2>&1)
section "FFmpeg";    (ffmpeg -hide_banner -version 2>&1 | head -1)
for flag in libass libharfbuzz libfribidi libx264; do
  if ffmpeg -hide_banner -version 2>/dev/null | grep -q -- "--enable-$flag"; then echo "  $flag: yes"; else echo "  $flag: NO"; fi
done
LIBASS=$(ldconfig -p 2>/dev/null | grep -m1 'libass.so' | awk '{print $NF}')
if [ -n "${LIBASS:-}" ]; then
  echo "  libass links: $(ldd "$LIBASS" | grep -oE 'harfbuzz|fribidi' | sort -u | tr '\n' ' ')"
fi
section "Network"
for u in https://pypi.org/simple/ https://registry.npmjs.org/ "https://huggingface.co/api/models?limit=1" \
         https://raw.githubusercontent.com/ https://registry-1.docker.io/v2/ https://ghcr.io/v2/ https://nodejs.org/dist/; do
  printf '  %-45s %s\n' "$u" "$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 "$u")"
done
