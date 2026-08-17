"""Small DuckDB API compatibility surface backed by a Doris MySQL connection.

This module is only for the experimental runner. Canonical tasks continue to
report DB_TYPE=duckdb so their existing verifier branch is exercised; the
connection itself is redirected to the Doris instance.
"""

from __future__ import annotations

import os
import re
import csv
from typing import Any

import mysql.connector


class _Frame:
    """Tiny DataFrame subset used by the promotional task's CSV export."""

    def __init__(self, rows, columns):
        self._rows = rows
        self._columns = columns

    def __len__(self):
        return len(self._rows)

    def to_csv(self, path, index=False):
        del index
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(self._columns)
            writer.writerows(self._rows)


def _split_sql_arguments(value: str) -> list[str]:
    arguments = []
    start = 0
    depth = 0
    quote = None
    index = 0
    while index < len(value):
        character = value[index]
        if quote:
            if character == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"'}:
            quote = character
        elif character == "(":
            depth += 1
        elif character == ")":
            depth -= 1
        elif character == "," and depth == 0:
            arguments.append(value[start:index].strip())
            start = index + 1
        index += 1
    arguments.append(value[start:].strip())
    return arguments


def _rewrite_date_diff_calls(sql: str) -> str:
    pattern = re.compile(r"\b(?:DATE_DIFF|DATEDIFF)\s*\(", re.IGNORECASE)
    offset = 0
    while True:
        match = pattern.search(sql, offset)
        if match is None:
            break
        open_parenthesis = sql.find("(", match.start())
        depth = 1
        quote = None
        index = open_parenthesis + 1
        while index < len(sql) and depth:
            character = sql[index]
            if quote:
                if character == quote:
                    if index + 1 < len(sql) and sql[index + 1] == quote:
                        index += 1
                    else:
                        quote = None
            elif character in {"'", '"'}:
                quote = character
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
            index += 1
        if depth:
            break
        close_parenthesis = index - 1
        arguments = _split_sql_arguments(sql[open_parenthesis + 1 : close_parenthesis])
        if len(arguments) != 3:
            offset = close_parenthesis + 1
            continue
        unit = arguments[0].strip().strip("'\"").upper()
        if unit == "DAY":
            replacement = f"DATEDIFF({arguments[2]}, {arguments[1]})"
        elif unit in {"MONTH", "YEAR", "HOUR", "MINUTE", "SECOND"}:
            replacement = f"TIMESTAMPDIFF({unit}, {arguments[1]}, {arguments[2]})"
        else:
            offset = close_parenthesis + 1
            continue
        sql = sql[: match.start()] + replacement + sql[close_parenthesis + 1 :]
        offset = match.start() + len(replacement)
    return sql


def _rewrite_strftime_calls(sql: str) -> str:
    pattern = re.compile(r"\bSTRFTIME\s*\(", re.IGNORECASE)
    offset = 0
    while True:
        match = pattern.search(sql, offset)
        if match is None:
            break
        open_parenthesis = sql.find("(", match.start())
        depth = 1
        quote = None
        index = open_parenthesis + 1
        while index < len(sql) and depth:
            character = sql[index]
            if quote:
                if character == quote:
                    if index + 1 < len(sql) and sql[index + 1] == quote:
                        index += 1
                    else:
                        quote = None
            elif character in {"'", '"'}:
                quote = character
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
            index += 1
        if depth:
            break
        close_parenthesis = index - 1
        arguments = _split_sql_arguments(sql[open_parenthesis + 1 : close_parenthesis])
        if len(arguments) != 2:
            offset = close_parenthesis + 1
            continue
        replacement = f"DATE_FORMAT({arguments[0]}, {arguments[1]})"
        sql = sql[: match.start()] + replacement + sql[close_parenthesis + 1 :]
        offset = match.start() + len(replacement)
    return sql


def _rewrite_postfix_casts(sql: str) -> str:
    operand = r"(?:[A-Za-z_][A-Za-z0-9_.]*|[A-Za-z_][A-Za-z0-9_]*\([^()]*\)|\([^()]*\))"
    data_type = r"[A-Za-z_][A-Za-z0-9_]*(?:\s*\(\s*\d+\s*(?:,\s*\d+\s*)?\))?"
    pattern = re.compile(rf"({operand})\s*::\s*({data_type})", re.IGNORECASE)
    while True:
        rewritten, count = pattern.subn(r"CAST(\1 AS \2)", sql)
        sql = rewritten
        if count == 0:
            return sql


def _rewrite_ordered_percentiles(sql: str) -> str:
    return re.sub(
        r"\bPERCENTILE_CONT\s*\(\s*([^()]+?)\s*\)\s*"
        r"WITHIN\s+GROUP\s*\(\s*ORDER\s+BY\s+([^()]+?)\s*\)",
        r"PERCENTILE(\2, \1)",
        sql,
        flags=re.IGNORECASE,
    )


def _rewrite_dayofweek(sql: str) -> str:
    """Preserve DuckDB's Sunday=0 numbering on Doris' Sunday=1 function."""
    return re.sub(
        r"\bEXTRACT\s*\(\s*DAYOFWEEK\s+FROM\s+([^()]+?)\s*\)",
        r"(DAYOFWEEK(\1) - 1)",
        sql,
        flags=re.IGNORECASE,
    )


def _rewrite_concat_operators(sql: str) -> str:
    atom = r"(?:CAST\([^()]+\)|[A-Za-z_][A-Za-z0-9_.]*)"
    sql = re.sub(
        rf"(?P<left>{atom})\s*\|\|\s*(?P<middle>'[^']*')\s*\|\|\s*"
        rf"(?P<right>{atom})",
        r"CONCAT(\g<left>, \g<middle>, \g<right>)",
        sql,
        flags=re.IGNORECASE,
    )
    return re.sub(
        rf"(?P<left>{atom})\s*\|\|\s*(?P<right>'[^']*'|{atom})",
        r"CONCAT(\g<left>, \g<right>)",
        sql,
        flags=re.IGNORECASE,
    )


def _rewrite_nested_postfix_date_casts(sql: str) -> str:
    return re.sub(
        r"(\(\s*DATE_TRUNC\([^)]*\)\s*\+\s*INTERVAL\s+\d+\s+"
        r"(?:DAY|MONTH|YEAR)\s*\))\s*::\s*DATE\b",
        r"CAST(\1 AS DATE)",
        sql,
        flags=re.IGNORECASE,
    )


def _rewrite_scalar_max_date_subtractions(sql: str) -> str:
    """Rewrite DuckDB scalar-subquery DATE subtraction used by RFM verifiers."""
    return re.sub(
        r"(\(\s*select\s+CAST\(\s*max\(\s*redeemed_at\s*\)\s+"
        r"AS\s+date\s*\).*?\))\s*-\s*"
        r"(CAST\(\s*max\(\s*redeemed_at\s*\)\s+AS\s+date\s*\))",
        r"DATEDIFF(\1, \2)",
        sql,
        flags=re.IGNORECASE | re.DOTALL,
    )


def _rewrite_is_distinct_from(sql: str) -> str:
    """Translate DuckDB's null-safe inequality to Doris' null-safe equality."""
    operand = (
        r"(?:[A-Za-z_][A-Za-z0-9_]*\([^()]*\)"
        r"|[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)?)"
    )
    return re.sub(
        rf"({operand})\s+IS\s+DISTINCT\s+FROM\s+({operand})",
        r"NOT (\1 <=> \2)",
        sql,
        flags=re.IGNORECASE,
    )


def _rewrite_deferred_revenue_date_differences(sql: str) -> str:
    sql = re.sub(
        r"\(\s*d\.RECOGNITION_END\s*-\s*d\.RECOGNITION_START\s*\)",
        "DATEDIFF(d.RECOGNITION_END, d.RECOGNITION_START)",
        sql,
        flags=re.IGNORECASE,
    )
    return re.sub(
        r"\(\s*LEAST\(\s*RECOGNITION_END\s*,\s*period_end_date\s*\)\s*-\s*"
        r"GREATEST\(\s*RECOGNITION_START\s*,\s*period_start_date\s*\)\s*\)",
        "DATEDIFF(LEAST(RECOGNITION_END, period_end_date), "
        "GREATEST(RECOGNITION_START, period_start_date))",
        sql,
        flags=re.IGNORECASE,
    )


def _rewrite_simple_date_subtractions(sql: str) -> str:
    # DuckDB verifiers often leave DATE subtraction unwrapped (the result is
    # an integer in DuckDB).  Doris treats DATE values as encoded numbers for
    # the binary `-` operator, so translate these exact date expressions
    # before the generic identifier pattern below.
    sql = re.sub(
        r"\bCAST\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s+AS\s+DATE\s*\)\s*-\s*"
        r"DATE\s+'([^']+)'",
        r"DATEDIFF(CAST(\1 AS DATE), DATE '\2')",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\bCAST\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s+AS\s+DATE\s*\)\s*-\s*"
        r"CAST\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s+AS\s+DATE\s*\)",
        r"DATEDIFF(CAST(\1 AS DATE), CAST(\2 AS DATE))",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\bCAST\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*-\s*"
        r"([A-Za-z_][A-Za-z0-9_.]*)\s+AS\s+INTEGER\s*\)",
        r"DATEDIFF(\1, \2)",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\b([A-Za-z_][A-Za-z0-9_.]*)\s*-\s*DATE\s+'([^']+)'",
        r"DATEDIFF(\1, DATE '\2')",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"CAST\(\s*CAST\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s+AS\s+DATE\s*\)\s*-\s*"
        r"CAST\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s+AS\s+DATE\s*\)\s+AS\s+INTEGER\s*\)",
        r"DATEDIFF(CAST(\1 AS DATE), CAST(\2 AS DATE))",
        sql,
        flags=re.IGNORECASE,
    )
    return sql


def _rewrite_pragma_table_info(sql: str) -> str:
    pattern = re.compile(
        r"\bFROM\s+pragma_table_info\(\s*'([A-Za-z_][A-Za-z0-9_$]*)\."
        r"([A-Za-z_][A-Za-z0-9_$]*)'\s*\)",
        re.IGNORECASE,
    )

    def replacement(match: re.Match) -> str:
        schema, table = match.groups()
        return f"""FROM (
            SELECT column_name AS name,
                   CASE
                       WHEN upper(column_type) LIKE 'TINYINT(1)'
                           THEN 'BOOLEAN'
                       WHEN upper(column_type) LIKE 'INT%'
                           THEN 'INTEGER'
                       ELSE column_type
                   END AS type,
                   ordinal_position - 1 AS cid
            FROM information_schema.columns
            WHERE lower(table_schema) = lower('{schema}')
              AND lower(table_name) = lower('{table}')
        ) AS pragma_table_info"""

    return pattern.sub(replacement, sql)


def _ensure_derived_aliases(sql: str) -> str:
    """Add aliases to DuckDB-style FROM (SELECT ...) subqueries."""
    pattern = re.compile(r"\bFROM\s*\(", re.IGNORECASE)
    offset = 0
    alias_number = 0
    while True:
        match = pattern.search(sql, offset)
        if match is None:
            return sql
        open_parenthesis = sql.find("(", match.start())
        depth = 1
        quote = None
        index = open_parenthesis + 1
        while index < len(sql) and depth:
            character = sql[index]
            if quote:
                if character == quote:
                    if index + 1 < len(sql) and sql[index + 1] == quote:
                        index += 1
                    else:
                        quote = None
            elif character in {"'", '"'}:
                quote = character
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
            index += 1
        if depth:
            return sql
        close_parenthesis = index - 1
        remainder = sql[close_parenthesis + 1 :]
        token = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)", remainder)
        if token and token.group(1).upper() not in {
            "WHERE", "GROUP", "ORDER", "LIMIT", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER", "ON", "UNION"
        }:
            offset = close_parenthesis + 1
            continue
        alias_number += 1
        alias = f" AS _derived_{alias_number}"
        sql = sql[:close_parenthesis + 1] + alias + sql[close_parenthesis + 1 :]
        offset = close_parenthesis + 1 + len(alias)


def _rewrite_sql(query: str) -> str:
    # DuckDB accepts ANSI double quotes for identifiers; Doris' MySQL parser
    # expects backticks unless ANSI_QUOTES is enabled.
    query = re.sub(r'"([A-Za-z_][A-Za-z0-9_$]*)"', r'`\1`', query)
    # The retail fixture uses upper-case Doris database names. DuckDB treats
    # schema identifiers case-insensitively, so canonical verifiers sometimes
    # spell the same source schema in lower case.
    raw_relations = (
        "ANALYTICS.FACT_SALES",
        "ANALYTICS.DIM_CUSTOMER",
        "ANALYTICS.DIM_DATE",
        "CUSTOMER.CUSTOMERS",
        "CUSTOMER.CUSTOMER_TIERS",
        "CUSTOMER.CUSTOMER_TIER_HISTORY",
        "ORDERS.ORDERS",
        "ORDERS.ORDER_LINES",
        "PROCUREMENT.SUPPLIER_INVOICES",
        "PROCUREMENT.SUPPLIERS",
        "FINANCE.CURRENCY_EXCHANGE_RATES",
        "FINANCE.CUSTOMER_INVOICES",
        "FINANCE.CUSTOMER_PAYMENT_APPLICATIONS",
        "FINANCE.CUSTOMER_PAYMENTS",
        "FINANCE.CUSTOMER_CREDITS",
    )
    for relation in raw_relations:
        query = re.sub(
            rf"(?<![A-Za-z0-9_`]){re.escape(relation)}\b",
            relation,
            query,
            flags=re.IGNORECASE,
        )
    # Do not normalize the output schema `analytics`: some standalone tasks
    # intentionally create a lower-case Doris database with that name.
    for schema in ("CUSTOMER", "ORDERS", "PRODUCT", "INVENTORY", "PROCUREMENT", "FINANCE"):
        query = re.sub(
            rf"(?<![A-Za-z0-9_`]){schema}\.",
            f"{schema}.",
            query,
            flags=re.IGNORECASE,
        )
    query = re.sub(
        r"\bTYPEOF\s*\([^)]*\)\s*=\s*'DATE'", "FALSE", query, flags=re.IGNORECASE
    )
    query = re.sub(
        r"\bTYPEOF\s*\([^)]*\)\s*!=\s*'DATE'", "TRUE", query, flags=re.IGNORECASE
    )
    query = re.sub(
        r"\bTO_VARCHAR\s*\(([^()]*)\)", r"CAST(\1 AS VARCHAR)", query, flags=re.IGNORECASE
    )
    query = re.sub(
        r"\bSTRPTIME\s*\(([^,]+),\s*'([^']+)'\s*\)",
        r"STR_TO_DATE(\1, '\2')",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"\bTRY_TO_DATE\s*\(([^,()]+|CAST\([^()]+\)),\s*'([^']+)'\s*\)",
        r"STR_TO_DATE(\1, '\2')",
        query,
        flags=re.IGNORECASE,
    )
    query = query.replace("'YYYYMMDD'", "'%Y%m%d'").replace("'YYYY-MM-DD'", "'%Y-%m-%d'")
    query = re.sub(
        r"\bTRY_TO_DATE\s*\(([^()]*)\)", r"CAST(\1 AS DATE)", query, flags=re.IGNORECASE
    )
    query = re.sub(r"\bNUMERIC\b", "DECIMAL", query, flags=re.IGNORECASE)
    query = re.sub(r"\bDOUBLE\s+PRECISION\b", "DOUBLE", query, flags=re.IGNORECASE)
    query = re.sub(r"\bAS\s+TIMESTAMP\b", "AS DATETIME", query, flags=re.IGNORECASE)
    query = _rewrite_postfix_casts(query)
    query = _rewrite_dayofweek(query)
    query = _rewrite_concat_operators(query)
    query = _rewrite_scalar_max_date_subtractions(query)
    query = _rewrite_ordered_percentiles(query)
    query = _rewrite_is_distinct_from(query)
    query = _rewrite_deferred_revenue_date_differences(query)
    query = _rewrite_simple_date_subtractions(query)
    query = _rewrite_pragma_table_info(query)
    query = re.sub(r"\bARG_MAX\s*\(", "MAX_BY(", query, flags=re.IGNORECASE)
    query = _ensure_derived_aliases(query)
    query = _rewrite_date_diff_calls(query)
    query = _rewrite_strftime_calls(query)
    query = query.replace("'%Y-%W'", "'%Y-%u'")
    query = re.sub(
        r"CAST\s*\(\s*\(\s*EXTRACT\s*\(\s*EPOCH\s+FROM\s+\(\s*([^()]+?)\s*-\s*([^()]+?)\s*\)\s*\)\s*/\s*3600\s*\)\s+AS\s+BIGINT\s*\)",
        r"TIMESTAMPDIFF(HOUR, \2, \1)",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(r"\?(?=([^']|'[^']*')*$)", "%s", query)
    query = re.sub(
        r"date_diff\(\s*'day'\s*,\s*([^,]+),\s*([^\)]+)\)",
        r"DATEDIFF(\2, \1)",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"date_diff\(\s*'month'\s*,\s*([^,]+),\s*([^\)]+)\)",
        r"TIMESTAMPDIFF(MONTH, \1, \2)",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(r"\bepoch\(([^\)]+)\)", r"UNIX_TIMESTAMP(\1)", query, flags=re.IGNORECASE)
    query = re.sub(
        r"interval\s+'(\d+)\s+(day|days|month|months|year|years)'",
        lambda match: f"INTERVAL {match.group(1)} {match.group(2).rstrip('sS').upper()}",
        query,
        flags=re.IGNORECASE,
    )
    query = _rewrite_nested_postfix_date_casts(query)
    query = re.sub(
        r"\bCAST\(\s*(REGEXP_EXTRACT\(\s*[^,]+,\s*'[^']*',\s*1\s*\))"
        r"\s+AS\s+INTEGER\s*\)",
        r"TRY_CAST(NULLIF(\1, '') AS INTEGER)",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"\bCAST\(\s*(SPLIT_PART\([^)]*\))\s+AS\s+(DECIMAL\([^)]*\))\s*\)",
        r"TRY_CAST(\1 AS \2)",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"\b([A-Za-z_][A-Za-z0-9_.]*)\s*\+\s*INTERVAL\s+1\s+DAY\s*\*\s*"
        r"([A-Za-z_][A-Za-z0-9_.]*)",
        r"DAYS_ADD(\1, \2)",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"\bNOT\s+SIMILAR\s+TO\s+'([^']+)'",
        r"NOT REGEXP '\1'",
        query,
        flags=re.IGNORECASE,
    )
    query = re.sub(
        r"\bmedian\s*\(([^\)]+)\)", r"PERCENTILE_APPROX(\1, 0.5)", query, flags=re.IGNORECASE
    )
    return query


class _Cursor:
    def __init__(self, connection):
        self._connection = connection
        self._cursor = None

    @property
    def description(self):
        return self._cursor.description if self._cursor is not None else None

    def execute(self, query: str, params=None):
        self._cursor = self._connection._execute(query, params)
        return self

    def fetchall(self):
        return self._cursor.fetchall()

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchdf(self):
        rows = self._cursor.fetchall()
        columns = [item[0] for item in (self._cursor.description or ())]
        import pandas as pd

        return pd.DataFrame(rows, columns=columns)

    def df(self):
        return self.fetchdf()

    def close(self):
        if self._cursor is not None:
            self._cursor.close()


class Connection:
    def __init__(self):
        self._mysql = mysql.connector.connect(
            host=os.environ.get("DORIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("DORIS_PORT", "29030")),
            user=os.environ.get("DORIS_USER", "root"),
            password=os.environ.get("DORIS_PASSWORD", ""),
            database=os.environ.get("DORIS_TARGET_DATABASE", "main"),
            autocommit=True,
            buffered=True,
        )
        self._result = None

    def _execute(self, query: str, params=None):
        query = _rewrite_sql(query)
        cursor = self._mysql.cursor(buffered=True)
        cursor.execute(query, params) if params else cursor.execute(query)
        return cursor

    def execute(self, query: str, params=None):
        self._result = _Cursor(self)
        return self._result.execute(query, params)

    def cursor(self):
        return _Cursor(self)

    @property
    def description(self):
        return self._result.description if self._result is not None else None

    def fetchall(self):
        return self._result.fetchall()

    def fetchone(self):
        return self._result.fetchone()

    def close(self):
        self._mysql.close()

    def commit(self):
        self._mysql.commit()

    def rollback(self):
        self._mysql.rollback()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def connect(_database=None, **_kwargs) -> Connection:
    return Connection()
