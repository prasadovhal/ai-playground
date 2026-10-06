"""Resumable full-PDF conversion and token-aware page chunking."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

import pandas as pd
import pymupdf
import yaml

from .prepare import ROOT


class TokenAdapter:
    """Count the same wordpieces the embedding model will consume."""

    def __init__(self):
        from transformers import AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(ROOT / "data/models/bge-base-en-v1.5", local_files_only=True)

    def encode(self, text: str) -> list[int]:
        return self.tokenizer.encode(text, add_special_tokens=False)

    def decode(self, ids: list[int]) -> str:
        return self.tokenizer.decode(ids, skip_special_tokens=True)


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def page_rows(page: pymupdf.Page) -> str:
    """Use word positions so table rows and left-to-right columns stay associated."""
    words = page.get_text("words")
    rows: list[list] = []
    for word in sorted(words, key=lambda w: ((w[1] + w[3]) / 2, w[0])):
        mid_y = (word[1] + word[3]) / 2
        if rows and abs(rows[-1][0] - mid_y) <= 3:
            rows[-1][1].append(word)
        else:
            rows.append([mid_y, [word]])
    lines = []
    for _, row in rows:
        row.sort(key=lambda w: w[0])
        parts: list[str] = []
        last_x = None
        for word in row:
            if last_x is not None:
                parts.append(" | " if word[0] - last_x > 22 else " ")
            parts.append(word[4])
            last_x = word[2]
        line = "".join(parts).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def section_from_text(text: str, previous: str) -> str:
    for line in text.splitlines()[:25]:
        line = line.strip("# |\t")
        if 8 <= len(line) <= 100 and (line.isupper() or re.match(r"^(consolidated |statements? of |notes to |management['’]s )", line, re.I)):
            return line
    return previous


def convert_pdf(path: Path, cfg: dict, converter) -> dict:
    start = time.perf_counter()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    record = {"document_id": path.stem, "document_name": path.name, "source_sha256": digest,
              "source_bytes": path.stat().st_size, "pages": [], "parser": None, "warnings": [],
              "table_count": None, "seconds": None}
    with pymupdf.open(path) as probe:
        page_count = len(probe)
        repaired = bool(probe.is_repaired)
    record["source_pages"] = page_count
    if repaired:
        record["warnings"].append("PDF required PyMuPDF repair to open")
    use_docling = page_count <= cfg["document_processing"]["docling_max_pages"]
    if use_docling:
        try:
            result = converter.convert(path, raises_on_error=False)
            if result.document is None or not result.document.pages:
                raise RuntimeError(f"Docling returned {result.status} without pages")
            record["parser"] = "docling"
            record["table_count"] = len(result.document.tables)
            section = path.stem
            for page_no in sorted(result.document.pages):
                markdown = result.document.export_to_markdown(page_no=page_no).strip()
                section = section_from_text(markdown, section)
                record["pages"].append({"page": page_no, "section": section, "text": markdown})
            if str(result.status).lower().find("success") < 0:
                record["warnings"].append(f"Docling status: {result.status}")
        except Exception as exc:
            record["warnings"].append(f"Docling failed; PyMuPDF fallback: {type(exc).__name__}: {exc}")
            record["pages"] = []
    if not record["pages"]:
        record["parser"] = "pymupdf_positional_rows"
        section = path.stem
        with pymupdf.open(path) as doc:
            for index, page in enumerate(doc):
                text = page_rows(page)
                section = section_from_text(text, section)
                record["pages"].append({"page": index + 1, "section": section, "text": text})
    record["empty_pages"] = sum(not p["text"] for p in record["pages"])
    if record["empty_pages"]:
        record["warnings"].append(f"{record['empty_pages']} pages had no extractable text; OCR disabled")
    record["seconds"] = time.perf_counter() - start
    return record


def make_converter(cfg: dict):
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions()
    options.do_ocr = cfg["document_processing"]["do_ocr"]
    options.do_table_structure = cfg["document_processing"]["do_table_structure"]
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})


def conversion_summary(normalized: Path, expected_paths: list[Path]) -> pd.DataFrame:
    rows = []
    for pdf in expected_paths:
        target = normalized / f"{pdf.stem}.json"
        if target.exists():
            item = json.loads(target.read_text(encoding="utf-8"))
            rows.append({"document_id": pdf.stem, "parser": item["parser"], "source_pages": item["source_pages"],
                         "parsed_pages": len(item["pages"]), "empty_pages": item["empty_pages"],
                         "table_count": item["table_count"], "seconds": item["seconds"],
                         "warnings": json.dumps(item["warnings"]), "status": "complete"})
        else:
            rows.append({"document_id": pdf.stem, "status": "pending"})
    return pd.DataFrame(rows)


def convert_all(cfg: dict, *, limit: int | None = None) -> None:
    pdfs = sorted((ROOT / cfg["source"]["pdf_directory"]).glob("*.pdf"))
    normalized = ROOT / "data/normalized"
    normalized.mkdir(parents=True, exist_ok=True)
    converter = None
    completed = 0
    for pdf in pdfs:
        target = normalized / f"{pdf.stem}.json"
        if target.exists():
            prior = json.loads(target.read_text(encoding="utf-8"))
            if prior["source_sha256"] == hashlib.sha256(pdf.read_bytes()).hexdigest():
                continue
            raise RuntimeError(f"Source PDF changed after conversion: {pdf.name}")
        try:
            if converter is None:
                converter = make_converter(cfg)
            record = convert_pdf(pdf, cfg, converter)
            atomic_json(target, record)
            print(f"converted {pdf.name} {record['source_pages']} pages {record['parser']} {record['seconds']:.1f}s", flush=True)
        except Exception as exc:
            print(f"FAILED {pdf.name}: {type(exc).__name__}: {exc}", flush=True)
            with (ROOT / "results/raw/document_processing_errors.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps({"document_id": pdf.stem, "error": repr(exc), "time": time.time()}) + "\n")
        completed += 1
        if completed % 10 == 0:
            conversion_summary(normalized, pdfs).to_csv(ROOT / "results/raw/document_processing.csv", index=False)
        if limit and completed >= limit:
            break
    conversion_summary(normalized, pdfs).to_csv(ROOT / "results/raw/document_processing.csv", index=False)


def recursive_units(text: str, encoding, limit: int) -> list[str]:
    if len(encoding.encode(text)) <= limit:
        return [text]
    for separator in ["\n\n", "\n", ". ", " "]:
        parts = text.split(separator)
        if len(parts) > 1 and sum(bool(part) for part in parts) > 1:
            result = []
            for index, part in enumerate(parts):
                segment = part + (separator if index < len(parts) - 1 else "")
                if segment.strip():
                    result.extend(recursive_units(segment, encoding, limit))
            return result
    tokens = encoding.encode(text)
    return [encoding.decode(tokens[i:i + limit]) for i in range(0, len(tokens), limit)]


def chunk_page(text: str, encoding, target: int, overlap: int) -> list[str]:
    if not text.strip():
        return []
    units = recursive_units(text, encoding, target)
    chunks: list[str] = []
    current = ""
    for unit in units:
        if current and len(encoding.encode(current + unit)) > target:
            chunks.append(current.strip())
            tail = encoding.encode(current)[-overlap:]
            current = encoding.decode(tail).lstrip() + "\n" + unit
            if len(encoding.encode(current)) > target:
                # A near-target unit may leave no room for the overlap.
                current = unit
        else:
            current += unit
    if current.strip():
        chunks.append(current.strip())
    return chunks


def build_chunks(cfg: dict) -> dict:
    normalized = ROOT / "data/normalized"
    pdfs = sorted((ROOT / cfg["source"]["pdf_directory"]).glob("*.pdf"))
    if sum((normalized / f"{p.stem}.json").exists() for p in pdfs) != len(pdfs):
        raise RuntimeError("Full corpus conversion incomplete; refusing to make a partial index")
    encoding = TokenAdapter()
    target = cfg["chunking"]["target_tokens"]
    overlap = cfg["chunking"]["overlap_tokens"]
    chunk_file = ROOT / "data/chunks.jsonl"
    temporary = chunk_file.with_suffix(".jsonl.partial")
    count = 0
    with temporary.open("w", encoding="utf-8") as out:
        for pdf in pdfs:
            record = json.loads((normalized / f"{pdf.stem}.json").read_text(encoding="utf-8"))
            for page in record["pages"]:
                for index, text in enumerate(chunk_page(page["text"], encoding, target, overlap)):
                    chunk_id = f"{pdf.stem}:p{page['page']:04d}:c{index:03d}"
                    out.write(json.dumps({"chunk_id": chunk_id, "document_id": pdf.stem,
                                          "document_name": pdf.name, "page": page["page"],
                                          "section": page["section"], "text": text,
                                          "token_count": len(encoding.encode(text)), "parser": record["parser"]},
                                         ensure_ascii=False) + "\n")
                    count += 1
            print(f"chunked {pdf.name}", flush=True)
    temporary.replace(chunk_file)
    manifest = {"document_count": len(pdfs), "chunk_count": count,
                "chunk_size_tokens": target, "overlap_tokens": overlap,
                "tokenizer": cfg["chunking"]["tokenizer"],
                "chunk_sha256": hashlib.sha256(chunk_file.read_bytes()).hexdigest()}
    atomic_json(ROOT / "results/chunk_manifest.json", manifest)
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("stage", choices=["convert", "chunk"])
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    if args.stage == "convert":
        convert_all(config, limit=args.limit)
    else:
        print(json.dumps(build_chunks(config), indent=2))
