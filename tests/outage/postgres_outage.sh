#!/usr/bin/env bash
# ADR-0019: stale-while-error auth, proved against a REAL Postgres outage.
#
# This lives outside pytest because the suite runs inside the api container, which has no
# docker socket and so cannot stop a sibling container. Mocking the outage was the
# alternative, and ADR-0018 warns that a path which runs only during an outage is the
# least-tested code in the system -- mocking it would leave that warning standing.
#
# Run from the host:  make test-outage
set -uo pipefail
cd "$(dirname "$0")/../.."

PASS=0; FAIL=0
check() { # check <label> <expected> <actual>
  if [ "$2" = "$3" ]; then printf '  \033[32mPASS\033[0m %-58s %s\n' "$1" "$3"; PASS=$((PASS+1))
  else printf '  \033[31mFAIL\033[0m %-58s got %s, want %s\n' "$1" "$3" "$2"; FAIL=$((FAIL+1)); fi
}
code() { curl -s -o /dev/null -w '%{http_code}' "$@"; }

KEY=$(grep DEV_API_KEY .env | cut -d= -f2)
API=http://localhost:8000

restore() {
  docker compose start postgres >/dev/null 2>&1
  for _ in $(seq 1 60); do
    [ "$(code "$API/readyz")" = "200" ] && break
    sleep 1
  done
}
trap restore EXIT

echo "ADR-0019: auth during a Postgres outage"
echo

check "warm the cache while Postgres is up" 200 "$(code -H "X-API-Key: $KEY" "$API/v1/echo")"

DIGEST=$(docker compose exec -T api python -c \
  "from meter.storage.repositories import keys; import os; print(keys.hash_key(os.environ['DEV_API_KEY']))" \
  2>/dev/null | tr -d '\r')
CACHE_KEY="meter:auth:$DIGEST"

docker compose stop postgres >/dev/null 2>&1
sleep 2
echo "  -- postgres stopped --"

check "a still-FRESH key keeps working" 200 "$(code -H "X-API-Key: $KEY" "$API/v1/echo")"

# Age the entry past its freshness deadline, keeping the stale ceiling (the Redis TTL).
VALUE=$(docker compose exec -T redis redis-cli GET "$CACHE_KEY" | tr -d '\r')
docker compose exec -T redis redis-cli SET "$CACHE_KEY" \
  "$(echo "$VALUE" | rev | cut -d'|' -f2- | rev)|1" KEEPTTL >/dev/null
echo "  -- freshness deadline expired, entry retained --"

check "a STALE known key is served (the point of ADR-0019)" 200 \
  "$(code -H "X-API-Key: $KEY" "$API/v1/echo")"
# Checked here, directly after the stale request: the 5xx responses below emit tracebacks
# that would push this line out of any reasonable tail window. --tail rather than --since,
# because the host and container clocks skew enough for a 60s window to miss entirely.
if docker compose logs api --tail 200 2>&1 | grep -q "DEGRADED: serving a stale auth entry"; then
  printf '  \033[32mPASS\033[0m %-58s logged\n' "degraded mode is announced, not silent"; PASS=$((PASS+1))
else
  printf '  \033[31mFAIL\033[0m %-58s no warning\n' "degraded mode is announced, not silent"; FAIL=$((FAIL+1))
fi

check "an UNKNOWN key is never served stale" 503 \
  "$(code -H "X-API-Key: not-a-real-key" "$API/v1/echo")"
# ADR-0019 is explicit that "we kept serving" is true only for customers already cached.
# Any 5xx proves onboarding stopped; the admin surface returns 500 rather than 503, which is
# less precise than the metered surface but is not worth a branch on a demo-only endpoint.
ONBOARD=$(code -X POST -H 'Content-Type: application/json' \
    -d '{"name":"during-the-outage","plan":"Growth"}' "$API/admin/customers")
check "onboarding stops, as ADR-0019 admits it must" 5xx "$(echo "$ONBOARD" | sed 's/^5..$/5xx/')"

restore
trap - EXIT
echo "  -- postgres restored --"
check "service recovers once the directory returns" 200 "$(code -H "X-API-Key: $KEY" "$API/v1/echo")"

echo
echo "  $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
