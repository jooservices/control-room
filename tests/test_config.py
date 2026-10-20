import tempfile
import unittest
from pathlib import Path

from controlroom.config import ConfigurationError, load_config


VALID_ENV = """\
JIRA_SITE=https://jira.example.test/
JIRA_READ_ONLY_USER=collector@example.test
JIRA_READ_ONLY_TOKEN=top-secret-token
GITHUB_REPOS=jooservices/ai-flow, jooservices/control-room
GITHUB_PROJECT_REPOS=JAIF=jooservices/ai-flow,JOOCR=jooservices/control-room
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
        self.assertEqual(
            config.github_project_repos,
            {
                "JAIF": "jooservices/ai-flow",
                "JOOCR": "jooservices/control-room",
            },
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

    def test_rejects_jira_site_without_hostname_or_valid_port(self):
        for site in ("https://:443", "https://jira.example.test:bad"):
            with self.subTest(site=site):
                env = VALID_ENV.replace("https://jira.example.test/", site)
                with self.assertRaises(ConfigurationError):
                    load_config(self.write_env(env))

    def test_rejects_malformed_github_repository_entries(self):
        for repos in (
            "owner/repo,not-a-repo",
            "owner/repo,",
            ",owner/repo",
            "owner/repo,,other/repo",
            "owner/,other/repo",
            "/repo",
            "owner/repo/extra",
            "owner /repo",
        ):
            with self.subTest(repos=repos):
                env = VALID_ENV.replace(
                    "jooservices/ai-flow, jooservices/control-room", repos
                )
                with self.assertRaises(ConfigurationError):
                    load_config(self.write_env(env))

    def test_rejects_malformed_project_repository_mappings(self):
        for mappings in (
            "not-a-mapping",
            "jaif=jooservices/ai-flow",
            "JAIF=jooservices/ai-flow,JAIF=jooservices/other",
            "JAIF=jooservices/ai-flow,",
            "JAIF=jooservices/ai-flow, JOOCR=owner/repo/extra",
        ):
            with self.subTest(mappings=mappings):
                env = VALID_ENV.replace(
                    "JAIF=jooservices/ai-flow,JOOCR=jooservices/control-room",
                    mappings,
                )
                with self.assertRaises(ConfigurationError):
                    load_config(self.write_env(env))

    def test_project_repository_mapping_is_optional_for_existing_config(self):
        config = load_config(
            self.write_env(
                VALID_ENV.replace(
                    "GITHUB_PROJECT_REPOS=JAIF=jooservices/ai-flow,JOOCR=jooservices/control-room\n",
                    "",
                )
            )
        )

        self.assertEqual(config.github_project_repos, {})

    def test_env_example_loads_all_required_settings(self):
        example_path = Path(__file__).parents[1] / ".env.example"

        config = load_config(example_path)

        self.assertEqual(config.interval_seconds, 45)
        self.assertEqual(
            config.github_repos,
            ("jooservices/ai-flow", "jooservices/control-room"),
        )
        self.assertEqual(
            config.github_project_repos,
            {
                "JAIF": "jooservices/ai-flow",
                "JOOCR": "jooservices/control-room",
            },
        )


if __name__ == "__main__":
    unittest.main()
