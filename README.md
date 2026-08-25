# sbox

A lightweight sandbox wrapper using [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`). Runs commands with a read-only root filesystem and a writable workspace, limiting what a tool can accidentally (or intentionally) modify.

## How it works

- The entire filesystem is mounted read-only
- Your workspace directory is mounted read-write
- A set of shared caches and agent state directories (`~/.cache`, npm, gradle, `~/.agents`, …) are also writable — see `COMMON_RW_MOUNTS`
- A mount profile adds write access to one tool's config directories, picked automatically from the command being run or set with `--profile`
- Tools that insist on writing outside the workspace get sandbox-private storage instead of your real state
- Your SSH configuration is replaced with a directory holding nothing but a locked-down `ssh_config`, and the ssh-agent socket is hidden
- Network access is preserved

This README explains *what* sbox does and *why*. The authoritative *how* — which paths, which profiles, which arguments — lives in `sbox` itself, and is named here rather than copied, so the two can't drift apart. The lists below are illustrative.

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
| `--ssh` / `--no-ssh` | Expose the real `~/.ssh` and ssh-agent (default: `--no-ssh`, an empty directory) |
| `--dry-run` | Print the `bwrap` command without running it |

### Workspace detection

The workspace root is detected automatically (unless `--workspace` is given):

1. Walk up from the current directory looking for a marker file, such as `.sandbox-workspace` — see `MARKER_FILES` for the set
2. Fall back to the outermost git repository root — the outermost, so a submodule or a nested checkout doesn't shrink the workspace to a subdirectory of the project you're working on

The current directory must be inside the workspace.

### Mount profiles

A *profile* is a named set of extra read-write mounts. It controls only what is writable — it does not decide what runs, and it does not decide which arguments get injected. Select one with `--profile`, or let it be auto-detected from the command's name:

```sh
sbox claude                 # the `claude` profile: ~/.claude and friends are writable
sbox all                    # all profile-specific paths are writable
sbox --profile none bash    # no extra mounts
```

`PROFILE_MOUNTS` is the list of profiles and what each one makes writable; `--profile` accepts exactly those names, and `--help` prints them.

A command is identified by its basename, so `sbox /usr/local/bin/codex` gets the same profile as `sbox codex`.

If the command's name matches a profile, that profile is selected automatically. Otherwise sbox stops and asks rather than guessing: an unrecognized command is an error, and you say `--profile none` to confirm it needs no extra writable paths. This is deliberate — silently running with no profile would let a tool fail deep inside the sandbox on a config directory it couldn't write, which is a far worse error message than the one you get up front.

Exactly one profile applies per run; they don't compose.

The `all` profile is the deduplicated union of every other profile. It is useful
for a launcher that can start multiple tools. Argument injection still follows
only the top-level command, so a launcher must supply any tool-specific
permission or sandbox arguments itself.

### Sandbox-private state

Some tools must write to a fixed path outside the workspace, and simply failing isn't an option — `mvn install`'s entire job is to write into `~/.m2/repository`, and `uv tool install` into `~/.local/share/uv/tools`. Making those paths writable would defeat the point, letting a sandboxed agent modify state your normal, unsandboxed work depends on. So sbox gives each tool private storage under `~/.cache` and lets it write there instead.

Two mechanisms, chosen per tool:

| Tool | Path it wants | Mechanism |
|---|---|---|
| Maven | `~/.m2/repository` | a bind mount over the path — see `REDIRECTS` |
| uv | `~/.local/share/uv/tools` | `UV_TOOL_DIR`, set in `build_bwrap_command` |

The environment variable is preferred where a tool offers one, because it announces itself to anyone debugging inside the sandbox; a bind mount silently makes a path mean something else. Maven gets the bind mount because its path is a hardcoded convention with several consumers — `mvn`, `mvnw`, Gradle's `mavenLocal()`, IDEs — and its only environment knobs are unusable or version-dependent. `sbox` documents that tradeoff where the redirects are defined, for whoever adds the next one.

Either way, no configuration is needed inside the sandbox, and the redirect is surgical: only `~/.m2/repository` is replaced, so `~/.m2/settings.xml` stays readable and your internal mirrors and credentials keep working.

The consequences are the same for both, and worth knowing:

- Private storage starts empty and fills up as you work. It persists across runs, so the cost is a slow first build, not a slow every build — but downloads outside the sandbox don't warm it, and vice versa.
- Anything an agent installs is invisible outside. A library `mvn install`ed inside the sandbox won't be found by an `mvn` run outside it.

sbox creates both ends of a bind redirect on the host if they don't exist — the only case where it writes outside the sandbox. `--dry-run` never does; a dry-run command pasted into a shell may need those directories created first.

If you genuinely want the real path inside the sandbox, `--rw` is applied after redirects and wins:

```sh
sbox --rw ~/.m2/repository claude    # writes land in the real local repository
```

### SSH

The rest of the filesystem is read-only, not invisible — a sandboxed tool can still *read* everything, including your private keys. So `~/.ssh` gets separate treatment: it is replaced with an empty `tmpfs`, and `SSH_AUTH_SOCK` is unset.

Both halves matter. Masking the key files alone would be theater: an ssh-agent socket survives `--unshare-all`, and keys loaded into the agent stay usable through it whether or not the sandbox can see the files they came from.

A `tmpfs` rather than an empty directory somewhere on disk means nothing is created on the host, the directory is writable so `ssh` can record a `known_hosts` entry instead of failing, and nothing written there survives the run.

The directory is not left entirely bare: a small `~/.ssh/config` is seeded into it — `SSH_CONFIG` in `sbox` — and it is the only configuration `ssh` sees. It sets three directives, each load-bearing:

| Directive | Why |
|---|---|
| `BatchMode yes` | An empty `known_hosts` makes the first connection to any host ask for confirmation, and a non-interactive agent hangs on that prompt rather than answering it — the sandbox looks wedged instead of reporting that SSH is unavailable. This turns every prompt into an immediate failure. |
| `IdentitiesOnly yes` | Offer only keys named by an `IdentityFile`. None is configured and the directory is empty, so nothing is offered — including keys held by an ssh-agent. This is what keeps the agent unreachable even if something inside the sandbox re-exports `SSH_AUTH_SOCK`, which unsetting the variable alone cannot prevent: the socket is still there under `/run`. |
| `ControlPath none` | Refuse connection multiplexing, so a `ControlPath` from `/etc/ssh/ssh_config` pointing somewhere writable (`/tmp` is writable here) can't let the sandbox ride a connection authenticated outside it. |

`~/.ssh/config` is read before `/etc/ssh/ssh_config` and the first value of a keyword wins, so nothing system-wide overrides these. The file arrives mode `0600` and is itself a mount point, so the sandbox can neither rewrite nor delete it — the settings hold for the whole run.

The practical consequence is that SSH authentication does not work inside the sandbox, and thanks to `BatchMode` it fails immediately and says so instead of hanging on a prompt. `git push` over `ssh` fails; `https` remotes with a credential helper are unaffected. One more thing to expect:

- Host aliases, `ProxyJump` and friends from your real `~/.ssh/config` are gone, so connections by alias fail. `/etc/ssh/ssh_config` still applies, except where the seeded config overrides it.

Use `--ssh` for a run that needs your SSH identity. It exposes the real `~/.ssh` read-only, like the rest of the root, and leaves the agent socket reachable:

```sh
sbox --ssh --profile none git push
```

`--rw` is applied after the mask, so `--rw ~/.ssh` also puts the real directory back and additionally makes it writable — for `ssh` to append to your real `known_hosts`, say. Note that it does *not* restore the agent socket; combine it with `--ssh` if you want both.

Two limitations:

- If you have no `~/.ssh` at all, there is nothing to mask and no mount point to seed the config into — a read-only root can't be given one — so both are skipped.
- If `~/.ssh` is a symlink, the mask covers the directory it points at, so the sandbox still sees an empty `~/.ssh` — but the real directory remains readable under its own path.

`--dry-run` prints a `--ro-bind-data` referring to a file descriptor (`SSH_CONFIG_FD`) that sbox opens just before it execs `bwrap`. That command isn't runnable as pasted without redirecting the descriptor yourself; `--dry-run` says which one on stderr.

### Command arguments

Because sbox already provides the sandbox, it tells the inner tool not to run its own. For recognized commands it injects a default argument ahead of your own — `claude` gets `--permission-mode bypassPermissions`, for instance. `COMMAND_ARGS` is the full mapping.

Injection is keyed on the **command you run**, not on `--profile`. That means you can borrow a tool's mounts for something else without the tool's flags coming along:

```sh
sbox --profile codex bash   # ~/.codex is writable; bash gets no --sandbox flag
```

Inside that shell you can export whatever you like and launch `codex` yourself, with full control over its arguments.

The two are independent in the other direction too: having a profile doesn't imply an injection. `claude-agent-acp` shares `claude`'s mounts, but the ACP adapter rejects `--permission-mode`, so it gets nothing injected.

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

The `SBOX=1` environment variable is set inside the sandbox so tools can detect they're running in a sandboxed environment. `UV_TOOL_DIR` is also set, for the reasons in [Sandbox-private state](#sandbox-private-state).

## Development

Tests use [pytest](https://docs.pytest.org/) and are run with [`uv`](https://docs.astral.sh/uv/):

```sh
uv run pytest
```

`uv` provisions the test dependencies (declared in `pyproject.toml`) automatically — no manual virtualenv setup needed.
