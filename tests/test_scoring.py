import unittest

from alpha_radar import score_signal


class ScoreSignalTests(unittest.TestCase):
    def test_aligned_momentum_volume_and_oi_rank_above_flat_market(self):
        flat = score_signal(0, 1, 0, 0)
        aligned = score_signal(2, 3, 2, 0.0001)
        self.assertGreater(aligned["score"], flat["score"])
        self.assertGreater(aligned["components"]["price_oi_agreement"], 0)

    def test_extreme_positive_funding_reduces_score(self):
        normal = score_signal(1, 2, 1, 0.0001)
        crowded = score_signal(1, 2, 1, 0.001)
        self.assertLess(crowded["score"], normal["score"])
        self.assertGreater(crowded["components"]["funding_penalty"], 0)

    def test_score_stays_within_bounds(self):
        self.assertLessEqual(score_signal(500, 500, 500, -1, 1)["score"], 100)
        self.assertEqual(score_signal(-500, 0, None, 1)["score"], 0)

    def test_missing_oi_does_not_claim_oi_confirmation(self):
        result = score_signal(1, 2, None, None)
        self.assertEqual(result["components"]["open_interest"], 0)
        self.assertEqual(result["components"]["price_oi_agreement"], 0)

    def test_positive_taker_buy_imbalance_adds_bullish_pressure_points(self):
        balanced = score_signal(1, 2, None, None, 0)
        buy_pressure = score_signal(1, 2, None, None, 0.5)
        sell_pressure = score_signal(1, 2, None, None, -0.5)
        self.assertGreater(buy_pressure["score"], balanced["score"])
        self.assertEqual(sell_pressure["components"]["spot_taker_buy_pressure"], 0)


if __name__ == "__main__":
    unittest.main()
