import unittest

from controlroom.analytics import eta


class EtaTests(unittest.TestCase):
    def test_uses_inclusive_quartiles_for_the_finished_ticket_samples(self):
        self.assertEqual(
            eta(1, [1, 2, 3, 4, 5, 6, 7, 8], 1),
            {"low": 2.75, "middle": 4.5, "high": 6.25},
        )

    def test_returns_none_with_fewer_than_five_samples(self):
        self.assertIsNone(eta(3, [10, 20, 30, 40], 2))

    def test_scales_estimate_by_remaining_tickets_and_parallel_workers(self):
        self.assertEqual(
            eta(4, [1, 2, 3, 4, 5], 2),
            {"low": 4.0, "middle": 6.0, "high": 8.0},
        )

    def test_returns_none_when_worker_capacity_is_unavailable(self):
        self.assertIsNone(eta(1, [1, 2, 3, 4, 5], None))
        self.assertIsNone(eta(1, [1, 2, 3, 4, 5], 0))


if __name__ == "__main__":
    unittest.main()
