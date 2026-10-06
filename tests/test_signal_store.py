import tempfile
import unittest
from pathlib import Path

from signal_store import evaluate_due_outcomes, outcome_summary, record_observations


class SignalStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "history.sqlite3"

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def row(price):
        return {"symbol": "ETHUSDT", "price": price, "score": 70, "feature": "test"}

    def test_records_observations_and_measures_due_return(self):
        start = 1_000_000.0
        record_observations([self.row(100)], self.db_path, start)
        record_observations([self.row(110)], self.db_path, start + 3600 + 600)

        completed = evaluate_due_outcomes(self.db_path, start + 4200)
        self.assertEqual(completed, 1)
        self.assertEqual(
            outcome_summary(self.db_path)["1h"],
            {
                "count": 1,
                "hit_rate_pct": 100.0,
                "mean_return_pct": 10.0,
                "median_return_pct": 10.0,
            },
        )

    def test_rejects_outcome_sample_far_after_target(self):
        start = 1_000_000.0
        record_observations([self.row(100)], self.db_path, start)
        record_observations([self.row(120)], self.db_path, start + 3600 + 3600)

        self.assertEqual(evaluate_due_outcomes(self.db_path, start + 7200), 0)
        self.assertEqual(outcome_summary(self.db_path), {})


if __name__ == "__main__":
    unittest.main()
