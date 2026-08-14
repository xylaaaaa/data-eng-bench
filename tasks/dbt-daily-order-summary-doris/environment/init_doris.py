#!/usr/bin/env python3
"""Create the disposable Doris fixture used by the demo task."""

from __future__ import annotations

import os
import re
import time
from decimal import Decimal

import mysql.connector


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEMO_SOURCE_DATABASE = "DBT_BENCH_ORDERS_DEMO"
DEMO_TARGET_DATABASE = "dbt_bench_daily_analytics_demo"
SOURCE_ROWS = (
    (1, "2026-01-01 09:15:00", Decimal("100.1050"), "COMPLETED"),
    (2, "2026-01-01 11:30:00", Decimal("20.2040"), "SHIPPED"),
    (3, "2026-01-01 12:45:00", Decimal("99.0099"), "CANCELLED"),
    (4, "2026-01-02 08:00:00", Decimal("5.5550"), "COMPLETED"),
    (5, "2026-01-02 13:20:00", Decimal("10.0099"), "RETURNED"),
    (6, "2026-01-03 07:10:00", Decimal("7.0799"), "FAILED"),
    (7, "2026-01-03 18:05:00", Decimal("12.3440"), "PROCESSING"),
)


def identifier_from_env(name: str, default: str) -> str:
    value = os.environ.get(name, default)
    if not IDENTIFIER.fullmatch(value):
        raise ValueError(f"{name} must be a simple SQL identifier, got {value!r}")
    return value


def quote_identifier(value: str) -> str:
    # All callers pass values through identifier_from_env first.
    return f"`{value}`"


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
            backends = cursor.fetchall()
            cursor.close()
            if any(str(row[alive_index]).lower() == "true" for row in backends):
                return connection
            connection.close()
        except (mysql.connector.Error, ValueError) as error:
            last_error = error
            if connection is not None and connection.is_connected():
                connection.close()
        time.sleep(2)

    raise RuntimeError(f"Doris did not become ready: {last_error}")


def main() -> None:
    source_database = identifier_from_env(
        "DORIS_SOURCE_DATABASE", DEMO_SOURCE_DATABASE
    )
    target_database = identifier_from_env(
        "DORIS_TARGET_DATABASE", DEMO_TARGET_DATABASE
    )
    if source_database != DEMO_SOURCE_DATABASE:
        raise RuntimeError(
            f"Refusing to drop non-demo source database {source_database!r}"
        )
    if target_database != DEMO_TARGET_DATABASE:
        raise RuntimeError(
            f"Refusing to drop non-demo target database {target_database!r}"
        )

    source = quote_identifier(source_database)
    target = quote_identifier(target_database)
    connection = wait_for_doris()
    cursor = connection.cursor(buffered=True)
    try:
        cursor.execute(
            """
            SELECT schema_name
            FROM information_schema.schemata
            WHERE schema_name IN (%s, %s)
            """,
            (source_database, target_database),
        )
        existing_databases = [row[0] for row in cursor.fetchall()]
        if existing_databases:
            raise RuntimeError(
                "Refusing to overwrite existing demo databases: "
                f"{sorted(existing_databases)}"
            )
        cursor.execute(f"CREATE DATABASE {source}")
        cursor.execute(f"CREATE DATABASE {target}")
        cursor.execute(
            f"""
            CREATE TABLE {source}.`ORDERS` (
                `ORDER_ID` BIGINT NOT NULL,
                `ORDERED_AT` DATETIMEV2(0) NOT NULL,
                `GRAND_TOTAL` DECIMAL(18, 4) NOT NULL,
                `STATUS` VARCHAR(32) NOT NULL
            )
            DUPLICATE KEY(`ORDER_ID`)
            DISTRIBUTED BY HASH(`ORDER_ID`) BUCKETS 1
            PROPERTIES ("replication_num" = "1")
            """
        )
        cursor.executemany(
            f"INSERT INTO {source}.`ORDERS` VALUES (%s, %s, %s, %s)",
            SOURCE_ROWS,
        )
        cursor.execute(
            f"""
            SELECT COUNT(*), ROUND(SUM(`GRAND_TOTAL`), 2)
            FROM {source}.`ORDERS`
            WHERE `STATUS` NOT IN ('CANCELLED', 'RETURNED', 'FAILED')
            """
        )
        count, revenue = cursor.fetchone()
        if count != 4 or Decimal(revenue) != Decimal("138.21"):
            raise AssertionError(
                f"Unexpected fixture aggregate: count={count}, revenue={revenue}"
            )
    finally:
        cursor.close()
        connection.close()

    print(
        "Doris demo fixture ready: "
        f"{source_database}.ORDERS -> {target_database} "
        f"({len(SOURCE_ROWS)} source rows)"
    )


if __name__ == "__main__":
    main()
