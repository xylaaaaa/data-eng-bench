"""Deterministic verifier for the Apache Doris production-sales filter task."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import mysql.connector
import pytest


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROJECT_DIR = Path("/app/dbt_project")
TARGET_DIR = Path("/tmp/dbt-doris-filter-verifier-target")
MODEL_NAME = "production_sales"
EXPECTED_TESTS = {
    ("not_null", "order_id"),
    ("unique", "order_id"),
    ("not_null", "customer_id"),
    ("not_null", "order_date"),
    ("not_null", "grand_total"),
    ("not_null", "status"),
}
EXPECTED_SOURCE = (
    ("ORD-001", "C-001", "2026-01-01 09:15:00", Decimal("100.1040"), "COMPLETED", False, False, False),
    ("ORD-002", "C-001", "2026-01-01 11:30:00", Decimal("20.2040"), "SHIPPED", False, False, False),
    ("ORD-003", "C-002", "2026-01-02 12:45:00", Decimal("99.0099"), "COMPLETED", True, False, False),
    ("ORD-004", "C-003", "2026-01-02 08:00:00", Decimal("5.5550"), "DELIVERED", False, True, False),
    ("ORD-005", "C-004", "2026-01-03 13:20:00", Decimal("10.0099"), "COMPLETED", False, False, True),
    ("ORD-006", "C-005", "2026-01-03 07:10:00", Decimal("7.0790"), "PROCESSING", None, None, None),
    ("ORD-007", "C-006", "2026-01-04 18:05:00", Decimal("12.3440"), "RETURNED", None, False, False),
)
EXPECTED_OUTPUT = (
    ("ORD-001", "C-001", "2026-01-01", Decimal("100.10"), "COMPLETED"),
    ("ORD-002", "C-001", "2026-01-01", Decimal("20.20"), "SHIPPED"),
    ("ORD-006", "C-005", "2026-01-03", Decimal("7.08"), "PROCESSING"),
    ("ORD-007", "C-006", "2026-01-04", Decimal("12.34"), "RETURNED"),
)


def identifier(name: str) -> str:
    value = os.environ[name]
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} is not a simple SQL identifier: {value!r}")
    return value


SOURCE_DATABASE = identifier("DORIS_SOURCE_DATABASE")
TARGET_DATABASE = identifier("DORIS_TARGET_DATABASE")
SOURCE_RELATION = f"{SOURCE_DATABASE}.ORDERS"
TARGET_RELATION = f"{TARGET_DATABASE}.{MODEL_NAME}"


def connection():
    assert os.environ.get("DB_TYPE", "").lower() == "doris"
    return mysql.connector.connect(
        host=os.environ["DORIS_HOST"],
        port=int(os.environ["DORIS_PORT"]),
        user=os.environ["DORIS_USER"],
        password=os.environ.get("DORIS_PASSWORD", ""),
        autocommit=True,
        buffered=True,
    )


def query(conn, statement: str, params=None):
    cursor = conn.cursor(buffered=True)
    try:
        cursor.execute(statement, params)
        return cursor.fetchall()
    finally:
        cursor.close()


def scalar(conn, statement: str, params=None):
    rows = query(conn, statement, params)
    return rows[0][0] if rows else None


def run_dbt(*args: str):
    environment = os.environ.copy()
    environment["DBT_TARGET_PATH"] = str(TARGET_DIR)
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


def artifact(name: str):
    with (TARGET_DIR / name).open(encoding="utf-8") as file:
        return json.load(file)


def inspect_manifest():
    manifest = artifact("manifest.json")
    models = [
        node
        for node in manifest["nodes"].values()
        if node.get("resource_type") == "model" and node.get("name") == MODEL_NAME
    ]
    assert len(models) == 1, f"Expected one {MODEL_NAME} model"
    model = models[0]
    config = model["config"]
    assert config.get("enabled") is True
    assert config.get("materialized") == "table"
    assert config.get("duplicate_key") == ["order_id"]
    assert config.get("distributed_by") == ["order_id"]
    assert config.get("buckets") == 1
    assert config.get("replication_num") == 1

    sources = [
        node
        for node in manifest["sources"].values()
        if str(node.get("identifier", node.get("name", ""))).casefold() == "orders"
        and SOURCE_DATABASE in {node.get("database"), node.get("schema")}
    ]
    assert len(sources) == 1, "The model must depend on the physical ORDERS source"
    assert sources[0]["unique_id"] in model["depends_on"]["nodes"]

    tests = {}
    for node in manifest["nodes"].values():
        metadata = node.get("test_metadata")
        if node.get("resource_type") != "test" or not metadata:
            continue
        if model["unique_id"] not in node.get("depends_on", {}).get("nodes", []):
            continue
        key = (metadata["name"], metadata["kwargs"].get("column_name"))
        tests[key] = node["unique_id"]
    missing = EXPECTED_TESTS - tests.keys()
    assert not missing, f"Missing required dbt tests: {sorted(missing)}"
    return model["unique_id"], set(tests.values())


def assert_statuses(expected_ids, expected_status: str):
    statuses = {
        item["unique_id"]: item["status"]
        for item in artifact("run_results.json")["results"]
    }
    assert not set(expected_ids) - statuses.keys()
    assert all(statuses[item] == expected_status for item in expected_ids), statuses


def drop_target():
    conn = connection()
    try:
        rows = query(
            conn,
            "SELECT table_type FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name = %s",
            (TARGET_DATABASE, MODEL_NAME),
        )
        if rows:
            relation_type = "VIEW" if rows[0][0].upper() == "VIEW" else "TABLE"
            cursor = conn.cursor()
            try:
                cursor.execute(f"DROP {relation_type} {TARGET_RELATION}")
            finally:
                cursor.close()
    finally:
        conn.close()


def run_pipeline(reset: bool = True):
    if reset:
        drop_target()
        if TARGET_DIR.is_symlink() or TARGET_DIR.is_file():
            TARGET_DIR.unlink()
        elif TARGET_DIR.exists():
            shutil.rmtree(TARGET_DIR)
    run_dbt("run", "--select", MODEL_NAME)
    model_id, test_ids = inspect_manifest()
    assert_statuses({model_id}, "success")
    run_dbt("test", "--select", MODEL_NAME)
    assert_statuses(test_ids, "pass")


def normalize_source(row):
    ordered_at = row[2]
    if isinstance(ordered_at, datetime):
        ordered_at = ordered_at.strftime("%Y-%m-%d %H:%M:%S")
    return (
        str(row[0]),
        str(row[1]),
        ordered_at,
        Decimal(str(row[3])).quantize(Decimal("0.0001")),
        str(row[4]),
        None if row[5] is None else bool(row[5]),
        None if row[6] is None else bool(row[6]),
        None if row[7] is None else bool(row[7]),
    )


def normalize_output(row):
    value = row[2]
    if isinstance(value, (date, datetime)):
        value = value.isoformat()
    return (
        str(row[0]),
        str(row[1]),
        str(value),
        Decimal(str(row[3])).quantize(Decimal("0.01")),
        str(row[4]),
    )


def source_rows():
    conn = connection()
    try:
        return tuple(
            normalize_source(row)
            for row in query(
                conn,
                f"SELECT ORDER_ID, CUSTOMER_ID, ORDERED_AT, GRAND_TOTAL, STATUS, "
                f"TEST_ORDER_FLAG, SAMPLE_ORDER_FLAG, INTERNAL_ORDER_FLAG "
                f"FROM {SOURCE_RELATION} ORDER BY ORDER_ID",
            )
        )
    finally:
        conn.close()


def output_rows():
    conn = connection()
    try:
        return tuple(
            normalize_output(row)
            for row in query(
                conn,
                f"SELECT order_id, customer_id, order_date, grand_total, status "
                f"FROM {TARGET_RELATION} ORDER BY order_id",
            )
        )
    finally:
        conn.close()


@pytest.fixture(scope="module")
def dbt_run():
    run_pipeline()
    return True


class TestStructure:
    def test_model_exists_as_table(self, dbt_run):
        conn = connection()
        try:
            rows = query(
                conn,
                "SELECT table_type FROM information_schema.tables "
                "WHERE table_schema = %s AND table_name = %s",
                (TARGET_DATABASE, MODEL_NAME),
            )
            assert rows == [("BASE TABLE",)]
        finally:
            conn.close()

    def test_columns_and_types(self, dbt_run):
        conn = connection()
        try:
            rows = query(
                conn,
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = %s",
                (TARGET_DATABASE, MODEL_NAME),
            )
            actual = {str(name).lower(): str(data_type).lower() for name, data_type in rows}
            expected = {
                "order_id": "varchar",
                "customer_id": "varchar",
                "order_date": "date",
                "grand_total": "decimal",
                "status": "varchar",
            }
            assert actual == expected, actual
        finally:
            conn.close()

    def test_physical_layout(self, dbt_run):
        conn = connection()
        try:
            ddl = " ".join(str(row[-1]) for row in query(conn, f"SHOW CREATE TABLE {TARGET_RELATION}"))
            assert re.search(r"DUPLICATE KEY.*order_id", ddl, re.I)
            assert re.search(r"DISTRIBUTED BY HASH.*order_id.*BUCKETS 1", ddl, re.I)
            assert re.search(r'"replication_(?:num|allocation)"\s*=\s*"(?:1|[^"]*: *1)"', ddl, re.I)
        finally:
            conn.close()

    def test_source_fixture_is_unchanged(self, dbt_run):
        assert source_rows() == EXPECTED_SOURCE


class TestFiltering:
    def test_exact_filtered_rows(self, dbt_run):
        assert output_rows() == EXPECTED_OUTPUT

    def test_all_flags_are_false_or_null(self, dbt_run):
        conn = connection()
        try:
            assert scalar(
                conn,
                f"SELECT COUNT(*) FROM {TARGET_RELATION} p JOIN {SOURCE_RELATION} o "
                "ON p.order_id = o.ORDER_ID "
                "WHERE o.TEST_ORDER_FLAG = TRUE OR o.SAMPLE_ORDER_FLAG = TRUE "
                "OR o.INTERNAL_ORDER_FLAG = TRUE",
            ) == 0
        finally:
            conn.close()

    def test_source_was_reduced(self, dbt_run):
        conn = connection()
        try:
            source_count = scalar(conn, f"SELECT COUNT(*) FROM {SOURCE_RELATION}")
            output_count = scalar(conn, f"SELECT COUNT(*) FROM {TARGET_RELATION}")
            assert output_count < source_count
            assert output_count == 4
        finally:
            conn.close()

    def test_revenue_is_rounded(self, dbt_run):
        conn = connection()
        try:
            rows = query(conn, f"SELECT grand_total FROM {TARGET_RELATION}")
            assert all(
                Decimal(str(row[0])) == Decimal(str(row[0])).quantize(Decimal("0.01"))
                for row in rows
            )
        finally:
            conn.close()


class TestIdempotency:
    def test_second_dbt_run_is_stable(self, dbt_run):
        before = output_rows()
        run_pipeline(reset=False)
        assert output_rows() == before
