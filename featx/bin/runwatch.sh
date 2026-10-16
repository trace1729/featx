#!/usr/bin/env bash
# runwatch.sh — generic long-running job watcher for LLM/human co-use.
#
# Problem: builds like `make emu` take 30min-2h+. Letting the LLM poll wastes
# turns; letting a human watch wastes attention. This script owns the polling:
# the caller issues ONE blocking `wait` and gets woken only on completion
# (or on a caller-chosen timeout, after which re-invoking the same command
# resumes waiting without restarting the build).
#
# Commands:
#   start  <job> [--log FILE] -- <cmd...>   launch cmd detached, track as <job>
#   watch  <job> (--pid PID | --match REGEX) [--log FILE]
#                                           adopt an already-running process;
#                                           --match picks the newest live
#                                           process whose full cmdline matches
#   wait   <job> [-i SEC] [-T SEC] [-n LINES]
#                                           block until job exits (heartbeat
#                                           every -i SEC, default 60). Exit
#                                           codes: job's own code; 75 = still
#                                           running at -T timeout (re-invoke
#                                           to resume); 70 = died w/o exit code
#   status <job> [-n LINES]                 one-shot state + log tail
#   list                                    list all jobs
#   kill   <job>                            terminate job (process group if we
#                                           started it; bare pid if adopted)
#
# State: $RUNWATCH_DIR/<job>/{pid,cmd,start,log,exit_code,adopted}
# RUNWATCH_DIR defaults to .runwatch in the current directory.

set -uo pipefail

RUNWATCH_DIR=${RUNWATCH_DIR:-.runwatch}

EX_USAGE=64 EX_NOJOB=66 EX_DEAD=70 EX_TIMEOUT=75

die() { echo "runwatch: $*" >&2; exit "${2:-$EX_USAGE}"; }

valid_job() {
  [[ ${1:-} =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "invalid job name: '${1:-}'"
}

jobdir() { echo "$RUNWATCH_DIR/$1"; }

require_job() {
  valid_job "$1"
  [[ -d $(jobdir "$1") ]] || die "no such job: $1 (state dir $(jobdir "$1") missing)" "$EX_NOJOB"
}

# Space-delimited list of this script's pid and all ancestors — excluded from
# --match candidates, since the pattern text appears in their cmdlines.
self_tree() {
  local p=$$ out=" "
  while [[ $p =~ ^[0-9]+$ ]] && ((p > 1)); do
    out+="$p "
    p=$(sed -n 's/^.*) [A-Za-z] \([0-9][0-9]*\) .*/\1/p' "/proc/$p/stat" 2>/dev/null)
  done
  echo "$out"
}

# kill -0 succeeds on zombies; check /proc state too (comm may contain
# spaces/parens, so parse after the last ')').
pid_alive() {
  [[ -n ${1:-} ]] || return 1
  kill -0 "$1" 2>/dev/null || return 1
  local st
  st=$(sed -n 's/^.*) \([A-Za-z]\) .*/\1/p' "/proc/$1/stat" 2>/dev/null) || true
  [[ $st != Z ]]
}

job_pid() { cat "$(jobdir "$1")/pid" 2>/dev/null || true; }

# state: finished | running | dead
job_state() {
  local d; d=$(jobdir "$1")
  [[ -f $d/exit_code ]] && { echo finished; return; }
  pid_alive "$(job_pid "$1")" && { echo running; return; }
  echo dead
}

fmt_elapsed() {
  local d now start delta
  d=$(jobdir "$1"); now=$(date +%s)
  start=$(cat "$d/start" 2>/dev/null || echo "$now")
  delta=$((now - start))
  printf '%dh%02dm%02ds' $((delta/3600)) $((delta%3600/60)) $((delta%60))
}

log_lines() {
  local d; d=$(jobdir "$1")
  [[ -f $d/log ]] && wc -l < "$d/log" || echo 0
}

tail_log() {
  local d n; d=$(jobdir "$1"); n=${2:-30}
  [[ -f $d/log ]] || return 0
  echo "--- tail -n $n $d/log ---"
  tail -n "$n" "$d/log"
}

cmd_start() {
  local job=$1; shift; valid_job "$job"
  local d; d=$(jobdir "$job")
  local log=
  while (($#)); do
    case $1 in
      --log) log=${2:-}; [[ -n $log ]] || die "start: --log needs an argument"; shift 2 ;;
      --) shift; break ;;
      *) break ;;
    esac
  done
  (($#)) || die "start: no command given (usage: start <job> [--log FILE] -- <cmd...>)"

  if [[ -d $d && $(job_state "$job") == running ]]; then
    echo "runwatch: job '$job' already running (pid $(job_pid "$job"), elapsed $(fmt_elapsed "$job"))"
    exit 0
  fi

  mkdir -p "$d"
  rm -f "$d/exit_code" "$d/exit_code.tmp" "$d/adopted"
  [[ -z $log ]] && log="$d/log"
  [[ -f $log ]] && mv "$log" "$log.prev"
  # Keep status/wait readers uniform: jobdir/log always points at the real log.
  [[ $log != "$d/log" ]] && ln -sfn "$(readlink -f "$log")" "$d/log"
  printf '%q ' "$@" > "$d/cmd"; echo >> "$d/cmd"
  date +%s > "$d/start"

  # Wrapper subshell captures the real exit code into exit_code (via tmp+mv so
  # waiters never read a partial file). setsid: own process group, survives the
  # invoking shell; kill uses the negative pgid.
  setsid bash -c '
    d=$1; log=$2; shift 2
    "$@" >"$log" 2>&1
    rc=$?
    echo "$rc" > "$d/exit_code.tmp"
    mv "$d/exit_code.tmp" "$d/exit_code"
  ' _ "$d" "$log" "$@" &
  local pid=$!
  echo "$pid" > "$d/pid"
  disown "$pid" 2>/dev/null || true
  echo "runwatch: started job '$job' pid=$pid log=$log"
  echo "runwatch: wait with: $0 wait $job"
}

cmd_watch() {
  local job=$1; shift; valid_job "$job"
  local d; d=$(jobdir "$job")
  local pid= match= log=
  while (($#)); do
    case $1 in
      --pid) pid=${2:-}; [[ -n $pid ]] || die "watch: --pid needs an argument"; shift 2 ;;
      --match) match=${2:-}; [[ -n $match ]] || die "watch: --match needs an argument"; shift 2 ;;
      --log) log=${2:-}; [[ -n $log ]] || die "watch: --log needs an argument"; shift 2 ;;
      *) die "watch: unknown arg: $1" ;;
    esac
  done
  [[ -n $pid || -n $match ]] || die "watch: need --pid PID or --match REGEX"
  if [[ -n $match ]]; then
    # pgrep may return stale/transient matches; the pattern also appears in
    # runwatch's own cmdline and its callers'. Keep the newest live match
    # outside that tree.
    pid=
    local cand excl; excl=$(self_tree)
    for cand in $(pgrep -f "$match"); do
      [[ $excl == *" $cand "* ]] && continue
      pid_alive "$cand" && pid=$cand
    done
    [[ -n $pid ]] || die "watch: no live process matches: $match" "$EX_NOJOB"
  fi
  pid_alive "$pid" || die "watch: pid $pid is not alive" "$EX_NOJOB"

  mkdir -p "$d"
  rm -f "$d/exit_code" "$d/exit_code.tmp"
  echo "$pid" > "$d/pid"
  date +%s > "$d/start"
  echo 1 > "$d/adopted"
  if [[ -r /proc/$pid/cmdline ]] && [[ -s /proc/$pid/cmdline ]]; then
    tr '\0' ' ' < "/proc/$pid/cmdline" > "$d/cmd"
  else
    ps -o args= -p "$pid" > "$d/cmd" 2>/dev/null || echo "pid $pid" > "$d/cmd"
  fi
  [[ -n $log ]] && ln -sf "$(readlink -f "$log")" "$d/log"
  echo "runwatch: adopted pid=$pid as job '$job' (exit code will be unknown; check the log)"
  echo "runwatch: cmd: $(cat "$d/cmd")"
}

cmd_wait() {
  local job=$1; shift; require_job "$job"
  local interval=60 timeout=0 ntail=30
  while (($#)); do
    case $1 in
      -i|--interval) interval=${2:-}; [[ $interval =~ ^[0-9]+$ ]] || die "wait: -i needs seconds"; shift 2 ;;
      -T|--timeout) timeout=${2:-}; [[ $timeout =~ ^[0-9]+$ ]] || die "wait: -T needs seconds"; shift 2 ;;
      -n|--tail) ntail=${2:-}; [[ $ntail =~ ^[0-9]+$ ]] || die "wait: -n needs a line count"; shift 2 ;;
      *) die "wait: unknown arg: $1" ;;
    esac
  done
  local deadline=0
  ((timeout > 0)) && deadline=$((SECONDS + timeout))

  while :; do
    local state; state=$(job_state "$job")
    case $state in
      finished)
        local rc; rc=$(cat "$(jobdir "$job")/exit_code")
        echo "runwatch: job '$job' finished after $(fmt_elapsed "$job"), exit_code=$rc"
        tail_log "$job" "$ntail"
        exit "$rc"
        ;;
      dead)
        if [[ -f $(jobdir "$job")/adopted ]]; then
          echo "runwatch: adopted job '$job' exited after $(fmt_elapsed "$job"); exit code unknown — inspect the log"
          tail_log "$job" "$ntail"
          exit 0
        fi
        echo "runwatch: job '$job' pid is gone but no exit_code was recorded (killed -9? reboot?)" >&2
        tail_log "$job" "$ntail"
        exit "$EX_DEAD"
        ;;
    esac
    if ((deadline > 0 && SECONDS >= deadline)); then
      echo "runwatch: job '$job' still running after $(fmt_elapsed "$job"); wait budget exhausted — re-invoke to resume"
      exit "$EX_TIMEOUT"
    fi
    echo "runwatch: job='$job' state=running elapsed=$(fmt_elapsed "$job") log_lines=$(log_lines "$job")"
    local nap=$interval
    ((deadline > 0 && SECONDS + nap > deadline)) && nap=$((deadline - SECONDS))
    sleep "$nap"
  done
}

cmd_status() {
  local job=$1; shift; require_job "$job"
  local ntail=5
  while (($#)); do
    case $1 in
      -n|--tail) ntail=${2:-}; [[ $ntail =~ ^[0-9]+$ ]] || die "status: -n needs a line count"; shift 2 ;;
      *) die "status: unknown arg: $1" ;;
    esac
  done
  local d state; d=$(jobdir "$job"); state=$(job_state "$job")
  echo "job:      $job"
  echo "state:    $state"
  echo "pid:      $(job_pid "$job")$([[ -f $d/adopted ]] && echo ' (adopted)')"
  echo "elapsed:  $(fmt_elapsed "$job")"
  echo "cmd:      $(cat "$d/cmd" 2>/dev/null)"
  [[ -f $d/exit_code ]] && echo "exit_code: $(cat "$d/exit_code")"
  if [[ -f $d/log ]]; then
    echo "log:      $(readlink -f "$d/log") ($(log_lines "$job") lines, mtime $(stat -c %y "$d/log" | cut -d. -f1))"
    tail_log "$job" "$ntail"
  else
    echo "log:      (none)"
  fi
}

cmd_list() {
  [[ -d $RUNWATCH_DIR ]] || { echo "runwatch: no jobs ($RUNWATCH_DIR missing)"; exit 0; }
  local d job
  for d in "$RUNWATCH_DIR"/*/; do
    [[ -d $d ]] || continue
    job=$(basename "$d")
    printf '%-24s %-9s elapsed=%s pid=%s\n' "$job" "$(job_state "$job")" "$(fmt_elapsed "$job")" "$(job_pid "$job")"
  done
}

cmd_kill() {
  local job=$1; require_job "$job"
  local d pid; d=$(jobdir "$job"); pid=$(job_pid "$job")
  pid_alive "$pid" || { echo "runwatch: job '$job' not running"; exit 0; }
  if [[ -f $d/adopted ]]; then
    kill "$pid"
  else
    kill -- "-$pid" 2>/dev/null || kill "$pid"
  fi
  echo "runwatch: sent SIGTERM to job '$job' (pid $pid)"
}

main() {
  (($#)) || { sed -n '2,30p' "$0"; exit "$EX_USAGE"; }
  local op=$1; shift
  case $op in
    start)  cmd_start "$@" ;;
    watch)  cmd_watch "$@" ;;
    wait)   cmd_wait "$@" ;;
    status) cmd_status "$@" ;;
    list)   cmd_list "$@" ;;
    kill)   cmd_kill "$@" ;;
    *) die "unknown command: $op (want: start|watch|wait|status|list|kill)" ;;
  esac
}

main "$@"
