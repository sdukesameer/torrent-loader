#!/usr/bin/env bash
# Build script for Render.com
set -e

# 'libtorrent' on PyPI has a prebuilt cp312 manylinux wheel — matches Python 3.12
pip install libtorrent==2.0.11 flask gunicorn
