"""Launch the resumable stage controller without an interactive window."""
from __future__ import annotations

import argparse
import subprocess
import sys

from .prepare import ROOT


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["development", "final", "autofinal"])
    args = parser.parse_args()
    stdout = (ROOT / "logs" / f"{args.phase}_orchestrator_stdout.log").open("a", encoding="utf-8")
    stderr = (ROOT / "logs" / f"{args.phase}_orchestrator_stderr.log").open("a", encoding="utf-8")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    command = ([sys.executable, "-B", "-m", "src.autorun_final"] if args.phase == "autofinal" else
               [sys.executable, "-B", "-m", "src.orchestrate", args.phase])
    process = subprocess.Popen(command,
                               cwd=ROOT, stdout=stdout, stderr=stderr, creationflags=flags)
    (ROOT / "logs" / f"{args.phase}_orchestrator_pid.txt").write_text(str(process.pid), encoding="utf-8")
    print(process.pid)
