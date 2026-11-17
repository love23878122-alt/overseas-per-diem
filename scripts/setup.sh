#!/bin/zsh
# Create/repair the skill's private Python env (PyMuPDF + Playwright driving the installed Google Chrome).
set -e
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q --disable-pip-version-check pymupdf playwright
.venv/bin/python -c "import pymupdf, playwright; print('ok: pymupdf', pymupdf.VersionBind)"
# Uses channel=chrome (installed Google Chrome). If Chrome is missing, install Playwright's Chromium:
[ -d "/Applications/Google Chrome.app" ] || .venv/bin/python -m playwright install chromium
