#!/usr/bin/env python3
"""Create the disposable Doris orders fixture used by the filter task."""

from __future__ import annotations

import os
import re
import time
from decimal import Decimal

import mysql.connector


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEMO_SOURCE_DATABASE = "DBT_BENCH_ORDERS_FILTER_DEMO"
DEMO_TARGET_DATABASE = "dbt_bench_production_sales_demo"
SOURCE_ROWS = (
    ("ORD-001", "C-001", "2026-01-01 09:15:00", Decimal("100.1040"), "COMPLETED", False, False, False),
    ("ORD-002", "C-001", "2026-01-01 11:30:00", Decimal("20.2040"), "SHIPPED", False, False, False),
    ("ORD-003", "C-002", "2026-01-02 12:45:00", Decimal("99.0099"), "COMPLETED", True, False, False),
    ("ORD-004", "C-003", "2026-01-02 08:00:00", Decimal("5.5550"), "DELIVERED", False, True, False),
    ("ORD-005", "C-004", "2026-01-03 13:20:00", Decimal("10.0099"), "COMPLETED", False, False, True),
    ("ORD-006", "C-005", "2026-01-03 07:10:00", Decimal("7.0790"), "PROCESSING", None, None, None),
    ("ORD-007", "C-006", "2026-01-04 18:05:00", Decimal("12.3440"), "RETURNED", None, False, False),
)


def identifier_from_env(name: str, default: str) -> str:
    value = os.environ.get(name, default)
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a simple SQL identifier, got {value!r}")
    return value


def wait_for_doris() -> mysql.connector.MySQLConnection:
    host = os.environ.get("DORIS_HOST", "doris")
    port = int(os.environ.get("DORIS_PORT", "9030"))
    user = os.environ.get("DORIS_USER", "root")
    password = os.environ.get("DORIS_PASSWORD", "")
    deadline = time.monotonic() + 240
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        connection = None
        try:
            connection = mysql.connector.connect(
                host=host,
                port=port,
                user=user,
                password=password,
                autocommit=True,
                connection_timeout=5,
            )
            cursor = connection.cursor(buffered=True)
            cursor.execute("SHOW BACKENDS")
            columns = [column[0].lower() for column in cursor.description]
            alive_index = columns.index("alive")
            capacity_index = columns.index("availcapacity")
            backends = cursor.fetchall()
            cursor.close()
            if any(
                str(row[alive_index]).lower() == "true"
                and re.search(r"[1-9]", str(row[capacity_index]))
                for row in backends
            ):
                return connection
            connection.close()
        except (mysql.connector.Error, ValueError) as error:
            last_error = error
            if connection is not None and connection.is_connected():
                connection.close()
        time.sleep(2)
    raise RuntimeError(f"Doris did not become ready: {last_error}")


def main() -> None:
    source_database = identifier_from_env("DORIS_SOURCE_DATABASE", DEMO_SOURCE_DATABASE)
    target_database = identifier_from_env("DORIS_TARGET_DATABASE", DEMO_TARGET_DATABASE)
    if source_database != DEMO_SOURCE_DATABASE or target_database != DEMO_TARGET_DATABASE:
        raise RuntimeError("Refusing to operate outside the fixed disposable demo databases")

    connection = wait_for_doris()
    cursor = connection.cursor(buffered=True)
    try:
        cursor.execute(
            "SELECT schema_name FROM information_schema.schemata WHERE schema_name IN (%s, %s)",
            (source_database, target_database),
        )
        existing = [row[0] for row in cursor.fetchall()]
        if existing:
            raise RuntimeError(f"Refusing to overwrite existing demo databases: {sorted(existing)}")
        cursor.execute(f"CREATE DATABASE {source_database}")
        cursor.execute(f"CREATE DATABASE {target_database}")
        cursor.execute(
            f"""
            CREATE TABLE {source_database}.ORDERS (
                ORDER_ID VARCHAR(32) NOT NULL,
                CUSTOMER_ID VARCHAR(32) NOT NULL,
                ORDERED_AT DATETIMEV2(0) NOT NULL,
                GRAND_TOTAL DECIMAL(18, 4) NOT NULL,
                STATUS VARCHAR(32) NOT NULL,
                TEST_ORDER_FLAG BOOLEAN,
                SAMPLE_ORDER_FLAG BOOLEAN,
                INTERNAL_ORDER_FLAG BOOLEAN
            )
            DUPLICATE KEY(ORDER_ID)
            DISTRIBUTED BY HASH(ORDER_ID) BUCKETS 1
            PROPERTIES ("replication_num" = "1")
            """
        )
        cursor.executemany(
            f"INSERT INTO {source_database}.ORDERS VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            SOURCE_ROWS,
        )
        cursor.execute(
            f"""
            SELECT COUNT(*), ROUND(SUM(GRAND_TOTAL), 2)
            FROM {source_database}.ORDERS
            WHERE COALESCE(TEST_ORDER_FLAG, FALSE) = FALSE
              AND COALESCE(SAMPLE_ORDER_FLAG, FALSE) = FALSE
              AND COALESCE(INTERNAL_ORDER_FLAG, FALSE) = FALSE
            """
        )
        count, revenue = cursor.fetchone()
        if count != 4 or Decimal(revenue) != Decimal("139.73"):
            raise AssertionError(f"Unexpected fixture aggregate: count={count}, revenue={revenue}")
    finally:
        cursor.close()
        connection.close()

    print(f"Doris orders fixture ready: {source_database}.ORDERS -> {target_database}")


if __name__ == "__main__":
    main()
