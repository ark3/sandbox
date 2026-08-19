# sbox

A lightweight sandbox wrapper using [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`). Runs commands with a read-only root filesystem and a writable workspace, limiting what a tool can accidentally (or intentionally) modify.

## How it works

- The entire filesystem is mounted read-only
- Your workspace directory is mounted read-write
- A set of common cache directories (npm, gradle, ~/.cache, etc.) are also writable
- A mount profile adds write access to one tool's config directories, picked automatically from the command being run or set with `--profile`
- Your SSH configuration is replaced with an empty directory, and the ssh-agent socket is hidden
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
| `--profile NAME` | Select the mount profile (see below) |
| `--rw PATH` | Add an extra read-write mount (repeatable) |
| `--ssh` | Expose the real `~/.ssh` and ssh-agent (default: an empty directory) |
| `--dry-run` | Print the `bwrap` command without running it |

### Workspace detection

The workspace root is detected automatically (unless `--workspace` is given):

1. Walk up from the current directory looking for a marker file: `.sandbox-workspace`, `.sandbox-root`, `.sandboxrc`, `.workspace-root`, or `WORKSPACE`
2. Fall back to the outermost git repository root

The current directory must be inside the workspace.

### Mount profiles

A *profile* is a named set of extra read-write mounts. It controls only what is writable — it does not decide what runs, and it does not decide which arguments get injected. Select one with `--profile`, or let it be auto-detected from the command name:

| Profile | Extra writable paths |
|---|---|
| `claude` | `~/.claude`, `~/.claude.json` |
| `codex` | `~/.codex` |
| `opencode` | `~/.config/opencode`, `~/.local/share/opencode`, `~/.local/state/opencode` |
| `none` | _(none)_ |

If the command name matches a profile, that profile is selected automatically. Otherwise sbox stops and asks rather than guessing: an unrecognized command is an error, and you say `--profile none` to confirm it needs no extra writable paths. This is deliberate — silently running with no profile would let a tool fail deep inside the sandbox on a config directory it couldn't write, which is a far worse error message than the one you get up front.

Exactly one profile applies per run; they don't compose.

### Redirects

Some tools write to a fixed path outside the workspace. Rather than making that path writable — which would let a sandboxed agent modify state your normal, unsandboxed work depends on — sbox bind-mounts a sandbox-private directory *over* it:

| Path inside the sandbox | Actually |
|---|---|
| `~/.m2/repository` | `~/.cache/agent-m2` |

Maven's local repository is the motivating case: `mvn install`'s whole job is writing the built artifact into `~/.m2/repository`, which is read-only here, so installs fail outright. With the redirect, Maven writes to a repository of its own and your real one is neither visible nor modifiable from inside.

No configuration is needed — `mvn`, `mvnw`, Gradle's `mavenLocal()` and IDEs all find the repository at the path they already expect, on any Maven version. Only the `repository` subtree is replaced, so `~/.m2/settings.xml` stays readable and internal mirrors and credentials keep working.

Two consequences:

- The sandbox repository starts empty and fills up as you build. It persists across runs, so the cost is a slow first build, not a slow every build — but dependencies fetched outside the sandbox don't warm it, and vice versa.
- Artifacts an agent installs are invisible to your unsandboxed builds. A library `mvn install`ed inside the sandbox won't be found by an `mvn` run outside it.

sbox creates both ends of a redirect on the host if they don't exist — the only case where it writes outside the sandbox. `--dry-run` never does; a dry-run command pasted into a shell may need those directories created first.

If you genuinely want the real repository inside the sandbox, `--rw` is applied after redirects and wins:

```sh
sbox --rw ~/.m2/repository claude    # writes land in the real local repository
```

### SSH

The rest of the filesystem is read-only, not invisible — a sandboxed tool can still *read* everything, including your private keys. So `~/.ssh` gets separate treatment: it is replaced with an empty `tmpfs`, and `SSH_AUTH_SOCK` is unset.

Both halves matter. Masking the key files alone would be theater: an ssh-agent socket survives `--unshare-all`, and keys loaded into the agent stay usable through it whether or not the sandbox can see the files they came from.

A `tmpfs` rather than an empty directory somewhere on disk means nothing is created on the host, the directory is writable so `ssh` can record a `known_hosts` entry instead of failing, and nothing written there survives the run.

The practical consequence is that SSH authentication does not work inside the sandbox. `git push` over `ssh` fails; `https` remotes with a credential helper are unaffected. Two other things to expect:

- `known_hosts` starts empty, so the first connection to any host prompts for confirmation — which a non-interactive agent will hang on rather than answer.
- Host aliases, `ProxyJump` and friends from your `~/.ssh/config` are gone, so connections by alias fail. `/etc/ssh/ssh_config` still applies.

Use `--ssh` for a run that needs your SSH identity. It exposes the real `~/.ssh` read-only, like the rest of the root, and leaves the agent socket reachable:

```sh
sbox --ssh --profile none git push
```

`--rw` is applied after the mask, so `--rw ~/.ssh` also puts the real directory back and additionally makes it writable — for `ssh` to append to your real `known_hosts`, say. Note that it does *not* restore the agent socket; combine it with `--ssh` if you want both.

One limitation: if `~/.ssh` is a symlink, the mask covers the directory it points at, so the sandbox still sees an empty `~/.ssh` — but the real directory remains readable under its own path.

### Command arguments

Because sbox already provides the sandbox, it tells the inner tool not to run its own. For recognized commands it injects a default argument ahead of your own:

| Command | Injected argument |
|---|---|
| `claude` | `--permission-mode bypassPermissions` |
| `codex` | `--sandbox danger-full-access` |

Injection is keyed on the **command you run**, not on `--profile`. That means you can borrow a tool's mounts for something else without the tool's flags coming along:

```sh
sbox --profile codex bash   # ~/.codex is writable; bash gets no --sandbox flag
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

# Run an arbitrary command with no profile mounts
sbox --profile none bash

# Explicit workspace
sbox --workspace ~/projects/myapp --profile none make test

# Expose the real ~/.ssh and ssh-agent (e.g. to allow git push)
sbox --ssh --profile none git push

# Add an extra read-write mount
sbox --rw ~/.cargo --profile none cargo build

# Open a shell with Codex's mounts, then launch codex yourself
sbox --profile codex bash

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
