import tempfile
import unittest
from pathlib import Path

from deploy.render_units import render_unit


REPOSITORY_ROOT = Path(__file__).parents[1]
DEPLOY_DIRECTORY = REPOSITORY_ROOT / "deploy"


class SystemdUnitTemplateTest(unittest.TestCase):
    def test_templates_render_checkout_and_required_service_fields(self):
        with tempfile.TemporaryDirectory(prefix="control room ") as temporary_directory:
            checkout = Path(temporary_directory)

            for unit_name, module in (
                ("controlroom-collector.service", "controlroom.collector"),
                ("controlroom-server.service", "controlroom.server"),
            ):
                with self.subTest(unit=unit_name):
                    rendered = render_unit(DEPLOY_DIRECTORY / unit_name, checkout)

                    self.assertIn(f'WorkingDirectory="{checkout.resolve()}"', rendered)
                    self.assertIn(f"ExecStart=/usr/bin/python3 -m {module}", rendered)
                    self.assertIn("Restart=always", rendered)
                    self.assertIn("RestartSec=15", rendered)
                    self.assertIn("WantedBy=default.target", rendered)
                    self.assertNotIn("@REPO_ROOT@", rendered)

    def test_renderer_rejects_template_without_single_path_marker(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            template = Path(temporary_directory) / "invalid.service"
            template.write_text("[Service]\nRestart=always\n", encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "exactly one"):
                render_unit(template, temporary_directory)


if __name__ == "__main__":
    unittest.main()
