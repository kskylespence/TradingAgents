#!/usr/bin/env bash
# Run every check .github/workflows/ci.yml defines, on this machine.
#
# GitHub Actions minutes are paid for, so CI runs here, not on push: ci.yml is
# manual-only and the pre-push hook (.githooks/pre-push) calls this script.
# Keep the two in step: a job added to ci.yml gets a step here.
#
# Every step ends PASS, FAIL or DID-NOT-RUN. A missing tool is a FAIL, and a
# step that did not run counts against the result: exit 0 means every step ran
# and passed. Logs: $CI_LOCAL_DIR/logs/<step>.log (default under $TMPDIR).
#
# Needs: uv, node 24 + npm, Docker Desktop running. Takes about 6 minutes.
# Usage: scripts/ci-local.sh            (bash 3.2 compatible, macOS default)

set -u
cd "$(git rev-parse --show-toplevel)" || exit 2

UV=${UV:-$(command -v uv || echo "$HOME/.local/bin/uv")}
DOCKER=${DOCKER:-docker}
WORK=${CI_LOCAL_DIR:-${TMPDIR:-/tmp}/tradingagents-ci}
LOGS="$WORK/logs"
rm -rf "$LOGS" && mkdir -p "$LOGS"

NAMES=() OUTCOMES=() REASONS=()
record() { NAMES+=("$1"); OUTCOMES+=("$2"); REASONS+=("$3"); printf '%-28s %s %s\n' "$1" "$2" "$3"; }

# step NAME CMD...: run CMD with output to the step's log, then record the outcome.
step() {
  local name=$1; shift
  local log="$LOGS/$name.log"
  if "$@" >"$log" 2>&1; then
    record "$name" PASS ""
  else
    record "$name" FAIL "(exit $?; tail of $log:)"
    tail -n 15 "$log" | sed 's/^/    /'
  fi
}

# A pytest run that collected nothing proves nothing: require a passed count.
pytest_counted() {
  "$@" && tail -n 1 "$PYTEST_LOG" | grep -Eq '[1-9][0-9]* passed'
}

missing=""
command -v "$UV" >/dev/null 2>&1 || missing="$missing uv"
command -v npm >/dev/null 2>&1 || missing="$missing npm"
"$DOCKER" info >/dev/null 2>&1 || missing="$missing docker(running)"
if [ -n "$missing" ]; then
  record tools FAIL "missing:$missing"
else
  record tools PASS ""
fi
have() { case " $missing " in *" $1"*) return 1;; esac; return 0; }

venv() {  # venv DIR PYTHON INSTALL-ARGS...
  local dir=$1 py=$2; shift 2
  "$UV" venv -q --clear --python "$py" "$dir" && "$UV" pip install -q --python "$dir/bin/python" "$@"
}

# --- tests job: root package only, py3.11-3.14, plus one run in New York ----
for row in 3.11:UTC 3.12:UTC 3.13:UTC 3.14:UTC 3.12:America/New_York; do
  v=${row%%:*} tz=${row#*:}
  name="root-py$v"; [ "$tz" = UTC ] || name="root-py$v-NY"
  if ! have uv; then record "$name" DID-NOT-RUN "uv missing"; continue; fi
  if [ "$tz" = UTC ]; then step "venv-py$v" venv "$WORK/py$v" "$v" -e '.[dev]'; fi
  PYTEST_LOG="$LOGS/$name.log"
  step "$name" pytest_counted env TZ="$tz" "$WORK/py$v/bin/python" -m pytest -q -p no:cacheprovider
done

# --- web-backend job ---------------------------------------------------------
if have uv; then
  step venv-backend venv "$WORK/backend" 3.12 -e . -e 'web/backend[dev]'
  PYTEST_LOG="$LOGS/web-backend.log"
  step web-backend pytest_counted sh -c "cd web/backend && '$WORK/backend/bin/python' -m pytest -q -p no:cacheprovider"
else
  record web-backend DID-NOT-RUN "uv missing"
fi

# --- web-frontend job ----------------------------------------------------------
if have npm; then
  step frontend sh -c 'cd web/frontend && npm ci --no-fund --no-audit && npm run lint && npm test && npm run build'
else
  record frontend DID-NOT-RUN "npm missing"
fi

# --- audit job -------------------------------------------------------------------
if have uv; then
  step audit-python "$UV" tool run pip-audit -r requirements.lock --require-hashes --disable-pip
else
  record audit-python DID-NOT-RUN "uv missing"
fi
if have npm; then
  step audit-npm sh -c 'cd web/frontend && npm audit --audit-level=moderate'
else
  record audit-npm DID-NOT-RUN "npm missing"
fi

# --- docker job (plus the version check RELEASING.md section 4 adds) --------------
if have docker; then
  step docker-build "$DOCKER" build --tag tradingagents:ci .
  step docker-version sh -c "'$DOCKER' run --rm --entrypoint python tradingagents:ci -c 'import tradingagents, app; print(tradingagents.__version__, app.__version__)'"
else
  record docker-build DID-NOT-RUN "docker not running"
fi

# --- smoke-install job: no dev extras, imported from outside the repo ------------
if have uv; then
  step smoke-install sh -c "'$UV' venv -q --clear --python 3.12 '$WORK/smoke' \
    && '$UV' pip install -q --python '$WORK/smoke/bin/python' . \
    && cd '$WORK' && '$WORK/smoke/bin/python' -c 'import tradingagents, cli.main'"
  # --- lint job: the latest ruff >=0.16, as ci.yml installs it ------------------
  step lint "$UV" tool run --from 'ruff>=0.16' ruff check .
else
  record smoke-install DID-NOT-RUN "uv missing"
  record lint DID-NOT-RUN "uv missing"
fi

pass=0 fail=0 dnr=0
for o in "${OUTCOMES[@]}"; do
  case $o in PASS) pass=$((pass+1));; FAIL) fail=$((fail+1));; *) dnr=$((dnr+1));; esac
done
echo
echo "local CI: $pass PASS, $fail FAIL, $dnr DID-NOT-RUN  (logs: $LOGS)"
for i in "${!NAMES[@]}"; do
  [ "${OUTCOMES[$i]}" = PASS ] || echo "  ${OUTCOMES[$i]}: ${NAMES[$i]} ${REASONS[$i]}"
done
[ "$fail" -eq 0 ] && [ "$dnr" -eq 0 ]
