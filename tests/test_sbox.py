"""Tests for sbox.

Two layers: end-to-end tests that drive the real script (argument passthrough
and argument injection, exactly as a user hits them), and unit tests for the
pure business logic (profile resolution and workspace resolution). Argument
splitting itself is argparse's job now (nargs=REMAINDER), so it isn't unit
tested here -- the end-to-end tests pin the behavior we rely on.

Injection is keyed on the command name, not on --profile; the tests below pin
both halves of that split.

Run with `uv run pytest`.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


# --- end-to-end: argument passthrough and injection -------------------------
# `sbox --dry-run` prints the assembled bwrap command to stdout; the wrapped
# command and its args are the tail of that line.


def test_flags_after_command_pass_through(run_sbox):
    # The headline feature: no `--` needed; claude's own flag reaches claude.
    r = run_sbox("claude", "--resume", "UUID")
    assert r.returncode == 0
    assert r.stdout.rstrip().endswith(
        "claude --permission-mode bypassPermissions --resume UUID"
    )


def test_command_flag_colliding_with_sbox_flag_is_not_stolen(run_sbox):
    # `--workspace` after the command belongs to the command, not to sbox: it
    # must be forwarded, and sbox's own workspace must stay auto-detected.
    r = run_sbox("--profile", "none", "bash", "--workspace", "/somewhere")
    assert r.returncode == 0
    assert "bash --workspace /somewhere" in r.stdout
    assert "--chdir /somewhere" not in r.stdout  # sbox did not consume it


def test_double_dash_still_separates(run_sbox):
    r = run_sbox("--profile", "none", "--", "bash", "--login")
    assert r.returncode == 0
    assert r.stdout.rstrip().endswith("bash --login")


def test_injected_default_can_be_overridden(run_sbox):
    # The user's --permission-mode follows the injected default, so it wins.
    r = run_sbox("claude", "--permission-mode", "plan")
    assert r.returncode == 0
    assert (
        "claude --permission-mode bypassPermissions --permission-mode plan"
        in r.stdout
    )


def test_claude_default_injected(run_sbox):
    # The headline invocation, `sbox claude`, with no extra args.
    r = run_sbox("claude")
    assert r.returncode == 0
    assert r.stdout.rstrip().endswith("claude --permission-mode bypassPermissions")
    assert "adding claude args: --permission-mode bypassPermissions" in r.stderr


def test_codex_default_injected(run_sbox):
    r = run_sbox("codex")
    assert r.returncode == 0
    assert r.stdout.rstrip().endswith("codex --sandbox danger-full-access")
    assert "adding codex args: --sandbox danger-full-access" in r.stderr


def test_profile_none_injects_nothing(run_sbox):
    r = run_sbox("--profile", "none", "bash")
    assert r.returncode == 0
    assert "adding" not in r.stderr
    assert r.stdout.rstrip().endswith("bash")


def test_profile_does_not_inject_into_other_command(run_sbox):
    # The point of keying injection on the command: borrow codex's mounts to
    # run a shell, and bash must not be handed --sandbox danger-full-access.
    r = run_sbox("--profile", "codex", "bash")
    assert r.returncode == 0
    assert "--sandbox" not in r.stdout
    assert "adding" not in r.stderr
    assert r.stdout.rstrip().endswith("bash")


def test_injection_follows_command_not_profile(run_sbox):
    # Converse of the above: --profile none drops codex's mounts but the command
    # is still codex, so it still must not run its own sandbox.
    r = run_sbox("--profile", "none", "codex")
    assert r.returncode == 0
    assert r.stdout.rstrip().endswith("codex --sandbox danger-full-access")


def test_injection_matches_command_basename(run_sbox):
    # An absolute path to the tool is still the tool.
    r = run_sbox("--profile", "codex", "/usr/local/bin/codex")
    assert r.returncode == 0
    assert r.stdout.rstrip().endswith(
        "/usr/local/bin/codex --sandbox danger-full-access"
    )


def test_acp_variant_gets_no_injection(run_sbox):
    # claude-agent-acp shares claude's mounts but the ACP adapter rejects
    # --permission-mode, so nothing is injected.
    r = run_sbox("claude-agent-acp")
    assert r.returncode == 0
    assert "--permission-mode" not in r.stdout
    assert "adding" not in r.stderr


def test_no_command_errors(run_sbox):
    r = run_sbox()
    assert r.returncode != 0
    assert "command" in r.stderr


# --- redirects --------------------------------------------------------------
# ~/.m2/repository is bind-mounted from ~/.cache/agent-m2 so `mvn install` has
# somewhere to write without exposing the real local repository.


def _bwrap_args(stdout: str) -> list[str]:
    return stdout.rstrip().split(" ")


def test_maven_repo_is_redirected(run_sbox, tmp_path):
    home = tmp_path / "home"
    r = run_sbox("--profile", "none", "bash", extra_env={"HOME": str(home)})
    assert r.returncode == 0
    assert f"--bind {home}/.cache/agent-m2 {home}/.m2/repository" in r.stdout


def test_dry_run_creates_no_directories(run_sbox, tmp_path):
    # create_redirect_dirs() is the only thing sbox writes outside the sandbox,
    # and it must stay off the --dry-run path.
    home = tmp_path / "home"
    home.mkdir()
    r = run_sbox("--profile", "none", "bash", extra_env={"HOME": str(home)})
    assert r.returncode == 0
    assert not (home / ".m2").exists()
    assert not (home / ".cache" / "agent-m2").exists()


def test_redirect_comes_after_common_mounts(run_sbox, tmp_path):
    # Order matters: a common mount landing after the redirect could re-expose
    # part of the real ~/.m2 on top of it.
    home = tmp_path / "home"
    (home / ".cache").mkdir(parents=True)
    r = run_sbox("--profile", "none", "bash", extra_env={"HOME": str(home)})
    argv = _bwrap_args(r.stdout)
    assert argv.index(f"{home}/.cache/agent-m2") > argv.index(f"{home}/.cache")


def test_explicit_rw_wins_over_redirect(run_sbox, tmp_path):
    # --rw is applied last, so a user who really wants the real repository can
    # mount it back over the redirect.
    home = tmp_path / "home"
    real_repo = home / ".m2" / "repository"
    real_repo.mkdir(parents=True)
    r = run_sbox(
        "--rw", str(real_repo), "--profile", "none", "bash",
        extra_env={"HOME": str(home)},
    )
    argv = _bwrap_args(r.stdout)
    # The redirect's own `--bind AGENT_M2 REAL_REPO` also mentions real_repo, so
    # match the --rw bind by its distinctive src==dest shape rather than by the
    # first occurrence of the path.
    rw_bind = [
        i
        for i in range(len(argv) - 2)
        if argv[i : i + 3] == ["--bind", str(real_repo), str(real_repo)]
    ]
    assert len(rw_bind) == 1
    assert rw_bind[0] > argv.index(f"{home}/.cache/agent-m2")


def test_real_m2_cache_is_not_mounted(run_sbox, tmp_path):
    # The old ~/.m2/repository/.cache entry would punch a hole through the
    # redirect; it must be gone from COMMON_RW_MOUNTS.
    home = tmp_path / "home"
    r = run_sbox("--profile", "none", "bash", extra_env={"HOME": str(home)})
    assert f"{home}/.m2/repository/.cache" not in r.stdout


# --- ssh masking ------------------------------------------------------------
# ~/.ssh is replaced with an empty tmpfs and SSH_AUTH_SOCK is unset, unless
# --ssh is given.


def test_ssh_dir_is_masked_by_default(run_sbox, tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    r = run_sbox("--profile", "none", "bash", extra_env={"HOME": str(home)})
    assert r.returncode == 0
    assert f"--tmpfs {home}/.ssh" in r.stdout


def test_ssh_agent_socket_is_dropped_by_default(run_sbox, tmp_path):
    # Hiding the key files is theater on its own: an agent socket survives
    # --unshare-all and carries usable keys.
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    r = run_sbox("--profile", "none", "bash", extra_env={"HOME": str(home)})
    assert "--unsetenv SSH_AUTH_SOCK" in r.stdout


def test_ssh_flag_exposes_real_config(run_sbox, tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    r = run_sbox("--ssh", "--profile", "none", "bash", extra_env={"HOME": str(home)})
    assert r.returncode == 0
    assert "--tmpfs" not in r.stdout
    assert "SSH_AUTH_SOCK" not in r.stdout


def test_missing_ssh_dir_is_skipped(run_sbox, tmp_path):
    # A tmpfs needs an existing mount point, and a read-only root cannot supply
    # one -- but the agent socket must still go, since it can hold keys with no
    # file on disk.
    home = tmp_path / "home"
    home.mkdir()
    r = run_sbox("--profile", "none", "bash", extra_env={"HOME": str(home)})
    assert r.returncode == 0
    assert f"--tmpfs {home}/.ssh" not in r.stdout
    assert "--unsetenv SSH_AUTH_SOCK" in r.stdout


def test_masking_creates_no_directories(run_sbox, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    r = run_sbox("--profile", "none", "bash", extra_env={"HOME": str(home)})
    assert r.returncode == 0
    assert not (home / ".ssh").exists()


def test_ssh_config_is_seeded(run_sbox, tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    r = run_sbox("--profile", "none", "bash", extra_env={"HOME": str(home)})
    assert r.returncode == 0
    assert f"--ro-bind-data 21 {home}/.ssh/config" in r.stdout


def test_seeded_config_lands_after_the_tmpfs(run_sbox, tmp_path):
    # The tmpfs is the config's mount point; emitted the other way round, the
    # tmpfs would wipe it straight back out.
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    r = run_sbox("--profile", "none", "bash", extra_env={"HOME": str(home)})
    argv = _bwrap_args(r.stdout)
    assert argv.index("--ro-bind-data") > argv.index("--tmpfs")


def test_seeded_config_cannot_reach_the_real_ssh_dir(run_sbox, tmp_path):
    # The safety property behind the whole ordering: --ro-bind-data writes at
    # the path it is given, so if it were emitted after `--rw ~/.ssh` bound the
    # real directory back, it would land in the user's actual ~/.ssh/config.
    home = tmp_path / "home"
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True)
    r = run_sbox(
        "--rw", str(ssh_dir), "--profile", "none", "bash",
        extra_env={"HOME": str(home)},
    )
    argv = _bwrap_args(r.stdout)
    rw_bind = [
        i
        for i in range(len(argv) - 2)
        if argv[i : i + 3] == ["--bind", str(ssh_dir), str(ssh_dir)]
    ]
    assert rw_bind[0] > argv.index("--ro-bind-data")


def test_no_config_seeded_without_a_dir_to_mask(run_sbox, tmp_path):
    # No tmpfs means no mount point for the config, and an fd nobody opened.
    home = tmp_path / "home"
    home.mkdir()
    r = run_sbox("--profile", "none", "bash", extra_env={"HOME": str(home)})
    assert r.returncode == 0
    assert "--ro-bind-data" not in r.stdout


def test_ssh_flag_seeds_nothing(run_sbox, tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    r = run_sbox("--ssh", "--profile", "none", "bash", extra_env={"HOME": str(home)})
    assert "--ro-bind-data" not in r.stdout


def test_dry_run_explains_the_config_fd(run_sbox, tmp_path):
    # The printed command references an inherited fd, so it is not runnable as
    # pasted; say so rather than letting it fail cryptically.
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    r = run_sbox("--profile", "none", "bash", extra_env={"HOME": str(home)})
    assert "fd 21" in r.stderr


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="needs real bubblewrap")
def test_seeded_config_is_readable_and_locked_down(sbox_path, tmp_path):
    """Run real bwrap: the config must arrive intact, 0600, and immutable.

    The rest of the suite reads the command sbox builds; this one checks what
    that command actually does, since the properties the mask relies on --
    permissions ssh will accept, and a file the sandbox cannot rewrite -- are
    bubblewrap's behavior, not sbox's.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_ed25519").write_text("PRIVATE KEY")

    probe = (
        f"cat {home}/.ssh/config; "
        f"stat -c 'perms=%a' {home}/.ssh/config; "
        f"ls {home}/.ssh/id_ed25519 2>&1 | tail -1; "
        f"(echo pwned > {home}/.ssh/config) 2>&1 | tail -1"
    )
    r = subprocess.run(
        [sys.executable, str(sbox_path), "--profile", "none", "sh", "-c", probe],
        cwd=workspace,
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
    )
    assert "BatchMode yes" in r.stdout
    assert "IdentitiesOnly yes" in r.stdout
    assert "ControlPath none" in r.stdout
    assert "perms=600" in r.stdout          # ssh rejects a laxer config
    assert "No such file" in r.stdout       # the real key is gone
    assert "Read-only file system" in r.stdout  # and the config cannot be rewritten
    assert (home / ".ssh" / "config").exists() is False  # nothing leaked to the host


def test_explicit_rw_wins_over_ssh_mask(run_sbox, tmp_path):
    # --rw is applied after the mask, so a run that genuinely needs the real
    # directory -- writable -- can have it back.
    home = tmp_path / "home"
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True)
    r = run_sbox(
        "--rw", str(ssh_dir), "--profile", "none", "bash",
        extra_env={"HOME": str(home)},
    )
    argv = _bwrap_args(r.stdout)
    rw_bind = [
        i
        for i in range(len(argv) - 2)
        if argv[i : i + 3] == ["--bind", str(ssh_dir), str(ssh_dir)]
    ]
    assert len(rw_bind) == 1
    assert rw_bind[0] > argv.index("--tmpfs")


# --- resolve_profile --------------------------------------------------------


def test_resolve_profile_explicit_wins(sbox):
    assert sbox.resolve_profile("none", "claude") == "none"


def test_resolve_profile_auto_from_command(sbox):
    assert sbox.resolve_profile(None, "codex") == "codex"


def test_profile_matches_command_basename(run_sbox, tmp_path):
    # An absolute path to the tool is still the tool, for the profile exactly
    # as it already was for injection: both lookups key off the basename, so
    # codex's mounts appear without --profile.
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    r = run_sbox("/usr/local/bin/codex", extra_env={"HOME": str(home)})
    assert r.returncode == 0
    assert f"--bind {home}/.codex {home}/.codex" in r.stdout


def test_all_auto_profile_mounts_every_supported_agent_state(run_sbox, tmp_path):
    home = tmp_path / "home"
    agent_state_paths = [
        ".claude",
        ".claude.json",
        ".codex",
        ".config/opencode",
        ".local/share/opencode",
        ".local/state/opencode",
        ".pi/agent",
        ".omp",
        ".eclipse",
    ]
    for relative_path in agent_state_paths:
        path = home / relative_path
        if path.suffix == ".json":
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        else:
            path.mkdir(parents=True, exist_ok=True)

    r = run_sbox("all", extra_env={"HOME": str(home)})

    assert r.returncode == 0
    assert "adding" not in r.stderr
    for relative_path in agent_state_paths:
        path = home / relative_path
        assert r.stdout.count(f"--bind {path} {path}") == 1


def test_resolve_profile_unknown_exits(sbox):
    with pytest.raises(SystemExit):
        sbox.resolve_profile(None, "not-a-real-tool")


# --- resolve_workspace: detection order and cwd validation ------------------


def test_explicit_workspace(sbox, tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    workspace, rel = sbox.resolve_workspace(tmp_path, sub)
    assert workspace == tmp_path.resolve()
    assert rel == Path("sub")


def test_explicit_workspace_missing_exits(sbox, tmp_path):
    with pytest.raises(SystemExit):
        sbox.resolve_workspace(tmp_path / "does-not-exist", tmp_path)


def test_workspace_root_rel_path_is_dot(sbox, tmp_path):
    (tmp_path / ".sandbox-workspace").touch()
    workspace, rel = sbox.resolve_workspace(None, tmp_path)
    assert workspace == tmp_path.resolve()
    assert rel == Path(".")


def test_marker_detection_from_subdir(sbox, tmp_path):
    (tmp_path / "WORKSPACE").touch()
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    workspace, rel = sbox.resolve_workspace(None, sub)
    assert workspace == tmp_path.resolve()
    assert rel == Path("a/b")


def test_git_root_fallback(sbox, tmp_path):
    (tmp_path / ".git").mkdir()
    sub = tmp_path / "pkg"
    sub.mkdir()
    workspace, rel = sbox.resolve_workspace(None, sub)
    assert workspace == tmp_path.resolve()
    assert rel == Path("pkg")


def test_marker_takes_precedence_over_git(sbox, tmp_path):
    # A git root sits at the top; a marker sits deeper. The marker (innermost)
    # wins, so the workspace is the marker dir, not the outer git root.
    (tmp_path / ".git").mkdir()
    inner = tmp_path / "inner"
    inner.mkdir()
    (inner / ".sandboxrc").touch()
    workspace, _ = sbox.resolve_workspace(None, inner)
    assert workspace == inner.resolve()


def test_outermost_git_root_wins(sbox, tmp_path):
    # Nested git repos: detection picks the OUTERMOST .git.
    (tmp_path / ".git").mkdir()
    inner = tmp_path / "vendored"
    inner.mkdir()
    (inner / ".git").mkdir()
    workspace, _ = sbox.resolve_workspace(None, inner)
    assert workspace == tmp_path.resolve()


def test_no_workspace_detected_exits(sbox, tmp_path):
    # No marker and no .git anywhere up the tree.
    with pytest.raises(SystemExit):
        sbox.resolve_workspace(None, tmp_path)


def test_cwd_outside_explicit_workspace_exits(sbox, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(SystemExit):
        sbox.resolve_workspace(workspace, outside)
