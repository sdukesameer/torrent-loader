#!/usr/bin/env bash
# Build script for Render.com – installs libtorrent from apt then Python deps
set -e

apt-get update -qq
apt-get install -y -qq python3-libtorrent

pip install -r requirements.txt --break-system-packages
