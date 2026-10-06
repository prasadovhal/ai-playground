import unittest
import json

from src.asd import validate_blind
from src.corpus import chunk_page
from src.metrics import numerical_correctness, retrieval_metrics
from src.prepare import classify
from src.rag import classify_failure, parse_evaluation


class FakeTokenizer:
    def encode(self, text):
        return [ord(c) for c in text]

    def decode(self, tokens):
        return "".join(chr(c) for c in tokens)


class CoreTests(unittest.TestCase):
    def test_retrieval_metrics_with_multiple_gold_chunks(self):
        result = retrieval_metrics(["wrong", "a", "other", "b", "none"], ["a", "b"])
        self.assertEqual(result["hit_at_5"], 1)
        self.assertEqual(result["recall_at_5"], 1)
        self.assertEqual(result["precision_at_5"], 0.4)
        self.assertEqual(result["mrr_at_5"], 0.5)
        self.assertIsNone(retrieval_metrics(["x"], [])["ndcg_at_5"])

    def test_numeric_units_and_ambiguity(self):
        self.assertTrue(numerical_correctness("$1,577.00", "$1.577 billion", "Give answer in USD millions.")[0])
        self.assertTrue(numerical_correctness("$1,577", "1,577", "What was the amount?")[0])
        self.assertFalse(numerical_correctness("$1,577", "$1,578", "Give answer in USD millions.")[0])
        self.assertIsNone(numerical_correctness("$10 or $12", "$10", "How much?")[0])
        self.assertFalse(numerical_correctness("25%", "0.25", "What is the percentage?")[0])

    def test_structural_difficulty_ignores_answer(self):
        row = {"question": "What was the 2022 cash amount?", "question_reasoning": "Information extraction",
               "evidence": [{}], "answer": "$10"}
        first = classify(row)
        row["answer"] = "a completely different answer"
        self.assertEqual(first, classify(row))
        self.assertEqual(first[0], "easy")

    def test_recursive_chunks_obey_token_limit(self):
        text = "Revenue | 2018 | 2017\n" + "Cash | 100 | 90\n" * 12
        chunks = chunk_page(text, FakeTokenizer(), 80, 12)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 80 for chunk in chunks))

    def test_structured_judgments_reject_missing_or_out_of_range_values(self):
        answer = {"answer_correctness": True, "faithfulness": 1.0,
                  "answer_relevance": 0.5, "unsupported_claim": False,
                  "incomplete_answer": False, "reasoning_failure": False,
                  "insufficient_context": False, "reason": "supported"}
        self.assertEqual(parse_evaluation(json.dumps(answer)), answer)
        answer["faithfulness"] = 1.5
        with self.assertRaises(ValueError):
            parse_evaluation(json.dumps(answer))
        score = {"technical_correctness": 4, "information_coverage": 4,
                 "clarity": 5, "conciseness": 4, "actionability": 3,
                 "fact_coverage": 0.8, "unsupported_explanation_claims": False}
        self.assertEqual(validate_blind(json.dumps({"A": score, "B": score,
                                                    "preference": "Tie"}))["preference"], "Tie")
        score["clarity"] = 6
        with self.assertRaises(ValueError):
            validate_blind(json.dumps({"A": score, "B": score, "preference": "A"}))

    def test_numeric_semantic_disagreement_is_retained_as_failure(self):
        row = {"answer_correctness": True, "faithfulness": 1.0, "answer_relevance": 1.0,
               "numeric_correctness": True, "numeric_correctness_reason": "same number",
               "qwen_evaluation": {"answer_correctness": False, "reason": "wrong subject"},
               "gold_chunk_ids": ["g"], "retrieval": {"hybrid": [{"chunk_id": "g"}],
                                                    "final": [{"chunk_id": "g"}]},
               "generated_answer": "100"}
        self.assertEqual(classify_failure(row)[0], "G5")


if __name__ == "__main__":
    unittest.main()
