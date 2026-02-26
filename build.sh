#!/usr/bin/env bash
# Build script for Render.com
set -e

# python-libtorrent has prebuilt wheels for Python 3.11 on Linux x86_64
pip install python-libtorrent flask gunicorn
