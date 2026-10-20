import unittest

from controlroom.model import DOCUMENT_NAMES, SNAPSHOT_VERSION, empty_snapshot


class SnapshotModelTest(unittest.TestCase):
    def test_snapshot_has_versioned_plain_dict_documents(self):
        snapshot = empty_snapshot()

        self.assertEqual(snapshot["version"], SNAPSHOT_VERSION)
        self.assertEqual(set(snapshot) - {"version"}, set(DOCUMENT_NAMES))
        self.assertEqual(
            snapshot["flow"]["statusMedianMinutes"],
            {"24h": {}, "7d": {}, "30d": {}},
        )
        self.assertEqual(
            snapshot["ai"]["usageByRole"],
            {"24h": [], "7d": [], "30d": []},
        )
        for name in DOCUMENT_NAMES:
            self.assertIs(type(snapshot[name]), dict)

    def test_each_call_returns_fresh_nested_documents(self):
        first = empty_snapshot()
        second = empty_snapshot()
        first["projects"]["list"].append({"key": "JOOCR-9"})

        self.assertEqual(second["projects"]["list"], [])


if __name__ == "__main__":
    unittest.main()
