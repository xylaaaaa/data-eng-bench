#!/opt/dbt-doris/bin/python
"""Load selected relations from the benchmark DuckDB fixture into Doris.

This is deliberately a small, deterministic loader for the compatibility
experiment.  It materializes a relation (including a DuckDB view) as a Doris
OLAP table, preserving the source database/table names so the canonical task
SQL and verifiers can be exercised without changing their data semantics.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import re
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

# This file lives next to the verifier compatibility module named duckdb.py.
# Remove its directory while importing the real DuckDB package; otherwise
# Python's script-directory precedence would make the loader connect to Doris
# while it is trying to read the source fixture.
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path = [entry for entry in sys.path if Path(entry or ".").resolve() != _SCRIPT_DIR]

import duckdb
import mysql.connector


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


def quote_identifier(value: str) -> str:
    # Backtick quoting is required for Doris names such as ORDER_LINES and
    # also makes the loader safe for the fixed fixture metadata.
    return "`" + value.replace("`", "``") + "`"


def quote_relation(schema: str, table: str) -> str:
    return f"{quote_identifier(schema)}.{quote_identifier(table)}"


def quote_duckdb_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def quote_duckdb_relation(schema: str, table: str) -> str:
    return f"{quote_duckdb_identifier(schema)}.{quote_duckdb_identifier(table)}"


def split_relation(value: str) -> tuple[str, str]:
    pieces = value.split(".", 1)
    if len(pieces) != 2 or not all(IDENTIFIER.fullmatch(piece) for piece in pieces):
        raise ValueError(f"relation must be SCHEMA.TABLE, got {value!r}")
    return pieces[0], pieces[1]


def type_for_doris(source_type: str) -> str:
    upper = source_type.upper().strip()
    if upper.startswith("DECIMAL") or upper.startswith("NUMERIC"):
        match = re.search(r"\((\d+)\s*,\s*(\d+)\)", upper)
        if match:
            precision = min(int(match.group(1)), 38)
            scale = min(int(match.group(2)), precision)
            return f"DECIMAL({precision},{scale})"
        return "DECIMAL(38,10)"
    if upper.startswith("TIMESTAMP") or upper.startswith("DATETIME"):
        return "DATETIMEV2(6)"
    if upper == "DATE" or upper.startswith("DATE "):
        return "DATE"
    if upper.startswith("TIME") or upper.startswith("INTERVAL"):
        return "VARCHAR(65533)"
    if upper in {"BOOLEAN", "BOOL"}:
        return "BOOLEAN"
    if upper in {"TINYINT", "SMALLINT", "INTEGER", "INT"}:
        return "INT"
    if upper in {"BIGINT", "UBIGINT"}:
        return "BIGINT"
    if upper in {"HUGEINT", "UHUGEINT"}:
        return "DECIMAL(38,0)"
    if upper in {"FLOAT", "REAL"}:
        return "FLOAT"
    if upper in {"DOUBLE", "DOUBLE PRECISION"}:
        return "DOUBLE"
    if upper in {"BLOB", "BIT", "UUID"}:
        return "VARCHAR(65533)"
    # DuckDB's VARCHAR, JSON, ENUM, STRUCT and LIST values are serialized by
    # normalize_value below.  Use VARCHAR rather than Doris STRING: STRING is
    # deliberately rejected as a DUPLICATE KEY column by Doris.
    return "VARCHAR(65533)"


def normalize_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat(sep=" ") if isinstance(value, dt.datetime) else value.isoformat()
    if isinstance(value, dt.timedelta):
        return str(value)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, default=str, ensure_ascii=False)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def connect_doris():
    return mysql.connector.connect(
        host=os.environ.get("DORIS_HOST", "127.0.0.1"),
        port=int(os.environ.get("DORIS_PORT", "29030")),
        user=os.environ.get("DORIS_USER", "root"),
        password=os.environ.get("DORIS_PASSWORD", ""),
        autocommit=True,
        buffered=True,
    )


def relation_columns(source, schema: str, table: str) -> list[tuple[str, str]]:
    relation = quote_duckdb_relation(schema, table)
    rows = source.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()
    return [(str(row[0]), str(row[1])) for row in rows]


def create_target(
    cursor,
    schema: str,
    table: str,
    columns: list[tuple[str, str]],
    table_model: str,
) -> None:
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS {quote_identifier(schema)}")
    relation = quote_relation(schema, table)
    # The default fixture tables are append-only snapshots.  A task that
    # mutates its source rows can opt into Doris Unique Key semantics via the
    # explicit loader flag; all other tasks retain Duplicate Key behavior.
    column_sql = ",\n  ".join(
        f"{quote_identifier(name)} {type_for_doris(kind)}" for name, kind in columns
    )
    key = quote_identifier(columns[0][0])
    cursor.execute(
        """SELECT TABLE_TYPE
           FROM information_schema.tables
           WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s""",
        (schema, table),
    )
    existing = cursor.fetchone()
    if existing is not None:
        object_kind = "VIEW" if str(existing[0]).upper() == "VIEW" else "TABLE"
        cursor.execute(f"DROP {object_kind} {relation}")
    key_clause = (
        f"UNIQUE KEY({key})" if table_model == "unique" else f"DUPLICATE KEY({key})"
    )
    cursor.execute(
        f"""CREATE TABLE {relation} (
  {column_sql}
)
ENGINE=OLAP
{key_clause}
DISTRIBUTED BY HASH({key}) BUCKETS 1
PROPERTIES ("replication_num" = "1")"""
    )


def load_relation(
    source,
    target,
    schema: str,
    table: str,
    batch_size: int,
    table_model: str,
) -> int:
    relation = quote_relation(schema, table)
    columns = relation_columns(source, schema, table)
    if not columns:
        raise RuntimeError(f"source relation has no columns: {schema}.{table}")
    create_target(target.cursor(), schema, table, columns, table_model)
    names = ", ".join(quote_identifier(name) for name, _ in columns)
    placeholders = ", ".join("%s" for _ in columns)
    insert_sql = f"INSERT INTO {relation} ({names}) VALUES ({placeholders})"
    source_cursor = source.execute(f"SELECT * FROM {quote_duckdb_relation(schema, table)}")
    target_cursor = target.cursor()
    count = 0
    while True:
        rows = source_cursor.fetchmany(batch_size)
        if not rows:
            break
        target_cursor.executemany(
            insert_sql, [tuple(normalize_value(value) for value in row) for row in rows]
        )
        count += len(rows)
    target_cursor.close()
    print(f"loaded {schema}.{table}: {count} rows, {len(columns)} columns", flush=True)
    return count


def discover_relations(source) -> list[str]:
    rows = source.execute(
        """SELECT table_schema, table_name
           FROM information_schema.tables
           WHERE table_schema NOT IN ('information_schema', 'pg_catalog')
           ORDER BY table_schema, table_name"""
    ).fetchall()
    return [f"{row[0]}.{row[1]}" for row in rows]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("relations", nargs="*", help="SCHEMA.TABLE relations to materialize")
    parser.add_argument("--all", action="store_true", help="load every DuckDB table/view")
    parser.add_argument("--batch-size", type=int, default=500)
    parser.add_argument(
        "--unique-key",
        action="store_true",
        help="create selected relations with Doris UNIQUE KEY semantics for UPDATE tests",
    )
    parser.add_argument(
        "--duckdb-path",
        default=os.environ.get("DUCKDB_PATH", "/app/database/retail.duckdb"),
    )
    args = parser.parse_args()
    source = duckdb.connect(args.duckdb_path, read_only=True)
    try:
        relations = discover_relations(source) if args.all else args.relations
        if not relations:
            parser.error("provide at least one SCHEMA.TABLE or --all")
        target = connect_doris()
        try:
            for relation in relations:
                schema, table = split_relation(relation)
                load_relation(
                    source,
                    target,
                    schema,
                    table,
                    args.batch_size,
                    "unique" if args.unique_key else "duplicate",
                )
        finally:
            target.close()
    finally:
        source.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
