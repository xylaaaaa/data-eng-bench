#!/usr/bin/env python3
"""Regression tests for the SQL and project rewrites used by the Doris probe."""

import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml


MODULE_PATH = Path(__file__).with_name("dbt-wrapper.py")
SPEC = importlib.util.spec_from_file_location("doris_fast30_dbt_wrapper", MODULE_PATH)
WRAPPER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WRAPPER)


class SqlRewriteTest(unittest.TestCase):
    def test_rfm_concat_is_complete_and_idempotent(self):
        original = (
            "'RFM_' || CAST(r_score AS VARCHAR) || "
            "CAST(f_score AS VARCHAR) || CAST(m_score AS VARCHAR)"
        )
        expected = (
            "CONCAT('RFM_', CAST(r_score AS VARCHAR), "
            "CAST(f_score AS VARCHAR), CAST(m_score AS VARCHAR))"
        )
        rewritten = WRAPPER.rewrite_doris_sql(original)
        self.assertEqual(expected, rewritten)
        self.assertNotIn("||", rewritten)
        self.assertEqual(rewritten, WRAPPER.rewrite_doris_sql(rewritten))


class ProjectConfigTest(unittest.TestCase):
    def test_generated_hook_directory_is_not_parsed_as_yaml(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "dbt_project.yml"
            project.write_text("name: test_project\n")
            generated = (
                root
                / "target"
                / "compiled"
                / "test_project"
                / "dbt_project.yml"
                / "hooks"
            )
            generated.mkdir(parents=True)

            previous = Path.cwd()
            try:
                os.chdir(root)
                with mock.patch.dict(os.environ, {"DBT_PROFILES_DIR": temporary}):
                    WRAPPER.patch_project_configs()
            finally:
                os.chdir(previous)

            document = yaml.safe_load(project.read_text())
            self.assertEqual(1, document["models"]["+replication_num"])

    def test_profile_thread_override_is_applied(self):
        with tempfile.TemporaryDirectory() as temporary:
            profile = Path(temporary) / "profiles.yml"
            profile.write_text(
                "demo:\n  target: dev\n  outputs:\n"
                "    dev:\n      type: duckdb\n      path: /tmp/demo.duckdb\n"
            )
            with mock.patch.dict(
                os.environ,
                {
                    "DBT_PROFILES_DIR": temporary,
                    "DORIS_HOST": "doris",
                    "DORIS_PORT": "9030",
                    "DORIS_TARGET_DATABASE": "main",
                    "DBT_THREADS": "1",
                },
                clear=False,
            ):
                WRAPPER.rewrite_profiles([])
            document = yaml.safe_load(profile.read_text())
            self.assertEqual(1, document["demo"]["outputs"]["dev"]["threads"])


if __name__ == "__main__":
    unittest.main()
