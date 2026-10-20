import tempfile
import unittest
from pathlib import Path

from controlroom.config import ConfigurationError, load_config


VALID_ENV = """\
JIRA_SITE=https://jira.example.test/
JIRA_READ_ONLY_USER=collector@example.test
JIRA_READ_ONLY_TOKEN=top-secret-token
GITHUB_REPOS=jooservices/ai-flow, jooservices/control-room
DATA_DIR=./data
COLLECT_INTERVAL_SECONDS=45
"""


class ConfigTest(unittest.TestCase):
    def write_env(self, content: str) -> Path:
        temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(temporary_directory.cleanup)
        env_path = Path(temporary_directory.name) / ".env"
        env_path.write_text(content, encoding="utf-8")
        return env_path

    def test_loads_and_normalizes_required_settings(self):
        config = load_config(self.write_env(VALID_ENV))

        self.assertEqual(config.jira_site, "https://jira.example.test")
        self.assertEqual(
            config.github_repos,
            ("jooservices/ai-flow", "jooservices/control-room"),
        )
        self.assertEqual(config.data_dir, Path("./data"))
        self.assertEqual(config.interval_seconds, 45)

    def test_missing_variable_names_are_reported_without_values(self):
        env_path = self.write_env(
            VALID_ENV.replace("JIRA_READ_ONLY_TOKEN=top-secret-token\n", "")
        )

        with self.assertRaises(ConfigurationError) as raised:
            load_config(env_path)

        self.assertIn("JIRA_READ_ONLY_TOKEN", str(raised.exception))
        self.assertNotIn("top-secret-token", str(raised.exception))

    def test_credentials_are_not_in_config_repr(self):
        config = load_config(self.write_env(VALID_ENV))

        self.assertNotIn("top-secret-token", repr(config))
        self.assertNotIn("collector@example.test", repr(config))

    def test_env_example_loads_all_required_settings(self):
        example_path = Path(__file__).parents[1] / ".env.example"

        config = load_config(example_path)

        self.assertEqual(config.interval_seconds, 30)
        self.assertEqual(
            config.github_repos,
            ("jooservices/ai-flow", "jooservices/control-room"),
        )


if __name__ == "__main__":
    unittest.main()
