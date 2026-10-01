from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

# Fixed sample drawn from the 200-row Kaggle LLM Science Exam train set.
# Using explicit IDs makes the experiment reproducible across pandas versions.
SAMPLED_IDS = [
    153, 21, 6, 26, 74, 11, 103, 31, 106, 83, 187, 52, 9, 108, 78,
    159, 179, 7, 89, 131, 194, 91, 61, 67, 190, 29, 164, 127, 54, 170,
    13, 125, 0, 151, 119, 104, 112, 132, 171, 116, 44, 136, 23, 55, 130,
    79, 100, 75, 85, 63, 12, 92, 155, 34, 184, 163, 147, 56, 176, 121,
]
OPTIONS = list("ABCDE")


def _wrong_option(row: pd.Series) -> str:
    """Pick a deterministic distractor answer text."""
    correct_label = str(row["answer"]).strip()
    wrong_label = next(label for label in OPTIONS if label != correct_label)
    return str(row[wrong_label]).strip()


def _correct_option(row: pd.Series) -> str:
    label = str(row["answer"]).strip()
    return str(row[label]).strip()


def build_eval_set(train: pd.DataFrame) -> pd.DataFrame:
    required = {"id", "prompt", "A", "B", "C", "D", "E", "answer"}
    missing = required - set(train.columns)
    if missing:
        raise ValueError(f"Missing columns: {sorted(missing)}")

    train = train.copy()
    train["id"] = train["id"].astype(int)
    by_id = train.set_index("id", drop=False)
    unavailable = [i for i in SAMPLED_IDS if i not in by_id.index]
    if unavailable:
        raise ValueError(f"Expected Kaggle IDs not found: {unavailable}")

    base = by_id.loc[SAMPLED_IDS].reset_index(drop=True)
    records: list[dict] = []

    # Offset unrelated evidence far enough away to minimize accidental topical overlap.
    other_offset = 17

    for i, row in base.iterrows():
        other = base.iloc[(i + other_offset) % len(base)]
        correct = _correct_option(row)
        distractor = _wrong_option(row)
        other_fact = _correct_option(other)
        prompt = str(row["prompt"]).strip()
        source_id = int(row["id"])

        variants = [
            {
                "variant": "supported_correct",
                "context": f"Evidence: {correct}",
                "proposed_answer": correct,
                "answerable": 1,
                "groundedness": 3,
                "relevance": 3,
            },
            {
                "variant": "unsupported_distractor",
                "context": f"Evidence: {correct}",
                "proposed_answer": distractor,
                "answerable": 1,
                "groundedness": 0,
                "relevance": 3,
            },
            {
                "variant": "partially_supported",
                "context": f"Evidence: {correct}",
                "proposed_answer": f"{correct} Additional claim: {distractor}",
                "answerable": 1,
                "groundedness": 2,
                "relevance": 3,
            },
            {
                "variant": "grounded_irrelevant",
                "context": (
                    f"Evidence relevant to the question: {correct}\n"
                    f"Unrelated retrieved evidence: {other_fact}"
                ),
                "proposed_answer": other_fact,
                "answerable": 1,
                "groundedness": 3,
                "relevance": 0,
            },
            {
                "variant": "missing_evidence",
                "context": f"Retrieved evidence: {other_fact}",
                "proposed_answer": correct,
                "answerable": 0,
                "groundedness": 0,
                "relevance": 3,
            },
        ]

        for v in variants:
            records.append(
                {
                    "case_id": f"kaggle_{source_id:03d}_{v['variant']}",
                    "source_id": source_id,
                    "question": prompt,
                    "gold_option": str(row["answer"]).strip(),
                    "gold_answer": correct,
                    "distractor": distractor,
                    **v,
                }
            )

    out = pd.DataFrame(records)
    if len(out) != 300:
        raise AssertionError(f"Expected 300 cases, got {len(out)}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, help="Kaggle train.csv")
    parser.add_argument("--out", default="data/eval_300.jsonl")
    args = parser.parse_args()

    train = pd.read_csv(args.input)
    eval_df = build_eval_set(train)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    eval_df.to_json(out, orient="records", lines=True, force_ascii=False)

    print(f"Wrote {len(eval_df)} controlled evaluation cases to {out}")
    print("Variant counts:")
    print(eval_df["variant"].value_counts().sort_index().to_string())
    print("\nAnswerability:")
    print(eval_df["answerable"].value_counts().sort_index().to_string())
    print("\nGroundedness:")
    print(eval_df["groundedness"].value_counts().sort_index().to_string())
    print("\nRelevance:")
    print(eval_df["relevance"].value_counts().sort_index().to_string())


if __name__ == "__main__":
    main()
