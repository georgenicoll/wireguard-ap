#!/usr/bin/env bash
# Shared plumbing for diagnostics.sh and diagnostics_sudo.sh - sourced by
# them, not run directly. Holds the command registry and the checking and
# dispatch of a command's arguments; each script registers its own
# commands and decides what --show-commands/--run-command do around this.
#
# diagnostics_sudo.sh sources this as root, so keep it owned and writable
# only by the account that already owns the scripts sudoers lets it run.
#
# Param syntax, as printed by --show-commands (see diagnostics.sh's header
# for the full protocol): a name, optionally followed by "?" (optional) and/
# or "=a,b,c" (must be one of these choices).

declare -A PARAMS DESCRIPTIONS
COMMAND_NAMES=()

# register <name> "<space-separated params>" "<description>"
register() {
  COMMAND_NAMES+=("$1")
  PARAMS["$1"]="$2"
  DESCRIPTIONS["$1"]="$3"
}

# Hostname or IPv4/IPv6 address only. In particular can't start with "-",
# so a value can never be mistaken for an option by the tool it's given to.
require_host() {
  if [[ ! $1 =~ ^[A-Za-z0-9][A-Za-z0-9._:-]*$ ]]; then
    echo "invalid host: '$1'" >&2
    exit 2
  fi
}

require_tool() {
  command -v "$1" >/dev/null || { echo "$1 is not installed" >&2; exit 127; }
}

# Prints this script's own commands, one "<name> [<param> ...] | <description>"
# line each.
show_commands() {
  local name
  for name in "${COMMAND_NAMES[@]}"; do
    # Unquoted on purpose: collapses an empty param list to nothing.
    echo "${name}${PARAMS[$name]:+ ${PARAMS[$name]}} | ${DESCRIPTIONS[$name]}"
  done
}

# True if <name> is registered in this script.
has_command() {
  [[ -v PARAMS[$1] ]]
}

# Checks one arg against its param spec ("name", "name?", "name=a,b" ...).
check_param() {
  local spec="$1" value="$2"
  local name="${spec%%[?=]*}" rest="${spec#"${spec%%[?=]*}"}"
  local optional=0 choices=""
  [[ $rest == \?* ]] && { optional=1; rest="${rest#\?}"; }
  [[ $rest == =* ]] && choices="${rest#=}"

  if [[ -z $value ]]; then
    ((optional)) || { echo "$name is required" >&2; exit 2; }
    return 0
  fi
  # A comma in the value would let it match several adjacent choices at
  # once (the list is matched as one comma-joined string), so it's never valid.
  if [[ -n $choices && ( $value == *,* || ",${choices}," != *",${value},"* ) ]]; then
    echo "$name must be one of: ${choices//,/, } (got '$value')" >&2
    exit 2
  fi
}

# run_command <name> [<arg> ...] - <name> must already be known to
# has_command. Exactly one arg per declared param ("" for a blank optional).
run_command() {
  local name="$1"
  shift

  local -a specs
  read -r -a specs <<<"${PARAMS[$name]}"
  if (($# != ${#specs[@]})); then
    echo "$name takes ${#specs[@]} parameter(s) (${PARAMS[$name]:-none}), got $#" >&2
    exit 2
  fi

  local i
  for i in "${!specs[@]}"; do
    check_param "${specs[$i]}" "${@:$((i + 1)):1}"
  done

  "cmd_${name//-/_}" "$@"
}
