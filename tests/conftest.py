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


@pytest.fixture(scope="session")
def sbox():
    return _load_sbox()


@pytest.fixture
def run_sbox(tmp_path):
    """Run the real ``sbox --dry-run`` in a throwaway git workspace.

    Returns ``callable(*args) -> CompletedProcess``. ``--dry-run`` prints the
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

    def _run(*args):
        return subprocess.run(
            [sys.executable, str(SBOX_PATH), "--dry-run", *args],
            cwd=workspace,
            env=env,
            capture_output=True,
            text=True,
        )

    return _run
