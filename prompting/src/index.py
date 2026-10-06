"""Exact normalized dense index, BM25, RRF, and BGE reranking."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pickle
import re
import time
from pathlib import Path

import faiss
import numpy as np
import torch
import yaml
from rank_bm25 import BM25Okapi
from sentence_transformers import CrossEncoder, SentenceTransformer

from .corpus import atomic_json
from .prepare import ROOT


def lexical_tokens(text: str) -> list[str]:
    return re.findall(r"[\w]+(?:[.,%-][\w]+)*", text.lower())


def load_chunks() -> list[dict]:
    return [json.loads(line) for line in (ROOT / "data/chunks.jsonl").open(encoding="utf-8") if line.strip()]


def model_revisions() -> dict:
    result = {}
    for name in ["bge-base-en-v1.5", "bge-reranker-base"]:
        metadata = ROOT / "data/models" / name / ".cache/huggingface/download/config.json.metadata"
        result[name] = metadata.read_text(encoding="utf-8").splitlines()[0] if metadata.exists() else None
    return result


def build(config: dict) -> dict:
    chunks_path = ROOT / "data/chunks.jsonl"
    if not chunks_path.exists():
        raise RuntimeError("Chunk corpus missing")
    chunks = load_chunks()
    if not chunks:
        raise RuntimeError("Chunk corpus empty")
    chunk_hash = hashlib.sha256(chunks_path.read_bytes()).hexdigest()
    index_dir = ROOT / "data/index"
    index_dir.mkdir(parents=True, exist_ok=True)
    progress_file = ROOT / "results/raw/embedding_progress.json"
    os.environ["HF_HUB_OFFLINE"] = "1"
    device = config["embedding"]["indexing_device"]
    if device == "cuda" and not torch.cuda.is_available():
        raise RuntimeError("Configured CUDA indexing device is unavailable")
    model = SentenceTransformer(str(ROOT / "data/models/bge-base-en-v1.5"), device=device, local_files_only=True)
    dimension = int(model.get_sentence_embedding_dimension())
    count = len(chunks)
    vectors_path = index_dir / "embeddings.float32"
    if progress_file.exists():
        progress = json.loads(progress_file.read_text(encoding="utf-8"))
        if progress["chunk_sha256"] != chunk_hash or progress["chunk_count"] != count or progress["dimension"] != dimension:
            raise RuntimeError("Embedding checkpoint differs from frozen chunk corpus")
        completed = progress["completed"]
        vectors = np.memmap(vectors_path, dtype="float32", mode="r+", shape=(count, dimension))
    else:
        completed = 0
        vectors = np.memmap(vectors_path, dtype="float32", mode="w+", shape=(count, dimension))
    started = time.perf_counter()
    batch_size = 32
    for begin in range(completed, count, batch_size):
        end = min(count, begin + batch_size)
        batch = [row["text"] for row in chunks[begin:end]]
        embeddings = model.encode(batch, batch_size=batch_size, normalize_embeddings=True,
                                  convert_to_numpy=True, show_progress_bar=False)
        vectors[begin:end] = np.asarray(embeddings, dtype="float32")
        vectors.flush()
        atomic_json(progress_file, {"chunk_sha256": chunk_hash, "chunk_count": count,
                                    "dimension": dimension, "completed": end, "elapsed_this_session_s": time.perf_counter() - started})
        if end % 320 == 0 or end == count:
            print(f"embedded {end}/{count}", flush=True)
    dense = faiss.IndexFlatIP(dimension)
    for begin in range(0, count, 20_000):
        dense.add(np.asarray(vectors[begin:begin + 20_000]))
    faiss.write_index(dense, str(index_dir / "dense.faiss"))
    tokenized = [lexical_tokens(row["text"]) for row in chunks]
    bm25 = BM25Okapi(tokenized)
    with (index_dir / "bm25.pkl").open("wb") as out:
        pickle.dump(bm25, out, protocol=pickle.HIGHEST_PROTOCOL)
    import importlib.metadata
    import sys

    manifest = {"chunk_count": count, "chunk_sha256": chunk_hash,
                "embedding_model": config["embedding"]["model"], "dimension": dimension,
                "normalized": True, "faiss_index": "IndexFlatIP", "bm25": "BM25Okapi",
                "indexing_device": device, "python_executable": sys.executable,
                "python_version": sys.version, "torch_version": torch.__version__,
                "transformers_version": importlib.metadata.version("transformers"),
                "sentence_transformers_version": importlib.metadata.version("sentence-transformers"),
                "faiss_cpu_version": importlib.metadata.version("faiss-cpu"),
                "rank_bm25_version": importlib.metadata.version("rank-bm25"),
                "numpy_version": np.__version__,
                "model_revisions": model_revisions(), "embedding_seconds_this_session": time.perf_counter() - started}
    atomic_json(ROOT / "results/index_manifest.json", manifest)
    return manifest


class Retrieval:
    def __init__(self, config: dict):
        self.config = config
        self.chunks = load_chunks()
        manifest = json.loads((ROOT / "results/index_manifest.json").read_text(encoding="utf-8"))
        if manifest["chunk_sha256"] != hashlib.sha256((ROOT / "data/chunks.jsonl").read_bytes()).hexdigest():
            raise RuntimeError("Index and chunk corpus mismatch")
        os.environ["HF_HUB_OFFLINE"] = "1"
        self.embedder = SentenceTransformer(str(ROOT / "data/models/bge-base-en-v1.5"),
                                            device=config["embedding"]["retrieval_device"], local_files_only=True)
        self.dense = faiss.read_index(str(ROOT / "data/index/dense.faiss"))
        with (ROOT / "data/index/bm25.pkl").open("rb") as source:
            self.bm25 = pickle.load(source)
        self.reranker = CrossEncoder(str(ROOT / "data/models/bge-reranker-base"),
                                     device=config["reranker"]["device"], max_length=512)

    def search(self, question: str) -> dict:
        start = time.perf_counter()
        dense_k = self.config["retrieval"]["dense_candidates"]
        bm25_k = self.config["retrieval"]["bm25_candidates"]
        query = self.embedder.encode([question], normalize_embeddings=True, convert_to_numpy=True).astype("float32")
        dense_scores, dense_indices = self.dense.search(query, dense_k)
        dense = [{"chunk_id": self.chunks[int(i)]["chunk_id"], "index": int(i),
                  "rank": rank + 1, "score": float(score)}
                 for rank, (i, score) in enumerate(zip(dense_indices[0], dense_scores[0])) if i >= 0]
        lexical_scores = self.bm25.get_scores(lexical_tokens(question))
        bm25_indices = np.argsort(-lexical_scores, kind="stable")[:bm25_k]
        bm25 = [{"chunk_id": self.chunks[int(i)]["chunk_id"], "index": int(i),
                 "rank": rank + 1, "score": float(lexical_scores[i])}
                for rank, i in enumerate(bm25_indices)]
        k = self.config["retrieval"]["rrf_k"]
        fused = {}
        for ranked in [dense, bm25]:
            for row in ranked:
                i = row["index"]
                fused[i] = fused.get(i, 0.0) + 1.0 / (k + row["rank"])
        fused_ranked = sorted(fused, key=lambda i: (-fused[i], self.chunks[i]["chunk_id"]))
        hybrid = [{"chunk_id": self.chunks[i]["chunk_id"], "index": i, "rank": rank + 1,
                   "rrf_score": fused[i]} for rank, i in enumerate(fused_ranked)]
        retrieval_latency = time.perf_counter() - start
        rerank_start = time.perf_counter()
        pairs = [(question, self.chunks[i]["text"]) for i in fused_ranked]
        scores = self.reranker.predict(pairs, batch_size=16, show_progress_bar=False)
        reranked_indices = sorted(range(len(fused_ranked)), key=lambda j: (-float(scores[j]), self.chunks[fused_ranked[j]]["chunk_id"]))
        reranked = [{"chunk_id": self.chunks[fused_ranked[j]]["chunk_id"],
                     "index": fused_ranked[j], "rank": rank + 1,
                     "reranker_score": float(scores[j]), "rrf_score": fused[fused_ranked[j]]}
                    for rank, j in enumerate(reranked_indices)]
        rerank_latency = time.perf_counter() - rerank_start
        top_k = self.config["retrieval"]["final_top_k"]
        return {"dense": dense, "bm25": bm25, "hybrid": hybrid, "reranked": reranked,
                "final": reranked[:top_k], "retrieval_latency_s": retrieval_latency,
                "reranking_latency_s": rerank_latency}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["build", "probe"])
    args = parser.parse_args()
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    if args.stage == "build":
        print(json.dumps(build(config), indent=2))
    else:
        retrieval = Retrieval(config)
        print(json.dumps(retrieval.search("What is 3M's 2018 capital expenditure?")["final"], indent=2))
