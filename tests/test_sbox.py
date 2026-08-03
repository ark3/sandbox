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


# --- resolve_profile --------------------------------------------------------


def test_resolve_profile_explicit_wins(sbox):
    assert sbox.resolve_profile("none", "claude") == "none"


def test_resolve_profile_auto_from_command(sbox):
    assert sbox.resolve_profile(None, "codex") == "codex"


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
