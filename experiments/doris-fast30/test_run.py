#!/usr/bin/env python3
"""Unit tests for the host-side fast-30 orchestration logic."""

import importlib.util
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("run.py")
SPEC = importlib.util.spec_from_file_location("doris_fast30_run", str(MODULE_PATH))
RUN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RUN)


def valid_entry():
    return {
        "task": "fifo-inventory-cogs",
        "project_mode": "minimal",
        "project_path": "experiments/doris-fast30/minimal-fifo",
        "container_project_dir": "/app/dbt_models_duckdb",
        "db_type": "duckdb",
        "env": {"DBT_PROJECT_DIR_DUCKDB": "/app/dbt_models_duckdb"},
        "fixtures": {
            "duplicate": ["INVENTORY.INVENTORY_TRANSACTIONS"],
            "unique": [],
        },
        "target_databases": ["main", "main_inventory_analytics"],
        "oracle": "fifo_doris",
    }


class ManifestValidationTest(unittest.TestCase):
    def test_repository_manifest_matches_fast_30(self):
        manifest = RUN.load_manifest(RUN.DEFAULT_MANIFEST)
        self.assertEqual(
            RUN.canonical_task_names(), tuple(task.task for task in manifest.tasks)
        )
        self.assertEqual(30, len(manifest.tasks))
        self.assertEqual(491008000, manifest.fixture_size)

    def test_rejects_fixture_command_injection(self):
        entry = valid_entry()
        entry["fixtures"]["duplicate"] = ["main.orders;DROP_DATABASE_main"]
        with self.assertRaisesRegex(RUN.ManifestError, "invalid SCHEMA.TABLE"):
            RUN.parse_task(entry, 0)

    def test_rejects_system_target_database(self):
        entry = valid_entry()
        entry["target_databases"] = ["information_schema"]
        with self.assertRaisesRegex(RUN.ManifestError, "system database"):
            RUN.parse_task(entry, 0)

    def test_rejects_missing_main_verifier_database(self):
        entry = valid_entry()
        entry["target_databases"] = ["main_inventory_analytics"]
        with self.assertRaisesRegex(RUN.ManifestError, "must include main"):
            RUN.parse_task(entry, 0)

    def test_rejects_path_outside_repository(self):
        with self.assertRaisesRegex(RUN.ManifestError, "escapes the repository"):
            RUN.resolve_repo_path("../outside")

    def test_case_sensitive_fixture_names_match_canonical_models(self):
        manifest = RUN.load_manifest(RUN.DEFAULT_MANIFEST)
        tasks = {task.task: task for task in manifest.tasks}
        self.assertIn(
            "main.orders",
            tasks["dbt-fraud-detection-model"].duplicate_fixtures,
        )
        self.assertEqual(
            ("main.customers", "main.orders"),
            tasks["dbt-rfm-customer-segmentation"].duplicate_fixtures,
        )
        self.assertEqual(
            ("main.orders",),
            tasks["dbt-test-orders-filter"].duplicate_fixtures,
        )
        workforce = tasks["workforce-analytics"]
        self.assertTrue(
            all(relation.startswith("hr.") for relation in workforce.duplicate_fixtures)
        )
        self.assertIn("hr", workforce.target_databases)

    def test_receivables_uses_live_unique_key_sources(self):
        manifest = RUN.load_manifest(RUN.DEFAULT_MANIFEST)
        tasks = {task.task: task for task in manifest.tasks}
        receivables = tasks["dbt-receivables-aging-buckets"]
        self.assertEqual((), receivables.duplicate_fixtures)
        self.assertEqual(
            (
                "FINANCE.CUSTOMER_CREDITS",
                "FINANCE.CUSTOMER_INVOICES",
                "FINANCE.CUSTOMER_PAYMENT_APPLICATIONS",
                "FINANCE.CUSTOMER_PAYMENTS",
            ),
            receivables.unique_fixtures,
        )
        project_path = RUN.resolve_repo_path(receivables.project_path)
        for model in sorted((project_path / "models").glob("*.sql")):
            self.assertIn("materialized='view'", model.read_text())
        project = (project_path / "dbt_project.yml").read_text()
        for relation in receivables.unique_fixtures:
            schema, table = relation.split(".")
            self.assertIn(
                f"SELECT * FROM `{schema}`.`{table}`",
                project,
            )

    def test_shared_input_digest_covers_runtime_compatibility_files(self):
        expected = {
            "Dockerfile",
            "dbt-wrapper.py",
            "duckdb.py",
            "load_duckdb.py",
            "run.py",
            "tasks.yaml",
        }
        self.assertTrue(expected.issubset({path.name for path in RUN.SHARED_INPUT_PATHS}))
        self.assertRegex(RUN.shared_input_digest(), r"^[0-9a-f]{64}$")

    def test_task_environment_allows_only_declared_dbt_thread_override(self):
        manifest = RUN.load_manifest(RUN.DEFAULT_MANIFEST)
        tasks = {task.task: task for task in manifest.tasks}
        self.assertEqual("1", tasks["dbt-fraud-detection-model"].environment["DBT_THREADS"])


class EvidenceTest(unittest.TestCase):
    def test_verifier_result_uses_last_structured_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            log = Path(temporary) / "verify.log"
            log.write_text(
                "Test results: 0 passed, 0 skipped, 1 failed\n"
                "more output\n"
                "Test results: 13 passed, 0 skipped, 0 failed\n"
            )
            self.assertEqual(
                {"passed": 13, "skipped": 0, "failed": 0, "total": 13},
                RUN.parse_verifier_result(log),
            )

    def test_pass_requires_host_reward_counts_and_no_copy_errors(self):
        counts = {"passed": 1, "skipped": 0, "failed": 0, "total": 1}
        self.assertTrue(RUN.evidence_is_complete("1", counts, ()))
        self.assertFalse(RUN.evidence_is_complete("0", counts, ()))
        self.assertFalse(RUN.evidence_is_complete("1", None, ()))
        self.assertFalse(RUN.evidence_is_complete("1", counts, ("copy failed",)))

    def test_verifier_aggregate_is_read_from_task_runtime_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            for name, verifier in (
                ("first", {"passed": 2, "skipped": 0, "failed": 0, "total": 2}),
                ("second", {"passed": 3, "skipped": 1, "failed": 0, "total": 4}),
            ):
                task = output / name
                task.mkdir()
                (task / "runtime.json").write_text(
                    __import__("json").dumps({"verifier": verifier})
                )
            self.assertEqual(
                {
                    "tasks_with_counts": 2,
                    "passed": 5,
                    "skipped": 1,
                    "failed": 0,
                    "total": 6,
                },
                RUN.aggregate_verifier_results(
                    output, {"first": True, "second": True}
                ),
            )


class StagingTest(unittest.TestCase):
    def test_fifo_staging_uses_doris_oracle_without_mutating_task(self):
        task = RUN.parse_task(valid_entry(), 0)
        canonical = task.task_dir / "tests" / "private_data" / "expected_results.txt"
        before = canonical.read_bytes()
        with tempfile.TemporaryDirectory() as temporary:
            _, tests, project = RUN.prepare_staging(task, Path(temporary))
            expected = (
                RUN.SCRIPT_DIR
                / "oracles"
                / task.task
                / "expected_results_doris.txt"
            ).read_bytes()
            self.assertEqual(
                expected,
                (tests / "private_data" / "expected_results.txt").read_bytes(),
            )
            self.assertTrue((project / "dbt_project.yml").is_file())
        self.assertEqual(before, canonical.read_bytes())

    def test_resource_names_are_unique_and_task_scoped(self):
        first = RUN.make_resource_names("dbt-daily-order-summary")
        second = RUN.make_resource_names("dbt-daily-order-summary")
        self.assertNotEqual(first.network, second.network)
        self.assertTrue(first.network.startswith(RUN.RESOURCE_PREFIX))
        self.assertTrue(first.doris.endswith("-doris"))
        self.assertTrue(first.runner.endswith("-runner"))

    def test_doris_state_is_kept_on_bounded_tmpfs_mounts(self):
        self.assertEqual(
            {
                "/opt/apache-doris/be/storage": "rw,size=4g,mode=0755",
                "/opt/apache-doris/fe/doris-meta": "rw,size=2g,mode=0755",
            },
            dict(RUN.DORIS_TMPFS_MOUNTS),
        )

    def test_only_observed_doris_startup_errors_are_retryable(self):
        self.assertEqual(
            (
                "No available backends for compute group",
                "Failed to find enough backend",
            ),
            RUN.DORIS_TRANSIENT_STARTUP_ERRORS,
        )


if __name__ == "__main__":
    unittest.main()
