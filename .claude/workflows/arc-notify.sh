#!/usr/bin/env bash
# arc-notify.sh — the decision-inbox's generic notification hook (issue #79, AU2,
# ruling notification-integration-shape).
#
# The kit ships NO built-in Telegram/Bishop/Slack integration. Instead, it runs
# WHATEVER command the maintainer configures (arc.config.jsonc's `notify.command`),
# handing it the digest message as plain text. The maintainer writes their own small
# forwarding script OUTSIDE the kit (e.g. one that posts to Telegram/Bishop) and points
# `notify.command` at it. This script is the ONE place that command is actually invoked.
#
# Usage:
#   arc-notify.sh <notifyCommand> <message-file>
#
#   <notifyCommand>  the raw string from arc.config.jsonc's notify.command. THE control
#                     against this command executing the untrusted digest as code is the
#                     charset + interpreter denylist validated here (see the validation
#                     block below): a SINGLE executable-path token with NO arguments, run
#                     against BOTH the raw value and the symlink-resolved target, rejecting
#                     any name that is (or resolves to) a shell/interpreter. It is STRICTER
#                     than the /arc skill's crossFamily.exec probe (SKILL.md rule 6).
#                     Rejected values refuse to run (exit 2), never silently fall back to
#                     running something else. (notify.command itself is a trusted,
#                     maintainer-configured surface — ruling notify-command-governing-
#                     protection leaves it unprotected in the committed config, uniform
#                     with crossFamily.exec/testCommand, with this charset validation as
#                     the control; follow-up #156 revisits all three together.)
#   <message-file>    a file containing the digest message (bare facts only — issue
#                     number, how-many-waiting count, and a short title; see the
#                     notification-egress-boundary ruling — full packet text never
#                     reaches this script). Fed to the notifyCommand on STDIN, NEVER as an
#                     argv element and NEVER string-concatenated into a shell command line:
#                     the message is untrusted, LLM/issue-authored text (an issue title can
#                     contain backticks, `$()`, quotes). Stdin delivery is a real control,
#                     but a NARROW one: it protects a data-reading forwarding script (the
#                     documented usage — `msg="$(cat)"`) and neutralizes a program-from-ARGV
#                     interpreter (awk/sed reads its PROGRAM from argv and treats stdin as
#                     DATA). It does NOT protect against a program-from-STDIN interpreter:
#                     bash/sh/dash/zsh/ksh/python/perl/ruby/node/php/lua invoked with zero
#                     argv args READ AND EXECUTE their stdin as their own program. So stdin
#                     delivery does NOT close the "notify.command is an interpreter" class —
#                     the denylist above is what closes it. See the copied-interpreter NOTE
#                     at the denylist for the disclosed, ruled-accepted residual.
#
# Exit code: propagates the invoked command's exit code. The caller (the /arc skill)
# stamps a pending-decisions.json entry's `notifiedAt` ONLY on exit 0 — a failing notify
# command must never be treated as "the maintainer was told" (errors fail loud, per
# CLAUDE.md's own principle; a silently-swallowed notify failure would permanently lose
# the one guarantee this feature exists to provide).
#
# This script does NOT capture or persist the invoked command's own stdout/stderr
# anywhere — it inherits the caller's descriptors directly. Diagnostics stay local to
# whoever's terminal/log is running this; nothing here writes to a committed file, round
# telemetry, or a PR body (a forwarding script's own stderr could otherwise echo a secret
# it embeds, e.g. a bot token in a curl URL).

set -euo pipefail

NOTIFY_CMD="${1:-}"
MSG_FILE="${2:-}"

if [ -z "$NOTIFY_CMD" ] || [ -z "$MSG_FILE" ]; then
  echo "arc-notify: usage: arc-notify.sh <notifyCommand> <message-file>" >&2
  exit 2
fi

# Refuse a symlinked message file (same -L guard the rest of the kit uses before
# reading a path it will open — arc-preflight.sh's append_guarded_line /
# pending_decision_mutate). The message is `cat`'d and forwarded verbatim to the
# external notify command; if a caller ever passed a predictable/attacker-influenced
# MSG_FILE path and an attacker planted a symlink there (e.g. pointing at an SSH key),
# reading through it would leak that file's contents to the configured channel.
if [ -L "$MSG_FILE" ]; then
  echo "arc-notify: message file '$MSG_FILE' is a symlink — refusing to read through it" >&2
  exit 2
fi

if [ ! -f "$MSG_FILE" ]; then
  echo "arc-notify: message file '$MSG_FILE' not found" >&2
  exit 2
fi

# Validate notifyCommand BEFORE it ever reaches a shell — an allowlist (never a
# blocklist, which fails open on any unrecognized input) + reject-and-refuse-with-a-
# loud-warning discipline, in the same spirit as the /arc skill's crossFamily.exec
# probe. A value that fails this check is NOT run as a fallback to anything else — it
# fails loud and does nothing, per CLAUDE.md's "errors fail loud".
#
# THE control against executing the untrusted digest MESSAGE as code is the charset +
# interpreter denylist enforced here: notify.command must be a single [A-Za-z0-9_./-] token
# that is NOT (and does not resolve to) a shell/interpreter. Do NOT rely on stdin delivery
# for this: a program-from-STDIN interpreter (bash/sh/python/perl/ruby/node/...) invoked
# with zero argv args reads and EXECUTES its stdin as code, so stdin delivery would NOT
# neutralize it — only the denylist keeps such a name from ever being run. Stdin delivery
# (the final `exec` below) is a separate, narrower control: it protects a data-reading
# forwarding script and a program-from-ARGV interpreter (awk/sed, which take their program
# from argv, not stdin). A value that fails the checks here is caught loudly, never run.
#
# The check here is STRICTER than crossFamily.exec's (which allows several space-
# separated tokens, e.g. `codex exec`): notify.command MUST be a SINGLE token naming one
# executable path, with NO space-separated arguments at all. The command is invoked with
# ZERO arguments (the MESSAGE goes on stdin), so an embedded flag/arg in the config value
# would be meaningless at best, and for an interpreter like `bash -c` a latent code sink
# were the invocation shape ever changed — forbidding spaces makes every `<interpreter>
# <flag>` (or `<wrapper> <interpreter>`) pair impossible to express. A small basename
# denylist below additionally rejects a BARE interpreter name (an ADDITIVE restriction on
# top of the allowlist, never a widening). The documented usage pattern already matches
# all this: the maintainer points notify.command at their OWN small forwarding script,
# which reads the message from stdin; any flags that script needs live INSIDE it, not in
# this config value.
# Whole-string test (a `case` glob matches the ENTIRE value, including any embedded
# newline — a line-oriented `grep` would pass a multi-line value whose FIRST line is
# clean). Two rejections here:
#   (a) any value that STARTS with '-'. A legitimate executable path never does, and a
#       leading-dash token (e.g. `-c`, `--`, `-l`) would otherwise be parsed by the final
#       `exec` as one of ITS OWN options rather than the command name. Forbidding a leading
#       dash closes that at the source; the literal `--` before the final exec is the
#       defense-in-depth backstop.
#   (b) any byte outside the [A-Za-z0-9_./-] allowlist — spaces, newlines, and shell
#       metacharacters alike.
# A rejected value fails loud and runs nothing (never a fallback), per "errors fail loud".
case "$NOTIFY_CMD" in
  -* | *[!A-Za-z0-9_./-]*)
    echo "arc-notify: notifyCommand '${NOTIFY_CMD}' failed the safe-command-string check (it must be a SINGLE executable path made only of [A-Za-z0-9_./-], must NOT start with '-', and carries no spaces, newlines, arguments, or flags; put any flags inside your forwarding script instead) — refusing to run it. Fix .gstack/arc.config.jsonc's notify.command." >&2
    exit 2 ;;
esac

# The interpreter denylist — THE control against a notify.command that would execute the
# untrusted digest as code (NOT mere defense-in-depth; see the header). It is ADDITIVE to
# the single-token allowlist above (it only ever rejects MORE, never widens). A
# notify.command that names an interpreter/shell (`awk`/`gawk`/`mawk`, the shells,
# `python`/`perl`/`ruby`/`node`, `env`/`xargs`/`eval`, ...) is rejected loudly: a
# program-from-STDIN interpreter (bash/sh/python/perl/ruby/node) fed the digest on stdin
# would EXECUTE it as code, and a program-from-ARGV interpreter (awk/sed) fed no program is
# nonsensical — either way not what the maintainer intended.
#
# The check runs against BOTH the raw config token AND the executable it actually RESOLVES
# to: a repo file `./forwarder` symlinked to /usr/bin/awk has a benign basename
# ('forwarder') but resolves to awk. So resolve the symlink chain (and PATH-resolve a bare
# name the way `exec` will) first, and deny on either the raw or the resolved basename.
# Compared case-insensitively, since a case-insensitive filesystem (macOS default) runs
# `/usr/bin/AWK` as awk. Versioned interpreter names (`python3.13`, `ruby3.2`, `perl5.38`)
# are also matched via the `<name>[0-9]*` globs below, since those are directly namable and
# a plain `python3` entry would otherwise miss `python3.13`.
#
# NOTE — disclosed, ruled-accepted residual: this basename denylist is inherently
# INCOMPLETE. A plain COPY or HARDLINK of a program-from-STDIN interpreter (e.g. a byte-copy
# of /bin/bash saved as `./notify-relay`) resolves to ITSELF, has a benign basename, and so
# passes both the charset check and this denylist — yet, invoked with zero args and the
# digest on stdin, it WOULD read and execute that digest as code. This is NOT "harmless": it
# is a real residual, accepted because notify.command is a trusted, maintainer-configured
# surface (ruling notify-command-governing-protection leaves it unprotected, uniform with
# crossFamily.exec — the game-over surface is config-write, not this exec) and closing the
# copy case at the root (e.g. refusing any resolved target that is a raw compiled binary
# rather than a `#!`-script) is deferred to follow-up #156. Do NOT weaken or remove this
# denylist on the false belief that stdin delivery makes named interpreters safe — it does
# not; the denylist is the only thing rejecting `bash`/`python`/`node` by name.
_arc_notify_is_interpreter() {
  local base
  base="$(basename "$1" | tr '[:upper:]' '[:lower:]')"
  case "$base" in
    sh|bash|dash|zsh|ksh|csh|tcsh|fish|ash|busybox|\
    awk|gawk|mawk|nawk|goawk|frawk|sed|\
    perl|perl5|python|python2|python3|ruby|node|nodejs|deno|bun|php|lua|luajit|tclsh|expect|rscript|\
    pwsh|powershell|\
    python[0-9]*|perl[0-9]*|ruby[0-9]*|php[0-9]*|node[0-9]*|lua[0-9]*|bash[0-9]*|zsh[0-9]*|\
    env|xargs|eval|exec|nohup|setsid|timeout|nice|stdbuf|time|command|watch|find)
      return 0 ;;
  esac
  return 1
}

# Resolve NOTIFY_CMD to the real executable `exec` will run: PATH-resolve a bare name (no
# slash), then follow the symlink chain by hand (portable — macOS's readlink has no -f).
# Bounded to 40 hops so a symlink cycle can't loop forever. Operates on the already-charset-
# validated token (no spaces/metacharacters), so basename/dirname/readlink see safe input.
_arc_notify_resolve() {
  local cmd="$1" target dir i=0
  case "$cmd" in
    */*) : ;;
    *) cmd="$(command -v -- "$cmd" 2>/dev/null || printf '%s' "$cmd")" ;;
  esac
  while [ -L "$cmd" ] && [ "$i" -lt 40 ]; do
    target="$(readlink "$cmd" 2>/dev/null)" || break
    [ -n "$target" ] || break
    case "$target" in
      /*) cmd="$target" ;;
      *) dir="$(dirname "$cmd")"; cmd="$dir/$target" ;;
    esac
    i=$((i + 1))
  done
  printf '%s' "$cmd"
}

RESOLVED_CMD="$(_arc_notify_resolve "$NOTIFY_CMD")"
if _arc_notify_is_interpreter "$NOTIFY_CMD" || _arc_notify_is_interpreter "$RESOLVED_CMD"; then
  echo "arc-notify: notifyCommand '${NOTIFY_CMD}' resolves to a script interpreter / shell / exec-wrapper ('$(basename "$RESOLVED_CMD")'), which would execute the untrusted digest message as code — refusing to run it. Point notify.command at your own forwarding script (which reads the message from stdin), not at an interpreter. Fix .gstack/arc.config.jsonc's notify.command." >&2
  exit 2
fi

# Invoke the ALREADY-VALIDATED single-token command with ZERO argv arguments and the
# untrusted digest MESSAGE delivered on STDIN, redirected from the already -L/-f-checked
# MSG_FILE (never as an argv element, never through `bash -c "$STRING"` / `eval`). Stdin
# delivery means no shell re-parses the message's bytes for a data-reading forwarding script
# (the documented usage) or a program-from-ARGV interpreter — but it is NOT what makes a
# named interpreter safe: the denylist above already refused every interpreter name and
# symlink-resolved target, which is the actual control. The one residual it cannot catch — a
# copied/hardlinked program-from-STDIN interpreter under a benign name — WOULD execute the
# message on stdin (disclosed and ruled-accepted; see the denylist NOTE), so this exec is not
# unconditionally safe; it is safe for exactly the values the denylist admits. The literal
# `--` terminates `exec`'s OWN option parsing before NOTIFY_CMD, so a leading-dash NOTIFY_CMD
# (already rejected by the charset check above) could never be read as an exec option.
exec -- "$NOTIFY_CMD" < "$MSG_FILE"
