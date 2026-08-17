FROM ghcr.io/astral-sh/uv:0.11.8@sha256:3b7b60a81d3c57ef471703e5c83fd4aaa33abcd403596fb22ab07db85ae91347 AS uv

FROM ghcr.io/snowflake-labs/data-eng-bench-base:1.0.0@sha256:29bcb90579ecc8fc0dbb0d658b2a938374ac057ff816c78746520d48ed1edb09

ARG DBT_DORIS_VERSION=1.1.0
ARG DBT_CORE_VERSION=1.12.2
ARG MYSQL_CONNECTOR_VERSION=26.7.0

COPY --from=uv /uv /uvx /bin/
RUN uv venv --python python3 /opt/dbt-doris \
    && uv pip install --python /opt/dbt-doris/bin/python \
        "dbt-for-apache-doris==${DBT_DORIS_VERSION}" \
        "dbt-core==${DBT_CORE_VERSION}" \
        "duckdb==1.2.2" \
        "mysql-connector-python==${MYSQL_CONNECTOR_VERSION}" \
        "pandas==2.2.2" \
        "pytest==8.4.1" \
        "pytest-json-ctrf==0.3.5" \
        "pyyaml==6.0.2"

COPY dbt-wrapper.py /opt/doris-fast30/dbt-wrapper.py
COPY duckdb.py /opt/doris-fast30/duckdb.py
COPY load_duckdb.py /opt/doris-fast30/load_duckdb.py

RUN mkdir -p /opt/doris-fast30/bin \
    && printf '%s\n' '#!/bin/sh' 'exec /opt/doris-fast30/dbt-wrapper.py "$@"' \
       > /opt/doris-fast30/bin/dbt \
    && chmod 755 /opt/doris-fast30/bin/dbt /opt/doris-fast30/dbt-wrapper.py \
       /opt/doris-fast30/load_duckdb.py

ENV DBT_SEND_ANONYMOUS_USAGE_STATS=false
ENV PATH=/opt/doris-fast30/bin:/opt/dbt-doris/bin:${PATH}
ENV PYTHONPATH=/opt/doris-fast30:${PYTHONPATH}
