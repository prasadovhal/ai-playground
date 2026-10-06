"""Capture exact hardware, software, model, and source revisions."""
from __future__ import annotations

import importlib.metadata
import json
import platform
import subprocess
import sys
import time

import psutil
import requests
import yaml

from .corpus import atomic_json
from .prepare import ROOT


def model_revisions() -> dict:
    result = {}
    for name in ["bge-base-en-v1.5", "bge-reranker-base"]:
        metadata = ROOT / "data/models" / name / ".cache/huggingface/download/config.json.metadata"
        result[name] = metadata.read_text(encoding="utf-8").splitlines()[0] if metadata.exists() else None
    return result


def command(argv: list[str]) -> dict:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=30, errors="replace")
        return {"exit_code": result.returncode, "stdout": result.stdout, "stderr": result.stderr}
    except Exception as exc:
        return {"error": repr(exc)}


def capture(final: bool = False) -> dict:
    cfg = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    path = ROOT / "results/environment.json"
    prior = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    tags = requests.get(cfg["ollama"]["base_url"] + "/api/tags", timeout=30).json()
    packages = {}
    for name in ["docling", "docling-core", "huggingface-hub", "pymupdf", "torch", "sentence-transformers", "transformers",
                 "faiss-cpu", "rank-bm25", "numpy", "pandas", "PyYAML", "scipy", "requests", "psutil", "tiktoken"]:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    models = {m["name"]: {"name": m["name"], "digest": m["digest"],
                           "size": m["size"], "details": m.get("details")} for m in tags["models"]}
    requested = {}
    for role in ["generator", "evaluator"]:
        tag = cfg["models"][role]["tag"]
        requested[role] = models.get(tag)
        if not requested[role] or requested[role]["digest"] != cfg["models"][role]["digest"]:
            raise RuntimeError(f"Missing/changed required model {tag}")
    physical = psutil.virtual_memory()
    cpu_name = platform.processor()
    if sys.platform == "win32":
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            cpu_name = winreg.QueryValueEx(key, "ProcessorNameString")[0]
    data = {"experiment_start_time": prior.get("experiment_start_time", time.strftime("%Y-%m-%dT%H:%M:%S%z")),
            "experiment_end_time": time.strftime("%Y-%m-%dT%H:%M:%S%z") if final else None,
            "os": platform.platform(), "cpu": cpu_name,
            "cpu_physical_cores": psutil.cpu_count(logical=False), "cpu_logical_cores": psutil.cpu_count(),
            "ram_bytes": physical.total, "gpu": command(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"]),
            "python_version": sys.version, "python_executable": sys.executable,
            "ollama_version": requests.get(cfg["ollama"]["base_url"] + "/api/version", timeout=30).json(),
            "ollama_list": command(["ollama", "list"]), "models": requested,
            "hf_model_revisions": model_revisions(), "package_versions": packages,
            "dataset_revision": cfg["source"]["revision"],
            "workspace_git_commit": command(["git", "rev-parse", "HEAD"]).get("stdout", "").strip(),
            "financebench_git_commit": command(["git", "-C", str(ROOT / "data/financebench_source"), "rev-parse", "HEAD"]).get("stdout", "").strip()}
    atomic_json(path, data)
    return data


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--final", action="store_true")
    args = parser.parse_args()
    print(json.dumps(capture(final=args.final), indent=2))
