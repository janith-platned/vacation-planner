#!/usr/bin/env bash
# Render build step: install, collect static files, migrate, and (demo only) seed data.
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate --no-input

if [ "${DEMO_SEED:-0}" = "1" ]; then
  python manage.py seed_demo
fi
