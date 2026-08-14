FROM ghcr.io/astral-sh/uv:0.11.8@sha256:3b7b60a81d3c57ef471703e5c83fd4aaa33abcd403596fb22ab07db85ae91347 AS uv

FROM ghcr.io/snowflake-labs/data-eng-bench-base:1.0.0@sha256:29bcb90579ecc8fc0dbb0d658b2a938374ac057ff816c78746520d48ed1edb09

ARG DBT_DORIS_VERSION=1.1.0
ARG DBT_CORE_VERSION=1.12.2
ARG MYSQL_CONNECTOR_VERSION=26.7.0

# This adapter requires dbt Core 1.12, while the canonical benchmark image is
# pinned below 1.11. Keep the two dependency graphs in separate environments
# while reusing the base image's terminal-agent toolchain (Node.js, tmux, git).
COPY --from=uv /uv /uvx /bin/
RUN uv venv --python python3 /opt/dbt-doris \
    && uv pip install --python /opt/dbt-doris/bin/python \
        "dbt-for-apache-doris==${DBT_DORIS_VERSION}" \
        "dbt-core==${DBT_CORE_VERSION}" \
        "mysql-connector-python==${MYSQL_CONNECTOR_VERSION}" \
        "pytest==8.4.1" \
        "pytest-json-ctrf==0.3.5"

COPY init_doris.py /init_doris.py

ENV DB_TYPE=doris
ENV DBT_PROFILES_DIR=/app/dbt_project
ENV DBT_SEND_ANONYMOUS_USAGE_STATS=false
ENV PATH=/opt/dbt-doris/bin:${PATH}

WORKDIR /app
