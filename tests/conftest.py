"""Shared fixtures: load the extensionless sbox script, and run it end-to-end."""

import importlib.machinery
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

SBOX_PATH = Path(__file__).resolve().parent.parent / "sbox"


def _load_sbox():
    loader = importlib.machinery.SourceFileLoader("sbox", str(SBOX_PATH))
    spec = importlib.util.spec_from_loader("sbox", loader)
    module = importlib.util.module_from_spec(spec)
    # The script guards execution behind `if __name__ == "__main__"`, so loading
    # it under the name "sbox" imports the definitions without running main().
    loader.exec_module(module)
    return module


# Runs sbox's main() with its containment probe stubbed. The real probe writes
# into the account's home directory, which a test cannot make read-only
# without a sandbox; the probe itself has unit tests instead.
_STUBBED_PROBE_DRIVER = """
import importlib.machinery, importlib.util, sys
path, confined, *argv = sys.argv[1:]
loader = importlib.machinery.SourceFileLoader("sbox", path)
module = importlib.util.module_from_spec(importlib.util.spec_from_loader("sbox", loader))
loader.exec_module(module)
module.home_is_write_protected = lambda home: confined == "1"
sys.argv = [path, *argv]
sys.exit(module.main())
"""


def sbox_argv(confined=None):
    """The argv prefix that runs sbox; ``confined`` stubs the probe's answer."""
    if confined is None:
        return [sys.executable, str(SBOX_PATH)]
    return [
        sys.executable, "-c", _STUBBED_PROBE_DRIVER, str(SBOX_PATH),
        "1" if confined else "0",
    ]


@pytest.fixture(scope="session")
def sbox_cmd():
    return sbox_argv


@pytest.fixture(scope="session")
def sbox():
    return _load_sbox()


@pytest.fixture(scope="session")
def sbox_path():
    """The script itself, for tests that run it rather than import it."""
    return SBOX_PATH


@pytest.fixture
def run_sbox(tmp_path):
    """Run the real ``sbox --dry-run`` in a throwaway git workspace.

    Returns ``callable(*args, extra_env=None, confined=None) ->
    CompletedProcess``; extra_env overlays the child's environment, e.g. to
    point HOME at a throwaway directory, and confined stubs the pass-through
    containment probe (see sbox_argv). ``--dry-run`` prints the
    assembled bwrap command to stdout instead of executing it; check_bubblewrap
    still needs bwrap on PATH, so a no-op stub is provided. The working
    directory is a git root so workspace detection succeeds.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / ".git").mkdir()

    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "bwrap"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)

    env = {**os.environ, "PATH": f"{bindir}{os.pathsep}{os.environ.get('PATH', '')}"}

    def _run(*args, extra_env=None, confined=None):
        return subprocess.run(
            [*sbox_argv(confined), "--dry-run", *args],
            cwd=workspace,
            env={**env, **(extra_env or {})},
            capture_output=True,
            text=True,
        )

    return _run
