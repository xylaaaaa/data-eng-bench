#!/opt/dbt-doris/bin/python
"""Run a canonical DuckDB task with dbt-doris for compatibility probing.

The canonical task remains untouched. The wrapper only changes the ephemeral
container's generated profile and broad SQL target-type branches before calling
the real dbt executable.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import yaml


REAL_DBT = "/opt/dbt-doris/bin/dbt"
TYPE_BRANCHES = (
    (r"target\.type\s*==\s*['\"]duckdb['\"]", "target.type in ['duckdb', 'doris']"),
    (r"target\.type\s*!=\s*['\"]duckdb['\"]", "target.type not in ['duckdb', 'doris']"),
)


def _split_sql_arguments(value: str) -> list[str]:
    arguments: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
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
        quote: str | None = None
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
        quote: str | None = None
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
    """Translate the simple scalar concatenations used by benchmark models."""
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


def _rewrite_department_hierarchy(sql: str) -> str:
    """Unroll the workforce task's recursive department tree for Doris."""
    marker = re.search(r"\bWITH\s+RECURSIVE\s+dept_hierarchy\s+AS\s*\(", sql, re.IGNORECASE)
    if marker is None:
        return sql

    relation = "{{ ref('stg_hr__departments') }}"
    ctes = [
        f"""level_1 as (
    select
        department_id,
        department_name,
        parent_department_id,
        department_id as level_1_department_id,
        department_name as level_1_department_name,
        CAST(null AS varchar) as level_2_department_id,
        CAST(null AS varchar) as level_2_department_name,
        CAST(null AS varchar) as level_3_department_id,
        CAST(null AS varchar) as level_3_department_name,
        1 as hierarchy_depth,
        department_name as full_path
    from {relation}
    where parent_department_id is null
)"""
    ]
    for depth in range(2, 11):
        previous = f"level_{depth - 1}"
        level_2_id = "d.department_id" if depth == 2 else "p.level_2_department_id"
        level_2_name = "d.department_name" if depth == 2 else "p.level_2_department_name"
        level_3_id = "d.department_id" if depth == 3 else "p.level_3_department_id"
        level_3_name = "d.department_name" if depth == 3 else "p.level_3_department_name"
        ctes.append(
            f"""level_{depth} as (
    select
        d.department_id,
        d.department_name,
        d.parent_department_id,
        p.level_1_department_id,
        p.level_1_department_name,
        {level_2_id} as level_2_department_id,
        {level_2_name} as level_2_department_name,
        {level_3_id} as level_3_department_id,
        {level_3_name} as level_3_department_name,
        {depth} as hierarchy_depth,
        CONCAT(p.full_path, ' > ', d.department_name) as full_path
    from {relation} d
    join {previous} p on d.parent_department_id = p.department_id
)"""
        )
    unions = "\n    union all\n    ".join(f"select * from level_{depth}" for depth in range(1, 11))
    ctes.append(f"dept_hierarchy as (\n    {unions}\n)")
    body = """select
    department_id,
    department_name,
    level_1_department_id,
    level_1_department_name,
    level_2_department_id,
    level_2_department_name,
    level_3_department_id,
    level_3_department_name,
    hierarchy_depth,
    full_path
from dept_hierarchy
"""
    return sql[: marker.start()] + "with\n" + ",\n\n".join(ctes) + "\n\n" + body


def _rewrite_nested_postfix_date_casts(sql: str) -> str:
    return re.sub(
        r"(\(\s*DATE_TRUNC\([^)]*\)\s*\+\s*INTERVAL\s+\d+\s+"
        r"(?:DAY|MONTH|YEAR)\s*\))\s*::\s*DATE\b",
        r"CAST(\1 AS DATE)",
        sql,
        flags=re.IGNORECASE,
    )


def _rewrite_schedule_time_diffs(sql: str) -> str:
    return re.sub(
        r"TIMESTAMPDIFF\(\s*SECOND\s*,\s*(s\.start_time)\s*,\s*(s\.end_time)\s*\)",
        r"TIMESTAMPDIFF(SECOND, "
        r"CAST(CONCAT('2000-01-01 ', \1) AS DATETIME), "
        r"CAST(CONCAT('2000-01-01 ', \2) AS DATETIME))",
        sql,
        flags=re.IGNORECASE,
    )


def _rewrite_rfm_concat(sql: str) -> str:
    return re.sub(
        r"'RFM_'\s*\|\|\s*CAST\(r_score\s+AS\s+VARCHAR\)\s*\|\|\s*"
        r"CAST\(f_score\s+AS\s+VARCHAR\)\s*\|\|\s*"
        r"CAST\(m_score\s+AS\s+VARCHAR\)",
        "CONCAT('RFM_', CAST(r_score AS VARCHAR), "
        "CAST(f_score AS VARCHAR), CAST(m_score AS VARCHAR))",
        sql,
        flags=re.IGNORECASE,
    )


def _rewrite_recursive_month_series(sql: str) -> str:
    """Replace DuckDB's recursive month CTE with Doris' numbers table."""
    if not re.search(r"\bWITH\s+RECURSIVE\b", sql, re.IGNORECASE):
        return sql
    pattern = re.compile(r"\bmonths\s+as\s*\(", re.IGNORECASE)
    match = pattern.search(sql)
    if match is None:
        return sql
    open_parenthesis = sql.find("(", match.start())
    depth = 1
    quote: str | None = None
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
    body = sql[open_parenthesis + 1 : close_parenthesis]
    if not re.search(r"\bfrom\s+months\b", body, re.IGNORECASE):
        return sql
    replacement = """months as (
    select MONTHS_ADD(DATE_TRUNC(min_date, 'month'), number) as month_start
    from date_range
    cross join numbers("number" = "1000")
    where MONTHS_ADD(DATE_TRUNC(min_date, 'month'), number)
          <= DATE_TRUNC(max_date, 'month')
)"""
    sql = sql[: match.start()] + replacement + sql[close_parenthesis + 1 :]
    return re.sub(r"\bWITH\s+RECURSIVE\b", "WITH", sql, count=1, flags=re.IGNORECASE)


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


def rewrite_doris_sql(sql: str) -> str:
    """Apply small, mechanical DuckDB/Snowflake-to-Doris SQL rewrites."""
    sql = _rewrite_department_hierarchy(sql)
    # DuckDB-only date parsing idioms used by a few benchmark verifiers and
    # solutions. The Doris fixture's date_key is numeric, so the TYPEOF branch
    # is deterministically false; STRPTIME/TRY_TO_DATE use Doris' equivalent.
    sql = re.sub(
        r"\bTYPEOF\s*\([^)]*\)\s*=\s*'DATE'",
        "FALSE",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\bTYPEOF\s*\([^)]*\)\s*!=\s*'DATE'",
        "TRUE",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(r"\bTO_VARCHAR\s*\(([^()]*)\)", r"CAST(\1 AS VARCHAR)", sql, flags=re.IGNORECASE)
    sql = re.sub(
        r"\bSTRPTIME\s*\(([^,]+),\s*'([^']+)'\s*\)",
        r"STR_TO_DATE(\1, '\2')",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\bTRY_TO_DATE\s*\(([^,()]+|CAST\([^()]+\)),\s*'([^']+)'\s*\)",
        r"STR_TO_DATE(\1, '\2')",
        sql,
        flags=re.IGNORECASE,
    )
    sql = sql.replace("'YYYYMMDD'", "'%Y%m%d'").replace("'YYYY-MM-DD'", "'%Y-%m-%d'")
    sql = re.sub(
        r"\bTRY_TO_DATE\s*\(([^()]*)\)",
        r"CAST(\1 AS DATE)",
        sql,
        flags=re.IGNORECASE,
    )
    sql = _rewrite_recursive_month_series(sql)
    sql = _rewrite_deferred_revenue_date_differences(sql)
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
    sql = re.sub(r"\bNUMERIC\b", "DECIMAL", sql, flags=re.IGNORECASE)
    sql = re.sub(r"\bDOUBLE\s+PRECISION\b", "DOUBLE", sql, flags=re.IGNORECASE)
    # Doris exposes the timestamp type as DATETIME/DATETIMEV2; TIMESTAMP is
    # not accepted as a CAST target by the 4.0 parser.
    sql = re.sub(r"\bAS\s+TIMESTAMP\b", "AS DATETIME", sql, flags=re.IGNORECASE)
    sql = _rewrite_postfix_casts(sql)
    sql = _rewrite_dayofweek(sql)
    sql = _rewrite_rfm_concat(sql)
    sql = _rewrite_concat_operators(sql)
    sql = _rewrite_ordered_percentiles(sql)
    # DuckDB's `/` operator returns floating-point output for integer ratios.
    # Doris can retain DECIMAL for the same expression, which changes the
    # Python value type seen by an otherwise backend-neutral verifier.
    sql = re.sub(
        r"\b((?:[A-Za-z_][A-Za-z0-9_]*\.)?[A-Za-z_][A-Za-z0-9_]*)\s*/\s*NULLIF\(",
        r"CAST(\1 AS DOUBLE) / NULLIF(",
        sql,
        flags=re.IGNORECASE,
    )
    sql = _rewrite_strftime_calls(sql)
    sql = sql.replace("'%Y-%W'", "'%Y-%u'")
    sql = re.sub(
        r"CAST\s*\(\s*\(\s*EXTRACT\s*\(\s*EPOCH\s+FROM\s+\(\s*([^()]+?)\s*-\s*([^()]+?)\s*\)\s*\)\s*/\s*3600\s*\)\s+AS\s+BIGINT\s*\)",
        r"TIMESTAMPDIFF(HOUR, \2, \1)",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(r"\bEPOCH\(([^\)]+)\)", r"UNIX_TIMESTAMP(\1)", sql, flags=re.IGNORECASE)
    sql = re.sub(
        r"\bINTERVAL\s+'(\d+)\s+(DAY|DAYS|MONTH|MONTHS|YEAR|YEARS)'",
        lambda match: f"INTERVAL {match.group(1)} {match.group(2).rstrip('sS').upper()}",
        sql,
        flags=re.IGNORECASE,
    )
    sql = _rewrite_nested_postfix_date_casts(sql)
    sql = sql.replace(r"'/(\d+)'", "'/([0-9]+)'")
    sql = sql.replace(r"'Net\s*(\d+)'", "'Net[ ]*([0-9]+)'")
    sql = re.sub(
        r"\bCAST\(\s*(REGEXP_EXTRACT\(\s*[^,]+,\s*'[^']*',\s*1\s*\))"
        r"\s+AS\s+INTEGER\s*\)",
        r"TRY_CAST(NULLIF(\1, '') AS INTEGER)",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\bCAST\(\s*(SPLIT_PART\([^)]*\))\s+AS\s+(DECIMAL\([^)]*\))\s*\)",
        r"TRY_CAST(\1 AS \2)",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\b([A-Za-z_][A-Za-z0-9_.]*)\s*\+\s*INTERVAL\s+1\s+DAY\s*\*\s*"
        r"([A-Za-z_][A-Za-z0-9_.]*)",
        r"DAYS_ADD(\1, \2)",
        sql,
        flags=re.IGNORECASE,
    )
    # Doris enforces full grouping even when a one-row reference-date CTE is
    # cross joined. DuckDB accepts the scalar as functionally dependent.
    if re.search(r"\bCROSS\s+JOIN\s+ref_date\s+ref\b", sql, re.IGNORECASE):
        sql = re.sub(
            r"\bGROUP\s+BY\s+customer_id\b",
            "GROUP BY customer_id, ref.ref_date",
            sql,
            flags=re.IGNORECASE,
        )
    sql = _rewrite_date_diff_calls(sql)
    return _rewrite_schedule_time_diffs(sql)


def candidate_profiles(arguments: list[str]) -> list[Path]:
    paths: list[Path] = []
    for index, argument in enumerate(arguments):
        if argument in {"--profiles-dir", "--profile-dir"} and index + 1 < len(arguments):
            paths.append(Path(arguments[index + 1]) / "profiles.yml")
    paths.append(Path(os.environ.get("DBT_PROFILES_DIR", "")) / "profiles.yml")
    paths.append(Path.cwd() / "profiles.yml")
    paths.append(Path.home() / ".dbt" / "profiles.yml")
    return list(dict.fromkeys(path for path in paths if path.is_file()))


def rewrite_profiles(arguments: list[str]) -> None:
    host = os.environ.get("DORIS_HOST", "127.0.0.1")
    port = int(os.environ.get("DORIS_PORT", "29030"))
    database = os.environ.get("DORIS_TARGET_DATABASE", "main")
    username = os.environ.get("DORIS_USER", "root")
    password = os.environ.get("DORIS_PASSWORD", "")
    threads = int(os.environ.get("DBT_THREADS", "4"))
    for path in candidate_profiles(arguments):
        document = yaml.safe_load(path.read_text()) or {}
        changed = False
        for profile in document.values():
            if not isinstance(profile, dict):
                continue
            outputs = profile.get("outputs")
            if not isinstance(outputs, dict):
                continue
            for output in outputs.values():
                if not isinstance(output, dict) or output.get("type") != "duckdb":
                    continue
                # Keep a task's explicitly selected target schema.  Standalone
                # tasks (for example daily_order_summary) use this to place the
                # relation where their verifier expects it; shared projects
                # commonly omit it and fall back to DORIS_TARGET_DATABASE.
                target_schema = output.get("schema") or database
                output.clear()
                output.update(
                    {
                        "type": "doris",
                        "host": host,
                        "port": port,
                        "username": username,
                        "password": password,
                        "schema": target_schema,
                        "threads": threads,
                    }
                )
                changed = True
        if changed:
            path.write_text(yaml.safe_dump(document, sort_keys=False))
        # A solution may export DBT_PROFILES_DIR only in the shell that
        # invokes this process.  The verifier starts a fresh dbt process, so
        # make the discovered profile location explicit on every invocation,
        # including when it was already converted to type=doris.
        if isinstance(document, dict) and document:
            os.environ["DBT_PROFILES_DIR"] = str(path.parent)


def patch_sql_branches() -> None:
    roots = {Path.cwd()}
    profiles_dir = os.environ.get("DBT_PROFILES_DIR")
    if profiles_dir:
        roots.add(Path(profiles_dir))
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.sql"):
            # dbt-doris snapshot artifacts can include a directory whose name
            # ends in `.sql`; only source/artifact files are rewrite targets.
            if not path.is_file():
                continue
            original = path.read_text()
            rewritten = original
            # The DuckDB solution for supplier payment uses a correlated
            # LATERAL lookup. Doris does not implement that form, while the
            # task already contains an equivalent ROW_NUMBER branch for
            # Snowflake. Select that branch for the Doris experiment only.
            rewritten = rewritten.replace(
                "{% if target.type == 'snowflake' %}\nlatest_rates as (",
                "{% if target.type in ['snowflake', 'doris'] %}\nlatest_rates as (",
            )
            # The marketing RFM model already has a three-argument DATEDIFF
            # branch. Select it for Doris so the normal balanced DATEDIFF
            # rewrite can translate it, instead of trying to parse a DATE
            # subtraction across an unrendered Jinja branch.
            rewritten = re.sub(
                r"\{%\s*if\s+target\.type\s*==\s*['\"]snowflake['\"]\s*%\}"
                r"(?=\s*DATEDIFF\(\s*['\"]day['\"]\s*,\s*max\(redeemed_at\))",
                "{% if target.type in ['snowflake', 'doris'] %}",
                rewritten,
                flags=re.IGNORECASE,
            )
            # The supplier-payment task puts a DuckDB REGEXP_EXTRACT inside a
            # Jinja branch and wraps the branch in CAST(... AS INTEGER).  The
            # normal SQL rewrite runs before Jinja rendering, so it cannot see
            # the eventual function call.  For Doris, make the non-Snowflake
            # branch explicitly NULL-safe: NET15/PREPAID rows legitimately
            # produce an empty regexp match, and Doris' strict CAST rejects
            # that empty string during CTAS evaluation.
            rewritten = re.sub(
                r"cast\(\s*\{%\s*if\s+target\.type\s*==\s*['\"]snowflake['\"]\s*%\}"
                r".*?\{%\s*else\s*%\}\s*"
                r"(?P<extract>regexp_extract\(.*?\))\s*"
                r"\{%\s*endif\s*%\}\s*as\s+integer\s*\)",
                lambda match: (
                    "{% if target.type == 'snowflake' %}"
                    "CAST(REGEXP_SUBSTR(payment_terms_code, '/(\\\\d+)', 1, 1, 'e', 1) AS INTEGER)"
                    "{% else %}"
                    "TRY_CAST(NULLIF(" + match.group("extract") + ", '') AS INTEGER)"
                    "{% endif %}"
                ),
                rewritten,
                flags=re.IGNORECASE | re.DOTALL,
            )
            for pattern, replacement in TYPE_BRANCHES:
                rewritten = re.sub(pattern, replacement, rewritten)
            rewritten = rewrite_doris_sql(rewritten)
            if rewritten != original:
                path.write_text(rewritten)


def patch_project_configs() -> None:
    """Make canonical table models viable on the one-BE demo cluster.

    The upstream tasks target DuckDB/Snowflake and therefore do not set a
    Doris replication count.  A single local BE cannot satisfy the adapter's
    default of three replicas.  Applying this project-level config affects
    only the ephemeral compatibility container; it is not written back to the
    benchmark task.
    """
    roots = {Path.cwd()}
    profiles_dir = os.environ.get("DBT_PROFILES_DIR")
    if profiles_dir:
        roots.add(Path(profiles_dir))
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("dbt_project.yml"):
            if not path.is_file():
                continue
            document = yaml.safe_load(path.read_text()) or {}
            models = document.get("models")
            if not isinstance(models, dict):
                models = {}
                document["models"] = models
            if models.get("+replication_num") == 1:
                continue
            models["+replication_num"] = 1
            path.write_text(yaml.safe_dump(document, sort_keys=False))


def main() -> int:
    arguments = sys.argv[1:]
    rewrite_profiles(arguments)
    patch_sql_branches()
    patch_project_configs()
    environment = os.environ.copy()
    environment["DBT_SEND_ANONYMOUS_USAGE_STATS"] = "false"
    return subprocess.call([REAL_DBT, *arguments], env=environment)


if __name__ == "__main__":
    raise SystemExit(main())
