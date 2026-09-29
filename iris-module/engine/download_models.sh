#!/usr/bin/env bash
# Fetches the VIS-IrisFormer checkpoint (453 MB, Git LFS in the upstream repo,
# too large for this repo) and checks its SHA-256.
set -euo pipefail
cd "$(dirname "$0")/models"
URL=https://media.githubusercontent.com/media/Mervin0266/LifeLink_Iris_module/main/deployments/pi5/models/sota.pth
SHA=6b6f710cfce20f7180e4d5e909e1ff9321748485172f23ce0fe708f2cd7c98db
if [ ! -f sota.pth ] || ! echo "$SHA  sota.pth" | sha256sum -c --quiet; then
  curl -fL -o sota.pth.part "$URL"
  echo "$SHA  sota.pth.part" | sha256sum -c --quiet
  mv sota.pth.part sota.pth
fi
echo "sota.pth OK"
