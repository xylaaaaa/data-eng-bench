#!/usr/bin/env bash
set -uo pipefail

mkdir -p /logs/verifier

pytest /tests/test_outputs.py -v -p no:cacheprovider 2>&1 \
    | tee /logs/verifier/pytest.log
exit_code=${PIPESTATUS[0]}

if [[ $exit_code -eq 0 ]]; then
    echo 1 > /logs/verifier/reward.txt
    echo "SUCCESS: Doris dbt demo passed"
else
    echo 0 > /logs/verifier/reward.txt
    echo "FAILURE: Doris dbt demo verifier failed"
fi

exit "$exit_code"
