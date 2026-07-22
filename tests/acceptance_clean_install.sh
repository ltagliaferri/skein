#!/usr/bin/env bash
# Clean-environment acceptance for the public `interskein` wheel.
#
# Everything else in tests/ runs against the source tree. This runs against the
# artifact a stranger would actually get: build the wheel, install ONLY that
# wheel with `uv tool install`, and drive the whole first-run path — start
# `skein-server`, pass `skein doctor`, initialize a project, create a site, post
# a folio, find it, read it back.
#
# It is a shell script rather than a pytest case because the thing under test is
# a separate installed interpreter; importing the package would defeat the point.
#
# Isolation: its own uv tool dir, its own SKEIN_HOME, its own port, and it starts
# and reaps its own service. It cannot touch a real ~/.skein, a real service, or
# a real PATH. Nothing is left behind.
#
# Usage:  bash tests/acceptance_clean_install.sh
# Needs:  uv, git, python with `build`, and network access for the isolated build.
set -uo pipefail

REPO="${REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
RUN="${RUN:-$(mktemp -d -t interskein-acceptance-XXXXXX)}"
SERVICE_PID=""

fail() { echo "ACCEPTANCE FAILED: $*" >&2; exit 1; }
step() { echo; echo "=== $* ==="; }
expect_exit() {
    local want="$1"; shift
    "$@"; local got=$?
    [[ "$got" == "$want" ]] || fail "expected exit $want from '$*', got $got"
}

export SKEIN_HOME="$RUN/skein-home"
export UV_TOOL_DIR="$RUN/uv-tools"
export UV_TOOL_BIN_DIR="$RUN/bin"
export PATH="$UV_TOOL_BIN_DIR:$PATH"
mkdir -p "$SKEIN_HOME" "$UV_TOOL_DIR" "$UV_TOOL_BIN_DIR" "$RUN/work"

PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
export SKEIN_URL="http://127.0.0.1:$PORT"
export SKEIN_PORT="$PORT"
export SKEIN_AGENT_ID="acceptance-agent"

cleanup() {
    echo
    echo "=== cleanup ==="
    [[ -n "$SERVICE_PID" ]] && kill "$SERVICE_PID" 2>/dev/null
    uv tool uninstall interskein >/dev/null 2>&1
    rm -rf "$RUN"
}
trap cleanup EXIT

echo "repo:      $REPO"
echo "scratch:   $RUN"
echo "port:      $PORT"

step "build the wheel"
(cd "$REPO" && python -m build --wheel --outdir "$RUN/dist") >"$RUN/build.log" 2>&1 \
    || { tail -20 "$RUN/build.log"; fail "wheel build"; }
WHEEL="$(ls "$RUN"/dist/interskein-*.whl)"
echo "wheel: $WHEEL"

step "install with uv tool install (wheel only, isolated tool dir)"
uv tool install --force "$WHEEL" 2>&1 | tail -3 || fail "uv tool install"
for cmd in skein skein-server mesh; do
    command -v "$cmd" >/dev/null || fail "$cmd not on PATH after install"
done
echo "skein resolves to: $(command -v skein)"
case "$(command -v skein)" in
    "$UV_TOOL_BIN_DIR"/*) ;;
    *) fail "PATH resolved to an install other than the one under test" ;;
esac

step "run from a directory with no source checkout in sight"
cd "$RUN/work" || fail "cd"
skein --version || fail "skein --version"
skein-server --version || fail "skein-server --version"

step "doctor before the service is up (must fail)"
expect_exit 1 skein doctor

step "start the packaged service"
# Exactly what a systemd unit's ExecStart does, backgrounded here.
skein-server >"$RUN/service.log" 2>&1 &
SERVICE_PID=$!
for _ in $(seq 1 120); do
    curl -fsS "$SKEIN_URL/health" >/dev/null 2>&1 && break
    kill -0 "$SERVICE_PID" 2>/dev/null || { tail -20 "$RUN/service.log"; fail "service died on startup"; }
    sleep 0.25
done
curl -fsS "$SKEIN_URL/health" || fail "service never answered /health"
echo

step "the service runs the installed code, not a checkout"
python3 - "$RUN/service.log" "$REPO" <<'PY' || fail "service provenance"
import re, sys
log = open(sys.argv[1], errors="replace").read()
repo = sys.argv[2]
assert repo not in log, f"service log references the source checkout {repo}"
print("service log clean of the source tree")
PY

step "doctor with the service up (must pass)"
expect_exit 0 skein doctor

step "packaged docs"
[[ "$(skein info quickstart | wc -l)" -gt 100 ]] || fail "quickstart doc missing or truncated"
skein info guide >/dev/null || fail "skein info guide"
skein info implementation >/dev/null || fail "skein info implementation"

step "disposable git project"
mkdir -p "$RUN/work/demo-project"
cd "$RUN/work/demo-project" || fail "cd project"
git init -q . || fail "git init"
skein init --project acceptance-demo || fail "skein init"
expect_exit 0 skein doctor

step "create a site"
skein site create notes "Acceptance notes" || fail "site create"
skein sites >/dev/null || fail "sites"

step "post a finding"
POST_OUT="$(skein post finding notes "Wheel install works" \
    -d "Installed from the built wheel with uv tool install; service run as skein-server.")" \
    || fail "post finding"
echo "$POST_OUT"
FOLIO_ID="$(echo "$POST_OUT" | grep -oE '[a-z]+-[0-9]{8}-[a-z0-9]+' | head -1)"
[[ -n "$FOLIO_ID" ]] || fail "could not parse folio id from: $POST_OUT"

step "find it"
skein find "Wheel install works" >"$RUN/find.out" || fail "find"
grep -q "$FOLIO_ID" "$RUN/find.out" || fail "find did not return $FOLIO_ID"

step "read it back"
skein folio "$FOLIO_ID" >"$RUN/folio.out" || fail "folio read"
grep -q "skein-server" "$RUN/folio.out" || fail "folio content missing"
skein folio "$FOLIO_ID" --json >"$RUN/folio.json" || fail "folio --json"
python3 - "$RUN/folio.json" "$FOLIO_ID" <<'PY' || fail "folio round-trip"
import json, sys
folio = json.load(open(sys.argv[1]))
assert folio["folio_id"] == sys.argv[2], folio
assert folio["title"] == "Wheel install works", folio
assert folio["type"] == "finding", folio
assert "skein-server" in folio["content"], folio
print("folio round-trip ok:", folio["folio_id"], "|", folio["title"])
PY

step "doctor reports agreeing versions"
skein doctor --json | python3 -c "
import json, sys
payload = json.load(sys.stdin)
checks = {c['name']: c for c in payload['checks']}
assert payload['healthy'] is True, payload
assert checks['version match']['ok'] is True, checks['version match']
assert checks['api service']['ok'] is True, checks['api service']
assert checks['packaged docs']['ok'] is True, checks['packaged docs']
print('version match:', checks['version match']['detail'])
" || fail "doctor json"

step "stop the service"
kill "$SERVICE_PID" || fail "kill service"
wait "$SERVICE_PID" 2>/dev/null
SERVICE_PID=""
expect_exit 1 skein doctor

echo
echo "ACCEPTANCE PASSED"
