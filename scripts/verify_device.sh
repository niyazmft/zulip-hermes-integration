#!/usr/bin/env bash
#
# verify_device.sh — prove this plugin works on a *deployed* Hermes host.
#
# Why this exists
# ---------------
# A device can be in four states, and the interesting ones are the mismatched
# ones: new Hermes + old plugin, or old Hermes + new plugin. Those are the states
# a real user is in on the day they upgrade one half and not the other, and they
# are the states this project's two fixtures exist to cover. Checking them by
# hand has already gone wrong three ways in this repo:
#
#   * `... | tail -n; echo $?` reports tail's status, so a failed gate looks green
#   * `import zulip` resolving to site-packages grades the *installed* plugin while
#     the operator believes they graded the working tree
#   * an editable install reports host version 0.0.0 from importlib.metadata while
#     `hermes --version` says 0.22.5 — trust the wrong one and you grade the wrong host
#
# Each of those produces a confident PASS that proves nothing. So this script
# captures exit codes directly, puts the staged tree first on sys.path, reads the
# host version from the gate's own detection, and cross-checks it against
# `hermes --version`.
#
# It is READ-ONLY with respect to the deployed plugin: the working tree is staged
# into a scratch directory, the gate runs there, and the gateway is never
# restarted. Deploying is a separate, deliberate act (`update.sh`, or rsync).
#
# Usage
#   scripts/verify_device.sh docker:<container>    # e.g. docker:lab-hermes
#   scripts/verify_device.sh ssh:<host>            # e.g. ssh:8x
#   scripts/verify_device.sh --matrix              # every configured device, as the matrix
#   scripts/verify_device.sh --help
#
# Environment
#   ZULIP_VERIFY_DEVICES    space-separated targets for --matrix
#                           (default: "docker:lab-hermes ssh:8x" — this repo's fixtures)
#   ZULIP_VERIFY_PLUGIN     override the installed-plugin path on the device
#   ZULIP_VERIFY_HOSTPATH   override the Hermes install dir (HERMES_COMPAT_PATH)
#   ZULIP_VERIFY_WORK       override the scratch directory staged on the device
#
# Exit codes
#   0  every check passed
#   1  a check failed (the failing step is named, with its output)
#   2  usage error / device unreachable
#
set -uo pipefail

REPO_ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
WORK_NAME=zulip-verify
REPO_VERSION=$(sed -n 's/^__version__ *= *"\(.*\)"/\1/p' "$REPO_ROOT/zulip/version.py")

# The host version at which the native exec-approval buttons first appeared. Above
# it a capability is present; below it the plugin must degrade rather than fail.
CAPABILITY_BOUNDARY_MAJOR=0
CAPABILITY_BOUNDARY_MINOR=21
CAPABILITY_BOUNDARY_PATCH=3

if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
  C_RED=''; C_GRN=''; C_YEL=''; C_DIM=''; C_OFF=''
fi

STEP=0
FAILED=0

step()  { STEP=$((STEP + 1)); printf '  %d. %-24s ' "$STEP" "$1"; }
pass()  {
  if [ -n "${1:-}" ]; then
    printf '%sok%s (%s)\n' "$C_GRN" "$C_OFF" "$1"
  else
    printf '%sok%s\n' "$C_GRN" "$C_OFF"
  fi
}
okn()   { printf '%sok%s' "$C_GRN" "$C_OFF"; }
warn()  { printf '%swarn%s%s\n' "$C_YEL" "$C_OFF" "${1:+ ($1)}"; }
bad()   { printf '%sFAIL%s%s\n' "$C_RED" "$C_OFF" "${1:+ ($1)}"; FAILED=$((FAILED + 1)); }
note()  { printf '     %s%s%s\n' "$C_DIM" "$1" "$C_OFF"; }
die()   { printf '%sverify_device: %s%s\n' "$C_RED" "$*" "$C_OFF" >&2; exit 2; }

usage() { sed -n '2,62p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

# --- device abstraction -----------------------------------------------------
#
# One transport at a time: docker (a container on this host) or ssh (a remote
# device). Everything talks to the device through `rq` (run a shell snippet there)
# and `send_tree` (push the working tree to its scratch dir).

KIND=; NAME=; HOSTPATH=; PLUGIN=; PY=; WORK=; LOG=
HOST_CLI_VERSION=; PLUGIN_VERSION=; GATE_HOST=

rq() { # rq <shell snippet> — run it on the device
  case $KIND in
    docker) docker exec -u "${DOCKER_USER:-hermes}" "$NAME" sh -c "$1" ;;
    ssh)    ssh -o BatchMode=yes -o ConnectTimeout=10 "$NAME" "sh -c $(printf '%q' "$1")" ;;
  esac
}

target_up() {
  case $KIND in
    docker) [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = "true" ] ;;
    ssh)    ssh -o BatchMode=yes -o ConnectTimeout=10 "$NAME" true >/dev/null 2>&1 ;;
  esac
}

discover_python() {
  # The interpreter that actually has the host installed. An editable install can
  # report 0.0.0 from importlib.metadata, so the launcher's own exec line is the
  # reliable source; then the conventional venv paths; then bare python3.
  local from_launcher candidate
  from_launcher=$(rq 'l=$(command -v hermes 2>/dev/null) || l=""
[ -n "$l" ] || exit 0
grep -oE "\"[^\"]*python3[^\"]*\"" "$l" 2>/dev/null | head -1 | tr -d "\""')
  if [ -n "$from_launcher" ]; then printf '%s\n' "$from_launcher"; return; fi
  for candidate in "$HOSTPATH/.venv/bin/python3" "$HOSTPATH/venv/bin/python3"; do
    if [ -n "$(rq "[ -x '$candidate' ] && echo yes")" ]; then printf '%s\n' "$candidate"; return; fi
  done
  rq 'command -v python3'
}

resolve_target() {
  case $1 in
    docker:*) KIND=docker; NAME=${1#docker:} ;;
    ssh:*)    KIND=ssh;    NAME=${1#ssh:} ;;
    "")       die "no target: use docker:<container> or ssh:<host>" ;;
    *)        die "unknown target '$1': use docker:<container> or ssh:<host>" ;;
  esac

  target_up || die "cannot reach $KIND target '$NAME'"

  if [ "$KIND" = docker ]; then
    DOCKER_USER=${ZULIP_VERIFY_DOCKER_USER:-hermes}
    HOSTPATH=${ZULIP_VERIFY_HOSTPATH:-/opt/hermes}
    PLUGIN=${ZULIP_VERIFY_PLUGIN:-/opt/data/plugins/zulip}
    LOG=${ZULIP_VERIFY_LOG:-/opt/data/logs/gateway.log}
    WORK_BASE=${ZULIP_VERIFY_WORK:-/tmp/$WORK_NAME}
  else
    # A remote device may have a read-only /tmp (Termux/Android does), so stage
    # under the home directory rather than guessing at /tmp.
    HOSTPATH=${ZULIP_VERIFY_HOSTPATH:-$(rq 'echo "$HOME/.hermes/hermes-agent"')}
    PLUGIN=${ZULIP_VERIFY_PLUGIN:-$(rq 'echo "$HOME/.hermes/plugins/zulip"')}
    LOG=${ZULIP_VERIFY_LOG:-$(rq 'echo "$HOME/.hermes/logs/gateway.log"')}
    WORK_BASE=${ZULIP_VERIFY_WORK:-$(rq 'echo "$HOME/.cache/'$WORK_NAME'"')}
  fi

  # A unique directory per run. A fixed path left behind by an earlier root-owned
  # staging run is not removable by the runtime user, which wedges the next run.
  WORK="$WORK_BASE/run.$$.${RANDOM:-0}"
  trap 'rq "rm -rf '"'"'$WORK'"'"'" >/dev/null 2>&1 || true' EXIT

  PY=$(discover_python)
  [ -n "$PY" ] || die "could not find a python on the device"
}

send_tree() {
  rq "rm -rf '$WORK' && mkdir -p '$WORK'" || return 1
  # COPYFILE_DISABLE stops macOS bsdtar emitting AppleDouble/xattr headers that
  # GNU tar on the device reports as errors.
  COPYFILE_DISABLE=1 tar czf - -C "$REPO_ROOT" --exclude='*__pycache__*' \
    scripts/check_compat.py checksums.txt zulip 2>/dev/null \
    | case $KIND in
        docker) docker exec -i -u "${DOCKER_USER:-hermes}" "$NAME" tar xzf - -C "$WORK" ;;
        ssh)    ssh -o BatchMode=yes -o ConnectTimeout=10 "$NAME" "tar xzf - -C '$WORK' 2>'$WORK/.tar.log'" ;;
      esac
}

# --- checks -----------------------------------------------------------------

is_old_host() {
  # 0.21.3 is the last host version that changed a capability the plugin reaches
  # into (native exec-approval buttons, and the typing helper pair).
  local v=$1 ma mi pa
  IFS=. read -r ma mi pa <<< "$v"
  pa=${pa%%[!0-9]*}
  [ -n "${ma:-}" ] && [ -n "${mi:-}" ] || return 1
  if [ "$ma" -eq 0 ] && { [ "$mi" -lt 21 ] || { [ "$mi" -eq 21 ] && [ "${pa:-0}" -lt 3 ]; }; }; then
    return 0
  fi
  return 1
}

check_identity() {
  local cli
  cli=$(rq 'hermes --version 2>/dev/null | head -1')
  PLUGIN_VERSION=$(rq "sed -n 's/^__version__ *= *\"\\(.*\\)\"/\\1/p' '$PLUGIN/version.py' 2>/dev/null" | head -1)
  HOST_CLI_VERSION=$(printf '%s' "$cli" | sed -n 's/.*[vV]\([0-9][0-9]*\.[0-9][0-9.]*\).*/\1/p' | head -1)
  printf '  %s%s%s\n' "$C_DIM" "${cli:-hermes: version unavailable}" "$C_OFF"
  printf '  %splugin %s · %s%s\n' "$C_DIM" "${PLUGIN_VERSION:-?}" "$PY" "$C_OFF"
}

check_identity_versions() {
  # Grading a host that reports 0.0.0 is grading the wrong thing, so the check is
  # on the *versions*, not just on the CLI banner above.
  step "identity versions"
  if [ -z "${PLUGIN_VERSION:-}" ] || [ -z "${HOST_CLI_VERSION:-}" ]; then
    bad "could not read plugin version (${PLUGIN_VERSION:-none}) or host version (${HOST_CLI_VERSION:-none})"
    return 1
  fi
  pass "plugin $PLUGIN_VERSION, host $HOST_CLI_VERSION"
}

check_stage() {
  step "stage working tree"
  if send_tree; then pass "$WORK"; else
    bad "could not copy scripts/ + zulip/ to $WORK"; return 1
  fi
}

check_import() {
  step "import preflight"
  local out rc
  out=$(rq "cd '$WORK' && PYTHONPATH='$WORK' '$PY' -c 'import zulip.adapter, zulip.commands, zulip.cli, zulip.model_picker'" 2>&1)
  rc=$?
  if [ "$rc" -eq 0 ]; then okn; printf '\n'; else
    bad "the staged tree does not import against this host"
    printf '%s\n' "$out" | tail -5 | sed 's/^/     /'
    return 1
  fi
}

check_gate() {
  # The real-host contract check. The exit code is captured directly — never
  # through a pipe, which is how this check has reported the wrong command's
  # status before.
  step "compat gate"
  local rc
  rq "cd '$WORK' && HERMES_COMPAT_PATH='$HOSTPATH' PYTHONPATH='$WORK' '$PY' '$WORK/scripts/check_compat.py' > '$WORK/gate.log' 2>&1"
  rc=$?
  GATE_HOST=$(rq "sed -n 's/^check_compat: host Hermes \\([^ ]*\\) detected.*/\\1/p' '$WORK/gate.log'" | head -1)
  if [ "$rc" -eq 0 ]; then
    local absent
    absent=$(rq "grep -c 'absent' '$WORK/gate.log' 2>/dev/null" | head -1)
    okn; printf ' (host %s, %s capability(ies) absent)\n' "${GATE_HOST:-?}" "${absent:-0}"
  else
    bad "gate exited $rc"
    rq "sed -n '1,20p' '$WORK/gate.log'" | sed 's/^/     /'
    return 1
  fi
}

check_crosscheck() {
  step "host version cross-check"
  case ${GATE_HOST:-} in
    ""|0.0.0|unknown)
      bad "the gate detected '${GATE_HOST:-nothing}' — it graded the installed package, not this host"
      return 1 ;;
  esac
  if [ "$GATE_HOST" = "$HOST_CLI_VERSION" ]; then
    pass "gate and CLI agree: $GATE_HOST"
  else
    bad "gate detected $GATE_HOST but 'hermes --version' says $HOST_CLI_VERSION — one is not this host"
    return 1
  fi
}

check_deployed_checksums() {
  # Two different questions, and conflating them is what makes a matrix fixture
  # look broken:
  #
  #   * a device deliberately behind is a *supported cell*, not a failure -- so
  #     its plugin is reported, never graded against the working tree
  #   * a device claiming the same version as the tree must actually be intact,
  #     which is checkable because checksums.txt is staged alongside the code
  #     (it is not shipped to devices -- it is not in PLUGIN_FILES)
  step "deployed plugin"
  if [ -z "${PLUGIN_VERSION:-}" ]; then
    warn "no installed plugin at $PLUGIN"; return 0
  fi
  if [ "$PLUGIN_VERSION" != "$REPO_VERSION" ]; then
    warn "device runs $PLUGIN_VERSION, tree is $REPO_VERSION -- behind by design, not graded"
    return 0
  fi
  # Same version number, so the comparison is meaningful only if this tree is
  # what that version *is*. An uncommitted edit at a released version makes every
  # edited file "differ" -- true, and useless. Say so instead of failing.
  if git -C "$REPO_ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    local dirty
    dirty=$(git -C "$REPO_ROOT" status --porcelain -- zulip | wc -l | tr -d ' ')
    if [ "$dirty" != "0" ]; then
      warn "tree has $dirty uncommitted file(s) under zulip/ -- integrity not comparable"
      return 0
    fi
  fi
  local out rc count
  out=$(rq "cd '$PLUGIN' 2>/dev/null || { echo '__NOPLUGIN__'; exit 1; }
sha256sum -c '$WORK/checksums.txt' 2>&1")
  rc=$?
  case $out in
    *__NOPLUGIN__*) warn "no installed plugin at $PLUGIN"; return 0 ;;
  esac
  if [ "$rc" -ne 0 ]; then
    bad "device claims $PLUGIN_VERSION but its files differ from that release"
    printf '%s\n' "$out" | grep -v ': OK$' | head -6 | sed 's/^/     /'
    return 1
  fi
  count=$(printf '%s\n' "$out" | grep -c ': OK$')
  pass "$count files match $REPO_VERSION"
}

check_startup_log() {
  # Anchor on the last startup banner: timestamp-less traceback continuation lines
  # sort above digits, so a timestamp filter makes old tracebacks look current.
  step "startup log"
  local out
  out=$(rq "f='$LOG'
[ -f \"\$f\" ] || { echo '__NOLOG__'; exit 0; }
n=\$(grep -n 'Starting Hermes Gateway' \"\$f\" 2>/dev/null | tail -1 | cut -d: -f1)
[ -n \"\$n\" ] || { echo '__NOBANNER__'; exit 0; }
sed -n \"\${n},\\\$p\" \"\$f\" | grep -E 'zulip (connection established|probe ok|bot authenticated|drop|bot not subscribed)' | tail -5")
  case $out in
    __NOLOG__)    warn "no gateway log at $LOG"; return 0 ;;
    __NOBANNER__) warn "no startup banner yet at $LOG"; return 0 ;;
  esac
  if printf '%s' "$out" | grep -q 'connection established'; then
    okn; printf '\n'
    printf '%s\n' "$out" | sed 's/^/     /'
  elif [ -n "$out" ]; then
    warn "startup lines present, no 'connection established'"
    printf '%s\n' "$out" | sed 's/^/     /'
  else
    warn "banner found, no zulip lines since it"
  fi
}

# --- report -----------------------------------------------------------------

cell_line() {
  # Name the state this device is in relative to the working tree. That state is
  # the point of two fixtures: both-old, old-host/new-plugin, new-host/old-plugin
  # and both-new are four different tests, and a mismatch is supported, not a bug.
  local host=${GATE_HOST:-?} host_state plugin_state
  if [ "$host" = "?" ] || [ "$host" = "0.0.0" ] || [ "$host" = "unknown" ]; then
    host_state='?'
  elif is_old_host "$host"; then
    host_state=old
  else
    host_state=new
  fi
  if [ "${PLUGIN_VERSION:-}" = "$REPO_VERSION" ]; then plugin_state=new; else plugin_state=old; fi
  printf 'cell: host %s (%s) · plugin %s (%s) · tree %s\n' \
    "$host" "$host_state" "${PLUGIN_VERSION:-?}" "$plugin_state" "$REPO_VERSION"
}

verdict_line() {
  if [ "$FAILED" -eq 0 ]; then
    printf '  verdict: %sPASS%s\n' "$C_GRN" "$C_OFF"
  else
    printf '  verdict: %sFAIL%s (%d check(s))\n' "$C_RED" "$FAILED" "$C_OFF"
  fi
}

verify_one() {
  HOST_CLI_VERSION=; PLUGIN_VERSION=; GATE_HOST=; FAILED=0; STEP=0
  printf '\n%sdevice %s:%s%s\n' "$C_DIM" "$KIND" "$NAME" "$C_OFF"
  check_identity
  check_identity_versions || { cell_line; verdict_line; return 1; }
  check_stage            || { cell_line; verdict_line; return 1; }
  check_import           || { cell_line; verdict_line; return 1; }
  check_gate             || { cell_line; verdict_line; return 1; }
  check_crosscheck       || { cell_line; verdict_line; return 1; }
  check_deployed_checksums || { cell_line; verdict_line; return 1; }
  check_startup_log
  cell_line
  verdict_line
}

matrix() {
  local targets=${ZULIP_VERIFY_DEVICES:-docker:lab-hermes ssh:8x}
  local t rc=0
  printf '%smatrix over: %s%s\n' "$C_DIM" "$targets" "$C_OFF"
  for t in $targets; do
    ( resolve_target "$t"; verify_one ) || rc=1
  done
  if [ "$rc" -eq 0 ]; then
    printf '\n%sall cells pass; a mismatch between host and plugin is a supported state.%s\n' "$C_DIM" "$C_OFF"
  fi
  return $rc
}

case ${1:-} in
  ""|-h|--help|help) usage ;;
  --matrix)          matrix; exit $? ;;
  *)                 resolve_target "$1"; verify_one; exit $(( FAILED > 0 )) ;;
esac
