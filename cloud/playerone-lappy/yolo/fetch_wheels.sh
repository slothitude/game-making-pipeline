#!/bin/bash
# fetch_wheels.sh — the pytorch cu124 wheels over aria2 (multi-connection,
# resumable) because uv's single pipe crawls at ~350KB/s and stalls.
# uv then installs them from --find-links without re-downloading.
set -u
cd /home/aaron/playerone/yolo/wheels || exit 1
mv -f torch-test.part 'torch-2.5.1+cu124-cp312-cp312-linux_x86_64.whl' 2>/dev/null
mv -f torch-test.part.aria2 'torch-2.5.1+cu124-cp312-cp312-linux_x86_64.whl.aria2' 2>/dev/null

fetch () {  # fetch <package-index-name> <version>
  local pkg="$1" ver="$2"
  local page href fname
  page=$(curl -4 -s "https://download.pytorch.org/whl/cu124/$pkg/")
  # x86_64 only — the index lists aarch64 builds too and Lappy is amd64
  href=$(printf '%s' "$page" | grep -o "href=\"[^\"]*$ver[^\"]*x86_64\.whl" | grep -v aarch64 | head -1 | sed 's/href="//;s/#.*$//')
  fname=$(basename "$href")
  if [ -z "$fname" ]; then
    echo "MISS $pkg $ver"
    return
  fi
  if [ ! -f "$fname" ] || [ -f "$fname.aria2" ]; then
    aria2c -x16 -s16 -k1M --file-allocation=none --console-log-level=warn \
      -o "$fname" "https://download.pytorch.org/whl/cu124/$fname"
  fi
  echo "OK $fname"
}

fetch torch 2.5.1%2Bcu124 &
fetch torchvision 0.20.1%2Bcu124 &
fetch nvidia-cublas-cu12 12.4.5.8 &
fetch nvidia-cuda-cupti-cu12 12.4.127 &
fetch nvidia-cuda-nvrtc-cu12 12.4.127 &
wait
fetch nvidia-cuda-runtime-cu12 12.4.127 &
fetch nvidia-cudnn-cu12 9.1.0.70 &
fetch nvidia-cufft-cu12 11.2.1.3 &
fetch nvidia-curand-cu12 10.3.5.147 &
wait
fetch nvidia-cusolver-cu12 11.6.1.9 &
fetch nvidia-cusparse-cu12 12.3.1.170 &
fetch nvidia-nccl-cu12 2.21.5 &
fetch nvidia-nvtx-cu12 12.4.127 &
fetch nvidia-nvjitlink-cu12 12.4.127 &
fetch triton 3.1.0 &
wait
echo ALL-FETCHED
