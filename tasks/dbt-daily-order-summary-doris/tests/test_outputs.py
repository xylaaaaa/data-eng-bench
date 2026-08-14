"""Deterministic verifier for the Apache Doris daily-order demo."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import mysql.connector
import pytest


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROJECT_DIR = Path("/app/dbt_project")
VERIFIER_TARGET_DIR = Path("/tmp/dbt-doris-verifier-target")
MODEL_NAME = "daily_order_summary"
EXPECTED_GENERIC_TESTS = {
    ("not_null", "order_date"),
    ("unique", "order_date"),
    ("not_null", "order_count"),
    ("not_null", "total_revenue"),
}
EXPECTED_SOURCE_ROWS = (
    (1, "2026-01-01 09:15:00", Decimal("100.1050"), "COMPLETED"),
    (2, "2026-01-01 11:30:00", Decimal("20.2040"), "SHIPPED"),
    (3, "2026-01-01 12:45:00", Decimal("99.0099"), "CANCELLED"),
    (4, "2026-01-02 08:00:00", Decimal("5.5550"), "COMPLETED"),
    (5, "2026-01-02 13:20:00", Decimal("10.0099"), "RETURNED"),
    (6, "2026-01-03 07:10:00", Decimal("7.0799"), "FAILED"),
    (7, "2026-01-03 18:05:00", Decimal("12.3440"), "PROCESSING"),
)
EXPECTED_DAILY_ROWS = (
    ("2026-01-01", 2, Decimal("120.31")),
    ("2026-01-02", 1, Decimal("5.56")),
    ("2026-01-03", 1, Decimal("12.34")),
)


def identifier_from_env(name: str) -> str:
    value = os.environ[name]
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a simple SQL identifier, got {value!r}")
    return value


SOURCE_DATABASE = identifier_from_env("DORIS_SOURCE_DATABASE")
TARGET_DATABASE = identifier_from_env("DORIS_TARGET_DATABASE")
SOURCE_RELATION = f"`{SOURCE_DATABASE}`.`ORDERS`"
TARGET_RELATION = f"`{TARGET_DATABASE}`.`daily_order_summary`"


def get_db_connection():
    assert os.environ.get("DB_TYPE", "").lower() == "doris"
    return mysql.connector.connect(
        host=os.environ["DORIS_HOST"],
        port=int(os.environ["DORIS_PORT"]),
        user=os.environ["DORIS_USER"],
        password=os.environ.get("DORIS_PASSWORD", ""),
        autocommit=True,
        buffered=True,
    )


def execute_query(connection, query: str, params=None):
    cursor = connection.cursor(buffered=True)
    try:
        if params is None:
            cursor.execute(query)
        else:
            cursor.execute(query, params)
        return cursor.fetchall()
    finally:
        cursor.close()


def execute_scalar(connection, query: str, params=None):
    rows = execute_query(connection, query, params)
    return rows[0][0] if rows else None


def execute_statement(connection, statement: str):
    cursor = connection.cursor(buffered=True)
    try:
        cursor.execute(statement)
    finally:
        cursor.close()


def run_dbt_command(*args: str):
    environment = os.environ.copy()
    environment["DBT_TARGET_PATH"] = str(VERIFIER_TARGET_DIR)
    result = subprocess.run(
        ["dbt", *args, "--profiles-dir", str(PROJECT_DIR)],
        cwd=PROJECT_DIR,
        env=environment,
        capture_output=True,
        text=True,
        timeout=300,
    )
    print(result.stdout[-4000:])
    if result.stderr:
        print(result.stderr[-2000:])
    assert result.returncode == 0, f"dbt {' '.join(args)} failed"


def load_artifact(name: str):
    with (VERIFIER_TARGET_DIR / name).open(encoding="utf-8") as artifact:
        return json.load(artifact)


def find_model_and_tests(manifest):
    models = [
        node
        for node in manifest["nodes"].values()
        if node.get("resource_type") == "model" and node.get("name") == MODEL_NAME
    ]
    assert len(models) == 1, f"Expected one enabled {MODEL_NAME} dbt model"
    model = models[0]
    model_id = model["unique_id"]
    config = model["config"]
    assert config.get("enabled") is True
    assert config.get("materialized") == "table"
    assert config.get("duplicate_key") == ["order_date"]
    assert config.get("distributed_by") == ["order_date"]
    assert config.get("buckets") == 1
    assert config.get("replication_num") == 1

    sources = [
        source
        for source in manifest["sources"].values()
        if source.get("source_name") == "orders" and source.get("name") == "orders"
    ]
    assert len(sources) == 1, "Expected the orders.orders dbt source"
    assert sources[0]["unique_id"] in model["depends_on"]["nodes"]

    generic_tests = {}
    for node in manifest["nodes"].values():
        metadata = node.get("test_metadata")
        if node.get("resource_type") != "test" or not metadata:
            continue
        if model_id not in node.get("depends_on", {}).get("nodes", []):
            continue
        key = (metadata["name"], metadata["kwargs"].get("column_name"))
        generic_tests[key] = node["unique_id"]

    missing = EXPECTED_GENERIC_TESTS - generic_tests.keys()
    assert not missing, f"Missing required dbt data tests: {sorted(missing)}"
    return model_id, {generic_tests[key] for key in EXPECTED_GENERIC_TESTS}


def assert_run_succeeded(unique_ids, expected_status: str):
    results = {
        result["unique_id"]: result["status"]
        for result in load_artifact("run_results.json")["results"]
    }
    missing = set(unique_ids) - results.keys()
    assert not missing, f"dbt did not execute expected nodes: {sorted(missing)}"
    failures = {
        unique_id: results[unique_id]
        for unique_id in unique_ids
        if results[unique_id] != expected_status
    }
    assert not failures, f"Unexpected dbt node statuses: {failures}"


def drop_target_relation():
    connection = get_db_connection()
    try:
        rows = execute_query(
            connection,
            """
            SELECT table_type
            FROM information_schema.tables
            WHERE table_schema = %s AND table_name = %s
            """,
            (TARGET_DATABASE, MODEL_NAME),
        )
        if rows:
            relation_type = "VIEW" if rows[0][0].upper() == "VIEW" else "TABLE"
            execute_statement(connection, f"DROP {relation_type} {TARGET_RELATION}")
    finally:
        connection.close()


def run_and_validate_dbt(*, reset_relation: bool, run_tests: bool):
    if reset_relation:
        drop_target_relation()
        if VERIFIER_TARGET_DIR.is_symlink() or VERIFIER_TARGET_DIR.is_file():
            VERIFIER_TARGET_DIR.unlink()
        elif VERIFIER_TARGET_DIR.exists():
            shutil.rmtree(VERIFIER_TARGET_DIR)
        assert not VERIFIER_TARGET_DIR.exists()

    run_dbt_command("run", "--select", MODEL_NAME)
    manifest = load_artifact("manifest.json")
    model_id, test_ids = find_model_and_tests(manifest)
    assert_run_succeeded({model_id}, "success")

    if run_tests:
        run_dbt_command("test", "--select", MODEL_NAME)
        assert_run_succeeded(test_ids, "pass")


def query_daily_summary():
    connection = get_db_connection()
    try:
        return execute_query(
            connection,
            f"""
            SELECT order_date, order_count, total_revenue
            FROM {TARGET_RELATION}
            ORDER BY order_date
            """,
        )
    finally:
        connection.close()


def get_expected_daily_rows():
    connection = get_db_connection()
    try:
        return execute_query(
            connection,
            f"""
            SELECT
                CAST(ORDERED_AT AS DATE) AS order_date,
                COUNT(*) AS order_count,
                ROUND(SUM(GRAND_TOTAL), 2) AS total_revenue
            FROM {SOURCE_RELATION}
            WHERE STATUS NOT IN ('CANCELLED', 'RETURNED', 'FAILED')
            GROUP BY CAST(ORDERED_AT AS DATE)
            ORDER BY order_date
            """,
        )
    finally:
        connection.close()


def get_source_rows():
    connection = get_db_connection()
    try:
        return execute_query(
            connection,
            f"""
            SELECT ORDER_ID, ORDERED_AT, GRAND_TOTAL, STATUS
            FROM {SOURCE_RELATION}
            ORDER BY ORDER_ID
            """,
        )
    finally:
        connection.close()


def get_all_orders_count():
    connection = get_db_connection()
    try:
        return execute_scalar(connection, f"SELECT COUNT(*) FROM {SOURCE_RELATION}")
    finally:
        connection.close()


def normalize_row(row):
    return (
        str(row[0]),
        int(row[1]),
        Decimal(str(row[2])).quantize(Decimal("0.01")),
    )


def normalize_source_row(row):
    ordered_at = row[1]
    if isinstance(ordered_at, datetime):
        ordered_at = ordered_at.strftime("%Y-%m-%d %H:%M:%S")
    return (
        int(row[0]),
        str(ordered_at),
        Decimal(str(row[2])).quantize(Decimal("0.0001")),
        str(row[3]),
    )


@pytest.fixture(scope="module")
def dbt_run():
    run_and_validate_dbt(reset_relation=True, run_tests=True)
    return True


@pytest.fixture(scope="module")
def summary_rows(dbt_run):
    return query_daily_summary()


@pytest.fixture(scope="module")
def expected_rows():
    actual_source = tuple(normalize_source_row(row) for row in get_source_rows())
    assert actual_source == EXPECTED_SOURCE_ROWS, "The source fixture was modified"

    calculated = tuple(normalize_row(row) for row in get_expected_daily_rows())
    assert calculated == EXPECTED_DAILY_ROWS, "Unexpected source aggregation"
    return EXPECTED_DAILY_ROWS


class TestStructure:
    def test_model_exists_as_table(self, dbt_run):
        connection = get_db_connection()
        try:
            rows = execute_query(
                connection,
                """
                SELECT table_type
                FROM information_schema.tables
                WHERE table_schema = %s AND table_name = %s
                """,
                (TARGET_DATABASE, "daily_order_summary"),
            )
            assert len(rows) == 1, "daily_order_summary was not materialized"
            assert rows[0][0].upper() == "BASE TABLE", "Model must be a table"
        finally:
            connection.close()

    def test_columns_and_types(self, dbt_run):
        connection = get_db_connection()
        try:
            rows = execute_query(
                connection,
                """
                SELECT column_name, data_type
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                """,
                (TARGET_DATABASE, "daily_order_summary"),
            )
            actual = {row[0].lower(): row[1].lower() for row in rows}
            expected = {
                "order_date": "date",
                "order_count": "bigint",
                "total_revenue": "decimal",
            }
            assert actual == expected, f"Unexpected columns: {actual}"
        finally:
            connection.close()

    def test_doris_physical_layout(self, dbt_run):
        connection = get_db_connection()
        try:
            rows = execute_query(connection, f"SHOW CREATE TABLE {TARGET_RELATION}")
            assert len(rows) == 1
            ddl = re.sub(r"\s+", " ", rows[0][-1])
            assert re.search(
                r"DUPLICATE KEY\s*\(\s*`order_date`\s*\)", ddl, re.IGNORECASE
            )
            assert re.search(
                r"DISTRIBUTED BY HASH\s*\(\s*`order_date`\s*\)\s*BUCKETS\s+1",
                ddl,
                re.IGNORECASE,
            )
            assert re.search(
                r'"replication_(?:num|allocation)"\s*=\s*"(?:1|[^";]*:\s*1)"',
                ddl,
                re.IGNORECASE,
            )
        finally:
            connection.close()

    def test_has_data(self, summary_rows):
        assert summary_rows, "daily_order_summary has no rows"

    def test_no_nulls(self, summary_rows):
        assert all(value is not None for row in summary_rows for value in row)


class TestDataQuality:
    def test_unique_dates(self, summary_rows):
        dates = [row[0] for row in summary_rows]
        assert len(dates) == len(set(dates)), "Duplicate order dates found"

    def test_positive_order_counts(self, summary_rows):
        for order_date, order_count, _ in summary_rows:
            assert int(order_count) == order_count
            assert order_count > 0, f"Non-positive count for {order_date}"

    def test_non_negative_revenue(self, summary_rows):
        for order_date, _, revenue in summary_rows:
            assert Decimal(revenue) >= 0, f"Negative revenue for {order_date}"

    def test_revenue_rounded_to_2_decimals(self, summary_rows):
        cents = Decimal("0.01")
        for order_date, _, revenue in summary_rows:
            value = Decimal(revenue)
            assert value == value.quantize(cents), f"Revenue not rounded for {order_date}"


class TestCorrectness:
    def test_daily_rows_match_expected_fixture(self, summary_rows, expected_rows):
        assert [normalize_row(row) for row in summary_rows] == [
            normalize_row(row) for row in expected_rows
        ]

    def test_status_filter_applied(self, summary_rows):
        valid_orders = sum(int(row[1]) for row in summary_rows)
        assert get_all_orders_count() == len(EXPECTED_SOURCE_ROWS)
        assert valid_orders == 4, "Excluded statuses were retained"

    def test_total_revenue_matches_filtered_source(self, summary_rows, expected_rows):
        actual = sum(Decimal(str(row[2])) for row in summary_rows)
        expected = sum(Decimal(str(row[2])) for row in expected_rows)
        assert actual == expected


class TestIdempotency:
    def test_idempotency(self, summary_rows):
        before = [normalize_row(row) for row in summary_rows]
        run_and_validate_dbt(reset_relation=False, run_tests=False)
        after = [normalize_row(row) for row in query_daily_summary()]
        assert before == after
