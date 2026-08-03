# sbox

A lightweight sandbox wrapper using [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`). Runs commands with a read-only root filesystem and a writable workspace, limiting what a tool can accidentally (or intentionally) modify.

## How it works

- The entire filesystem is mounted read-only
- Your workspace directory is mounted read-write
- A set of common cache directories (npm, gradle, ~/.cache, etc.) are also writable
- Tool-specific config directories get write access based on the command being run
- Network access is preserved

## Requirements

- Linux
- [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`)
- Python 3.10+

```
sudo apt install bubblewrap   # Debian/Ubuntu
sudo dnf install bubblewrap   # Fedora
sudo pacman -S bubblewrap     # Arch
```

## Installation

Copy `sbox` somewhere on your `$PATH`:

```sh
cp sbox ~/.local/bin/sbox
```

## Usage

```
sbox [OPTIONS] COMMAND [ARGS...]
```

sbox parses only its own options, up to the first bare word (the `COMMAND`). Everything from `COMMAND` onward is passed through to the command untouched, so the command's own flags need no `--` guard:

```sh
sbox claude --resume UUID     # --resume UUID goes to claude, not sbox
```

An explicit `--` still works as a hard separator if a command name would otherwise look like an option.

### Options

| Option | Description |
|---|---|
| `--workspace PATH` | Explicitly set the workspace root |
| `--tool TOOL` | Configure mounts for a specific tool (see below) |
| `--rw PATH` | Add an extra read-write mount (repeatable) |
| `--dry-run` | Print the `bwrap` command without running it |

### Workspace detection

The workspace root is detected automatically (unless `--workspace` is given):

1. Walk up from the current directory looking for a marker file: `.sandbox-workspace`, `.sandbox-root`, `.sandboxrc`, `.workspace-root`, or `WORKSPACE`
2. Fall back to the outermost git repository root

The current directory must be inside the workspace.

### Tool presets

The `--tool` option (or auto-detection from the command name) controls which extra directories get write access:

| Tool | Extra writable paths |
|---|---|
| `claude` | `~/.claude`, `~/.claude.json` |
| `codex` | `~/.codex` |
| `opencode` | `~/.config/opencode`, `~/.local/share/opencode`, `~/.local/state/opencode` |
| `none` | _(none)_ |

If the command name matches a known tool, it's selected automatically. Otherwise, use `--tool` explicitly.

### Command arguments

Because sbox already provides the sandbox, it tells the inner tool not to run its own. For recognized commands it injects a default argument ahead of your own:

| Command | Injected argument |
|---|---|
| `claude` | `--permission-mode bypassPermissions` |
| `codex` | `--sandbox danger-full-access` |

Injection is keyed on the **command you run**, not on `--tool`. That means you can borrow a tool's mounts for something else without the tool's flags coming along:

```sh
sbox --tool codex bash   # ~/.codex is writable; bash gets no --sandbox flag
```

Inside that shell you can export whatever you like and launch `codex` yourself, with full control over its arguments.

These are single-valued flags, so passing the same flag yourself overrides the default (the last occurrence wins):

```sh
sbox claude --permission-mode plan    # your value wins over the injected default
sbox codex --sandbox read-only        # likewise
```

## Examples

```sh
# Run claude with auto-detected workspace
sbox claude

# Run an arbitrary command with no tool-specific mounts
sbox --tool none bash

# Explicit workspace
sbox --workspace ~/projects/myapp --tool none make test

# Add an extra read-write mount (e.g. to allow git push)
sbox --rw ~/.ssh --tool none git push

# The command's own flags need no -- guard
sbox claude --resume UUID
```

The `SBOX=1` environment variable is set inside the sandbox so tools can detect they're running in a sandboxed environment.

## Development

Tests use [pytest](https://docs.pytest.org/) and are run with [`uv`](https://docs.astral.sh/uv/):

```sh
uv run pytest
```

`uv` provisions the test dependencies (declared in `pyproject.toml`) automatically — no manual virtualenv setup needed.
