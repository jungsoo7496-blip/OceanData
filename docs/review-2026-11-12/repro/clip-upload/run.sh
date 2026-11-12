#!/bin/bash
# usage: run.sh <test file> [pytest args...]
cd /home/user/oceandata/backend
export PYTHONPATH=$PWD TEST_DATABASE_URL=postgresql+pg8000://marine_owner:change-me-owner@127.0.0.1:5432/marine_rv12_clip
f="$1"; shift
timeout 900 .venv/bin/pytest -p tests.conftest -p no:cacheprovider -p no:logging -q -s --rootdir=$PWD -c pyproject.toml "$f" "$@" 2>&1 | grep -E "OBS|passed|failed|^E  |Error|FAILED" | sed -e "s/^.*OBS/OBS/"
