#!/usr/bin/env bash
# Shared by the launch, picker, and workspace scripts. Bash 3.2 compatible.
HARPOON_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

option() {
  local value
  value=$(tmux show-option -gqv "$1" 2>/dev/null) || value=
  printf '%s\n' "${value:-$2}"
}

fail() { printf 'agent-harpoon: %s\n' "$*" >&2; return 1; }

valid_agent() {
  case "$1" in ''|*[!a-zA-Z0-9_-]*) return 1 ;; esac
}

launchers() {
  local agent
  while IFS= read -r agent; do
    valid_agent "$agent" && printf '%s\n' "$agent"
  done < <(option @agent-picker-launchers 'codex|pi|claude' | tr '|' '\n')
}

agent_command() {
  valid_agent "$1" || return 1
  option "@agent-picker-command-$1" "$1"
}

# Always use the invoking client, including when multiple clients are attached.
jump_to() {
  local pane=$1 session=$2 zoom=${3:-off}
  tmux display-message -p -t "$pane" '#{pane_id}' >/dev/null 2>&1 || {
    fail 'That pane has closed. Refresh with Ctrl+r.'; return 1;
  }
  if [[ -n "${AH_CLIENT:-}" ]]; then
    tmux switch-client -c "$AH_CLIENT" -t "$session" || return 1
  else
    tmux switch-client -t "$session" || return 1
  fi
  local window
  window=$(tmux display-message -p -t "$pane" '#{window_id}') || return 1
  tmux select-window -t "$session:$window" || return 1
  tmux select-pane -t "$pane" || return 1
  if [[ "$zoom" == on && $(tmux display-message -p -t "$pane" '#{window_zoomed_flag}') == 0 ]]; then
    if (( $(tmux display-message -p -t "$pane" '#{window_panes}') > 1 )); then
      tmux resize-pane -Z -t "$pane"
    fi
  fi
}

picker_ui() {
  FZF_DEFAULT_OPTS= FZF_DEFAULT_OPTS_FILE= fzf \
    --sync --layout=reverse --border=none --info=hidden --padding=1,2 \
    --no-separator --gutter=' ' --pointer='▸' --footer-border=none \
    --color='bg+:-1,gutter:-1,pointer:cyan,hl:underline,hl+:underline,header:dim,footer:dim' "$@"
}
