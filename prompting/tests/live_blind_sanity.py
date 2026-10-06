"""Optional pre-test interface check; uses a synthetic packet, not benchmark data."""
from __future__ import annotations

import json

import yaml

from src.asd import BLIND_SYSTEM, validate_blind
from src.prepare import ROOT
from src.rag import call_model


if __name__ == "__main__":
    config = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))
    packet = {"evaluation_packet": {"question": "What is the amount?", "gold_answer": "5",
                                    "mistral_answer": "5", "answer_correctness": True},
              "Explanation_A": "The answer is correct and supported by the given context.",
              "Explanation_B": "The RAG output matched the benchmark answer. Check its cited evidence."}
    result = call_model(config, model=config["models"]["evaluator"]["tag"],
                        prompt=json.dumps(packet), system=BLIND_SYSTEM,
                        max_tokens=config["ollama"]["evaluation_max_tokens"],
                        stage="blind_interface_sanity", question_id="synthetic_sanity",
                        json_output=True, retries=1)
    if result["error"]:
        raise RuntimeError(result["error"])
    print(json.dumps(validate_blind(result["response"]), indent=2))
