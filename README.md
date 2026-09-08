# Agent Harpoon

A keyboard-first tmux switcher and launcher for agent workspaces. Based on
[tmux-agent-picker](https://github.com/ykumards/tmux-agent-picker).

Open with **prefix + a**, type to search, and press **Enter** to jump to an agent.
Press **Ctrl+n** to choose Codex, Pi, or Claude Code and create a new window in the current
session: agent on the left (70%), normal terminal on the right (30%). Both start
in the directory of the pane where you opened the picker. The agent pane receives
focus and the split is initially visible. Normal tmux zoom and window switching
continue to work.

## Install

Requires tmux, fzf, Bash, a C compiler, and ripgrep (`rg`) for conversation search.
Tested with tmux 3.6a and fzf 0.67.0 on macOS.

With [TPM](https://github.com/tmux-plugins/tpm), add this before TPM's initialization
in your tmux configuration, reload the configuration, then press **prefix + I**:

```tmux
set -g @plugin 'ykumards/tmux-agent-harpoon'
```

Remove the old Agent Picker plugin declaration or direct `run` line if migrating.
Existing `@agent-picker-*` options remain supported, including the shortcut and
zoom preference. The `agent-picker.tmux` entry point keeps its original name for
compatibility; TPM loads it automatically.

For a manual installation:

```sh
git clone https://github.com/ykumards/tmux-agent-harpoon.git "$HOME/.tmux/plugins/tmux-agent-harpoon"
```

Then add this to your tmux configuration and reload it:

```tmux
run-shell '~/.tmux/plugins/tmux-agent-harpoon/agent-picker.tmux'
```

Loading the plugin only binds the shortcut. Install and authenticate your chosen
agent CLIs separately. To try a development checkout on an attached tmux server,
run `tmux run-shell /absolute/path/to/agent-harpoon/agent-picker.tmux`.

## Keys

| Key | Action |
| --- | --- |
| Type | Fuzzy search task name, full project path, agent, pane title, window name, and session context |
| Enter | Jump to the selected agent |
| Ctrl+n | New workspace; type the launcher name and press Enter |
| Ctrl+t | Name the selected conversation; submit an empty name to clear it |
| Ctrl+g | Live grep across active agents’ retained pane scrollback |
| Esc | Close the picker, or return from the launcher menu |
| Up/Down or Ctrl+k/Ctrl+j | Change selection |
| Ctrl+r | Refresh agents and the selected pane preview |
| Ctrl+d/Ctrl+u | Scroll the preview |

The empty picker remains open. Ctrl+n also works when your search has no matches.
Cancelling the launcher returns to your previous query without creating a window.

## Scope and behavior

- `sesh` (or your usual tmux tools) continues to own project/session selection.
- Discovery finds manually started agents too; you do not need a launch wrapper.
- With no query, rows use stable project/pane order. Typing ranks matches by fuzzy relevance across complete metadata, independently of the compact display.
- A Harpoon name is shown first, otherwise a meaningful pane title, window name, or project name. Full titles remain searchable when shortened on screen.
- Ctrl+t sets a pane-local Harpoon name. It survives agent-written terminal titles until cleared or the pane closes; it does not rename the agent’s saved conversation.
- `●` means an interruption marker was detected. No marker means activity is unknown.
  These are best-effort screen observations, not completion notifications.
- The pane preview is a snapshot of the screen plus up to 200 scrollback lines.
  Changing selection or pressing Ctrl+r refreshes it. There is no timer to reset
  your preview scrolling or change the list while you navigate.
- Missing executables fail before creating a window. Failed layout construction
  removes only the new window. An agent that exits, including startup failure,
  leaves an ordinary login shell and its companion terminal usable.
- The picker manages tmux panes, not saved model conversations. Agent history,
  authentication, permissions, and resume behavior remain in the agent CLIs. Loading
  this tool does not change their configuration or loosen their permissions.
- No pins, background daemon, automatic worktrees, model router, or persistent task database.

## Conversation grep

Press **Ctrl+g** in the picker, then type a regular expression. Results update as
you type and show the conversation name, snapshot line number, and matching text.
The preview shows surrounding lines with the match line marked. **Enter** focuses
the corresponding live agent pane; it does not scroll or inject input into the
agent. **Esc** returns to your prior metadata query.

Grep uses ripgrep with smart case: lowercase patterns ignore case, uppercase
patterns are case-sensitive. Invalid or incomplete regexes yield no results while
you type. Each query shows at most 500 matches (up to 200 per pane).

The searchable snapshot includes up to 5,000 retained scrollback lines per active
agent plus its current screen, joining terminal-wrapped lines. **Ctrl+r** captures
fresh content and reruns your query. This is live query filtering over a snapshot,
not continuous output monitoring. Only what tmux still retains is available;
saved/closed agent conversations are not searched.

Snapshots and the metadata index live in a private temporary directory for the
popup's lifetime and are removed when it closes normally or receives INT/TERM.
There are no model calls, remote uploads, or persistent conversation index.

## Configuration

Defaults:

```tmux
set -g @agent-picker-key 'a'
set -g @agent-picker-width '90%'
set -g @agent-picker-height '75%'
set -g @agent-picker-preview-width '70%'
set -g @agent-picker-zoom 'off'          # zoom existing panes on jump
set -g @agent-picker-terminal-width '30' # percent; new windows start unzoomed
set -g @agent-picker-launchers 'codex|pi|claude'
set -g @agent-picker-grep-lines '5000'   # 1–50000 retained lines per agent
set -g @agent-picker-agents 'claude|codex|opencode|aider|pi|goose|amp|gemini'
```

Launcher names contain only letters, numbers, `_`, and `-`. By default, each
launcher runs the executable of that name. Override a command if needed:

```tmux
set -g @agent-picker-command-codex 'codex'
set -g @agent-picker-command-pi 'pi'
set -g @agent-picker-command-claude 'claude'
```

These are trusted shell commands from your tmux configuration, run in an
interactive login shell so shell initialization and per-project runtime tools
are available. The companion pane is a normal login shell. Set new launcher
commands only to programs you intend to run; titles and working directories are
never evaluated as shell commands.

Your existing `@agent-picker-zoom 'on'` preference is honored for jumps. Newly
created windows always reveal both panes first.

## Development

Dependencies: tmux, fzf, Bash, and a C compiler. Conversation grep also needs ripgrep (`rg`). Tested on macOS with tmux 3.6a,
fzf 0.67.0, and Bash 3.2. The scanner retains its Linux `/proc` implementation,
but the current integration run covers macOS only.

```sh
python3 -m unittest discover -s tests -v
scripts/agent-picker --list
```

Tests create temporary tmux servers with fake agents and their own homes. They
exercise real popup keystrokes and never contact a model provider or use the live
tmux server. The tests require permission to start a local tmux server and PTYs.

The scanner compiles automatically under
`${XDG_CACHE_HOME:-$HOME/.cache}/agent-harpoon/`; concurrent rebuilds use temporary
files and an atomic rename. Builds and runtime state stay outside the checkout.

| File | Responsibility |
| --- | --- |
| `agent-picker.tmux` | Bind the shortcut |
| `scripts/agent-popup` | Capture the invoking pane/client and open a popup |
| `scripts/agent-picker` | Search, preview, launcher menu, and navigation |
| `scripts/agent-search` | Rank hidden metadata; capture, grep, and preview scrollback |
| `scripts/agent-grep` | Live-grep popup view |
| `scripts/agent-rename` | Set or clear a pane-local conversation name |
| `scripts/agent-workspace` | Construct the split and run the selected agent |
| `scripts/agent-preview` | Capture a pane safely |
| `scripts/common.sh` | Options and targeting helpers |
| `src/agent-scan.c` | Process discovery and compact display rows |

MIT license; original license retained.
