import unittest

import controlroom


class SmokeTest(unittest.TestCase):
    def test_package_has_version(self):
        self.assertTrue(controlroom.__version__)


if __name__ == "__main__":
    unittest.main()
