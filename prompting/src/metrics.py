"""Deterministic retrieval and numerical-answer measurements."""
from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation


def retrieval_metrics(ranked_ids: list[str], gold_ids: list[str], k: int = 5) -> dict:
    gold = set(gold_ids)
    names = {"hit_at_5": None, "recall_at_5": None, "precision_at_5": None,
             "mrr_at_5": None, "ndcg_at_5": None}
    if not gold:
        return names
    selected = ranked_ids[:k]
    hits = [int(cid in gold) for cid in selected]
    relevant = sum(hits)
    ideal = sum(1.0 / math.log2(i + 2) for i in range(min(k, len(gold))))
    dcg = sum(hit / math.log2(i + 2) for i, hit in enumerate(hits))
    first = next((i + 1 for i, hit in enumerate(hits) if hit), None)
    return {"hit_at_5": int(relevant > 0), "recall_at_5": relevant / len(gold),
            "precision_at_5": relevant / k, "mrr_at_5": 1 / first if first else 0.0,
            "ndcg_at_5": dcg / ideal if ideal else None}


NUMBER = re.compile(r"(?<!\w)(\(?-?\$?\s*\d[\d,]*(?:\.\d+)?\)?)(?!\w)")
SCALE = {"thousand": Decimal(1000), "million": Decimal(1000000),
         "billion": Decimal(1000000000), "trillion": Decimal(1000000000000)}


def one_number(text: str) -> dict | None:
    """Return a single unambiguous value and unit, or None instead of guessing."""
    text = re.sub(r"\[[^\]]*\]", "", text)
    text = re.sub(r"\b(?:ctx|context|source|chunk)\s*[:#-]?\s*\w+\b", "", text, flags=re.I)
    matches = list(NUMBER.finditer(text))
    if len(matches) != 1:
        return None
    raw = matches[0].group(1).strip()
    negative = raw.startswith("(") and raw.endswith(")") or raw.startswith("-")
    currency = "$" in raw or bool(re.search(r"\b(?:USD|dollars?)\b", text, re.I))
    try:
        value = Decimal(raw.strip("()$ -").replace(",", ""))
    except InvalidOperation:
        return None
    if negative:
        value = -value
    suffix = text[matches[0].end():matches[0].end() + 35].lower()
    prefix = text[max(0, matches[0].start() - 25):matches[0].start()].lower()
    context = prefix + " " + suffix
    scale_matches = [scale for name, scale in SCALE.items() if re.search(rf"\b{name}s?\b", context)]
    if len(scale_matches) > 1:
        return None
    value *= scale_matches[0] if scale_matches else 1
    percentage = "%" in suffix[:4] or bool(re.search(r"\bpercent(?:age)?\b", suffix[:18]))
    return {"value": value, "is_percentage": percentage, "is_currency": currency,
            "scale": str(scale_matches[0]) if scale_matches else None,
            "raw": raw}


def numerical_correctness(gold_answer: str, predicted_answer: str, question: str) -> tuple[bool | None, str]:
    gold = one_number(gold_answer)
    predicted = one_number(predicted_answer)
    if not gold or not predicted:
        return None, "NA: gold or generated answer does not contain exactly one unambiguous number"
    unit_match = re.search(r"\b(?:in|of|units? of)\s+(?:USD|dollars?)?\s*(thousand|million|billion|trillion)s?\b", question, re.I)
    implied_scale = SCALE[unit_match.group(1).lower()] if unit_match else None
    if implied_scale is not None:
        for value in [gold, predicted]:
            if value["scale"] is None:
                value["value"] *= implied_scale
                value["scale"] = str(implied_scale)
    if re.search(r"\b(?:percent|percentage|in %)\b", question, re.I):
        gold["is_percentage"] = True
        predicted["is_percentage"] = True
    if gold["is_percentage"] != predicted["is_percentage"]:
        return None, "NA: percentage and non-percentage units are not safely comparable"
    # A dollar sign is a formatting marker here; neither string specifies a
    # competing currency. Magnitude and percentage units still have to agree.
    rounding = re.search(r"round\s+to\s+(?:one|two|three|\d+)\s+decimal", question, re.I)
    places = {"one": 1, "two": 2, "three": 3}
    def last_digit_tolerance(item: dict) -> Decimal:
        raw = item["raw"].replace(",", "")
        decimals = len(raw.split(".", 1)[1]) if "." in raw else 0
        return Decimal("0.5") * Decimal(10) ** -decimals * Decimal(item["scale"] or "1")
    if rounding:
        count = re.search(r"(one|two|three|\d+)", rounding.group(0), re.I).group(1).lower()
        tolerance = Decimal("0.5") * (Decimal(10) ** -int(places.get(count, count) if count.isdigit() else places[count]))
        if gold["scale"]:
            tolerance *= Decimal(gold["scale"])
    else:
        tolerance = max(last_digit_tolerance(gold), last_digit_tolerance(predicted))
    correct = abs(gold["value"] - predicted["value"]) <= tolerance
    return bool(correct), f"absolute difference {abs(gold['value']-predicted['value'])}; tolerance {tolerance}"
