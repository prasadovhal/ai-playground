"""Resume long local stages in order, with durable stage logs and no silent skips."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

import pandas as pd

from .corpus import atomic_json
from .prepare import ROOT


def stage(name: str, arguments: list[str], *, executable: str | None = None) -> None:
    log = ROOT / "logs" / f"{name}.log"
    state = ROOT / "results" / "pipeline_status.json"
    atomic_json(state, {"stage": name, "status": "running", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})
    with log.open("a", encoding="utf-8") as output:
        output.write(f"\nSTART {time.strftime('%Y-%m-%dT%H:%M:%S%z')} {arguments}\n")
        output.flush()
        process = subprocess.run([executable or sys.executable, "-B", *arguments], cwd=ROOT,
                                 stdout=output, stderr=subprocess.STDOUT)
        output.write(f"END exit={process.returncode} {time.strftime('%Y-%m-%dT%H:%M:%S%z')}\n")
    if process.returncode:
        atomic_json(state, {"stage": name, "status": "failed", "exit_code": process.returncode,
                            "log": str(log.relative_to(ROOT))})
        raise RuntimeError(f"{name} failed; inspect {log}")
    atomic_json(state, {"stage": name, "status": "complete", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")})


def corpus_ready() -> bool:
    expected = len(list((ROOT / "data/financebench_source/pdfs").glob("*.pdf")))
    if len(list((ROOT / "data/normalized").glob("*.json"))) != expected:
        return False
    summary = ROOT / "results/raw/document_processing.csv"
    if not summary.exists():
        return False
    frame = pd.read_csv(summary)
    return len(frame) == expected and frame.status.eq("complete").all()


def to_development() -> None:
    while not corpus_ready():
        print("Waiting for complete document conversion", flush=True)
        time.sleep(30)
    for name, arguments in [
        ("chunk", ["-m", "src.corpus", "chunk"]),
        ("gold", ["-m", "src.gold"]),
    ]:
        stage(name, arguments)
    stage("index", ["-m", "src.index", "build"], executable="python")
    stage("development_retrieval", ["-m", "src.retrieval_stage", "development"], executable="python")
    stage("tests", ["-m", "unittest", "discover", "-s", "tests", "-v"])
    stage("development", ["-m", "src.rag", "development"])


def final() -> None:
    for name, arguments in [
        ("freeze", ["-m", "src.freeze"]),
        ("test_retrieval", ["-m", "src.retrieval_stage", "test"]),
        ("test", ["-m", "src.rag", "test"]),
        ("asd", ["-m", "src.asd"]),
        ("summary", ["-m", "src.summarize"]),
        ("environment_final", ["-m", "src.environment", "--final"]),
        ("report", ["-m", "src.report"]),
    ]:
        stage(name, arguments, executable="python" if name == "test_retrieval" else None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=["development", "final"])
    args = parser.parse_args()
    if args.phase == "development":
        to_development()
    else:
        final()
