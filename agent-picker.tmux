#!/usr/bin/env bash
# Compatibility entry point: existing TPM/config installations keep working.
set -uo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/common.sh"
printf -v popup '%q' "$HARPOON_DIR/scripts/agent-popup"
# run-shell expands these formats at keypress time. display-popup's -e does not.
tmux bind-key "$(option @agent-picker-key a)" run-shell -b \
  "$popup '#{pane_id}' '#{session_id}' '#{client_name}'"
