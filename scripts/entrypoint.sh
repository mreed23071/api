#!/usr/bin/env bash
# Applies pending Alembic migrations, then hands the container over to the CMD.
# Running migrations here (rather than from the app's lifespan) keeps schema
# changes out of the request path and lets us scale the API to N replicas with
# a single migration job if we ever move to Kubernetes.
set -euo pipefail

if [[ "${RUN_MIGRATIONS:-1}" == "1" ]]; then
    echo "[entrypoint] applying database migrations..."
    alembic upgrade head
fi

# Demo data, off by default. The compose stack turns it on so that one
# command gives a database somebody can actually look at; nothing else should.
# `scripts/seed.py` is idempotent, so a restarted container re-runs it harmlessly.
if [[ "${SEED_ON_STARTUP:-0}" == "1" ]]; then
    echo "[entrypoint] seeding demo data..."
    python scripts/seed.py
fi

echo "[entrypoint] starting: $*"
exec "$@"
