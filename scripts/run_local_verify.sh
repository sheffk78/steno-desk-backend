#!/usr/bin/env bash
# Self-contained local integration boot for StenoDesk backend + tests.
# Usage: bash scripts/run_local_verify.sh   (from repo root)
# Boots uvicorn on TEST_PORT (default 8007) with a full env, waits for
# health, runs pytest, then cleans up the server.
set -euo pipefail
cd "$(dirname "$0")/.."

TEST_PORT="${TEST_PORT:-8007}"
REPO="$(pwd)"
export TEST_BASE_URL="http://localhost:${TEST_PORT}"
export MONGO_URL="${MONGO_URL:-mongodb://localhost:27017/stenodesk_local_verify}"
export DB_NAME="${DB_NAME:-stenodesk_local_verify}"
export JWT_SECRET="${JWT_SECRET:-local-verify-secret-oct8-not-for-prod-0001}"
export FRONTEND_URL="${FRONTEND_URL:-http://localhost:3000}"
export POSTMARK_SERVER_TOKEN="${POSTMARK_SERVER_TOKEN:-fake}"
export ADMIN_EMAILS="${ADMIN_EMAILS:-support@stenodesk.co}"
export POSTMARK_WEBHOOK_TOKEN="${POSTMARK_WEBHOOK_TOKEN:-jfbF1wuUKwh5rBW8vSpDFPT27SWWZWjG6gX04GMukDo}"
# Live-mode price IDs (identifiers, not secrets) so checkout legs work out of the box.
export STRIPE_PRICE_MONTHLY="${STRIPE_PRICE_MONTHLY:-price_1U5aFvJE7N1BszdfmWTYF8jc}"
export STRIPE_PRICE_ANNUAL="${STRIPE_PRICE_ANNUAL:-price_1U5aFxJE7N1BszdfgAQEYaOA}"
export STRIPE_PRICE_FOUNDING="${STRIPE_PRICE_FOUNDING:-price_1UOLaZJE7N1Bszdf0M0uJ2AA}"
export DISABLE_SCHEDULER=1

# Stripe: use real keys if provided via env or the Hermes secrets file, else
# none — Stripe-dependent legs skip/fail-close honestly.
if [ -z "${STRIPE_SECRET_KEY:-}" ] && [ -f "$HOME/.hermes/secrets/stripe-trustoffice-agentic.json" ]; then
  STRIPE_SECRET_KEY="$(/usr/bin/python3 -c "import json;print(json.load(open('$HOME/.hermes/secrets/stripe-trustoffice-agentic.json'))['secret_key'])")"
  export STRIPE_SECRET_KEY
fi
if [ -z "${STRIPE_WEBHOOK_SECRET:-}" ] && [ -f "$HOME/.hermes/secrets/stripe-stenodesk-agentic.json" ]; then
  STRIPE_WEBHOOK_SECRET="$(/usr/bin/python3 -c "import json;print(json.load(open('$HOME/.hermes/secrets/stripe-stenodesk-agentic.json'))['webhook_secret'])")"
  export STRIPE_WEBHOOK_SECRET
fi
for V in STRIPE_PRICE_MONTHLY STRIPE_PRICE_ANNUAL STRIPE_PRICE_FOUNDING STRIPE_WEBHOOK_SECRET; do
  if [ -n "${!V:-}" ]; then export "$V"; fi
done
if [ -n "${STRIPE_SECRET_KEY:-}" ]; then export SERVER_STRIPE_KEY_PRESENT=1; fi
if [ -n "${STRIPE_WEBHOOK_SECRET:-}" ]; then export SERVER_STRIPE_WEBHOOK_PRESENT=1; fi

# Dedicated venv by default — user-site packages make bare-interpreter
# detection unreliable (fastapi present, boto3/motor missing etc.).
PYBIN="${PYTHON_BIN:-/tmp/sd_venv_sdverify/bin/python}"
if [ ! -x "$PYBIN" ] || ! "$PYBIN" -c "import fastapi, motor, boto3, pytest" 2>/dev/null; then
  echo "Building verify venv at /tmp/sd_venv_sdverify (py3.14)"
  rm -rf /tmp/sd_venv_sdverify
  /opt/homebrew/bin/python3.14 -m venv /tmp/sd_venv_sdverify
  /tmp/sd_venv_sdverify/bin/pip install --quiet --upgrade pip -r requirements.txt pytest pymongo
  PYBIN=/tmp/sd_venv_sdverify/bin/python
fi

lsof -ti tcp:"$TEST_PORT" | xargs kill -9 2>/dev/null || true
echo "Booting server on :$TEST_PORT"
env MONGO_URL="$MONGO_URL" DB_NAME="$DB_NAME" JWT_SECRET="$JWT_SECRET" FRONTEND_URL="$FRONTEND_URL" \
  POSTMARK_SERVER_TOKEN="$POSTMARK_SERVER_TOKEN" ADMIN_EMAILS="$ADMIN_EMAILS" \
  POSTMARK_WEBHOOK_TOKEN="$POSTMARK_WEBHOOK_TOKEN" DISABLE_SCHEDULER=1 \
  ${STRIPE_SECRET_KEY:+STRIPE_SECRET_KEY="$STRIPE_SECRET_KEY"} \
  ${STRIPE_PRICE_MONTHLY:+STRIPE_PRICE_MONTHLY="$STRIPE_PRICE_MONTHLY"} \
  ${STRIPE_PRICE_ANNUAL:+STRIPE_PRICE_ANNUAL="$STRIPE_PRICE_ANNUAL"} \
  ${STRIPE_PRICE_FOUNDING:+STRIPE_PRICE_FOUNDING="$STRIPE_PRICE_FOUNDING"} \
  ${STRIPE_WEBHOOK_SECRET:+STRIPE_WEBHOOK_SECRET="$STRIPE_WEBHOOK_SECRET"} \
  SERVER_STRIPE_KEY_PRESENT="$([ -n "${STRIPE_SECRET_KEY:-}" ] && echo 1 || echo 0)" \
  "$PYBIN" -m uvicorn server:app --host 127.0.0.1 --port "$TEST_PORT" &
SERVER_PID=$!

trap 'kill -9 $SERVER_PID 2>/dev/null || true' EXIT

for i in $(seq 1 30); do
  sleep 1
  if curl -sf "http://localhost:${TEST_PORT}/api/health" >/dev/null 2>&1; then break; fi
  if ! kill -0 $SERVER_PID 2>/dev/null; then echo "server died"; exit 1; fi
done
curl -sf "http://localhost:${TEST_PORT}/api/health"; echo " <- healthy"

PYTHONPATH="$REPO" SERVER_STRIPE_KEY_PRESENT="$([ -n "${STRIPE_SECRET_KEY:-}" ] && echo 1 || echo 0)" \
  MONGO_URL="$MONGO_URL" DB_NAME="$DB_NAME" JWT_SECRET="$JWT_SECRET" \
  "$PYBIN" -m pytest tests -p no:warnings -q
RC=$?
exit $RC