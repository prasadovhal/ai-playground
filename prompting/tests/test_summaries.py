"""Check that missing measurements stay missing through aggregation."""
import unittest

from src.summarize import ablation, asd_summary, flatten_asd, flatten_rag, paired_stats, rag_summary


class SummaryTests(unittest.TestCase):
    def test_raw_rows_recompute_measured_and_na_metrics(self):
        scores = {"hit_at_5": 1, "recall_at_5": 0.5, "precision_at_5": 0.2,
                  "mrr_at_5": 1.0, "ndcg_at_5": 0.61}
        rows = {"q1": {"question_id": "q1", "difficulty": "easy", "status": "success",
                       "gold_evidence": [{}], "gold_chunk_ids": ["c1", "c2"],
                       "answer_correctness": True, "faithfulness": 1.0,
                       "answer_relevance": 1.0, "unsupported_claim": False,
                       "generation_output_tokens": 12, "rag_total_latency_s": 2.5,
                       "retrieval_metrics": {"dense": scores, "hybrid": scores,
                                             "hybrid_reranker": scores}},
                "q2": {"question_id": "q2", "difficulty": "hard", "status": "failed",
                       "gold_evidence": [{}], "gold_chunk_ids": [],
                       "answer_correctness": None, "faithfulness": None,
                       "answer_relevance": None, "unsupported_claim": None}}
        flat = flatten_rag(rows)
        overall = rag_summary(flat).query("difficulty == 'overall'").iloc[0]
        self.assertEqual(overall["number_of_questions"], 2)
        self.assertEqual(overall["answer_correctness_n_valid"], 1)
        self.assertEqual(overall["answer_correctness"], 1.0)
        self.assertEqual(overall["Hit@5_n_valid"], 1)
        dense = ablation(rows).query("difficulty == 'overall' and configuration == 'Dense'").iloc[0]
        self.assertEqual(dense["Hit@5"], 1.0)

    def test_paired_summary_preserves_blind_mapping(self):
        normal = {"word_count": 20, "average_sentence_length": 10,
                  "sentence_count": 2, "maximum_sentence_length": 11,
                  "flesch_reading_ease_approx": 45, "technical_term_consistency": 0.8,
                  "unique_technical_terms": 3, "token_count": 25}
        asd = {**normal, "word_count": 14, "average_sentence_length": 7,
               "token_count": 18}
        weak = {"technical_correctness": 3, "information_coverage": 3,
                "clarity": 3, "conciseness": 3, "actionability": 3,
                "fact_coverage": 0.7, "unsupported_explanation_claims": False}
        strong = {**weak, "clarity": 4, "conciseness": 4}
        rows = {"q1": {"question_id": "q1", "difficulty": "easy", "status": "success",
                       "packet": {}, "outputs": {"normal": {"response": "Normal text"},
                                               "asd_ste": {"response": "Simple text"}},
                       "objective": {"normal": normal, "asd_ste": asd},
                       "blind_judgment": {"A": strong, "B": weak, "preference": "A"},
                       "deterministic_anchor_coverage": {"normal": 1, "asd_ste": 1}}}
        mapping = {"q1": {"A": "asd_ste", "B": "normal"}}
        flat = flatten_asd(rows, mapping)
        self.assertEqual(flat.iloc[0].winner_style, "asd_ste")
        overall = asd_summary(flat).query("difficulty == 'overall'").iloc[0]
        self.assertEqual(overall["ASD_STE_win_rate"], 1.0)
        tests = paired_stats(flat)
        self.assertIn("blind_preference", set(tests.comparison))


if __name__ == "__main__":
    unittest.main()
