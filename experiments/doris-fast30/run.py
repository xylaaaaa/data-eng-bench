#!/usr/bin/env python3
"""Run the Doris fast-30 compatibility suite in clean-room containers.

The runner is intentionally independent of Harbor.  It executes the canonical
golden solution and verifier for each manifest entry, while giving every task
its own Doris process, Docker network, and writable runner container.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

try:
    import yaml
except ImportError as error:  # pragma: no cover - depends on the host environment
    raise SystemExit("PyYAML is required to read tasks.yaml") from error


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
DEFAULT_MANIFEST = SCRIPT_DIR / "tasks.yaml"
DEFAULT_RUNNER_IMAGE = "data-eng-bench-doris-fast30:local"
DEFAULT_DORIS_IMAGE = (
    "apache/doris:4.0.3-all-slim@"
    "sha256:3237bfb73471da7482c6575cecc1632c96dec09509cd2e60bcb798e2c5342823"
)
DORIS_TMPFS_MOUNTS = (
    ("/opt/apache-doris/be/storage", "rw,size=4g,mode=0755"),
    ("/opt/apache-doris/fe/doris-meta", "rw,size=2g,mode=0755"),
)
DORIS_TRANSIENT_STARTUP_ERRORS = (
    "No available backends for compute group",
    "Failed to find enough backend",
)
RESOURCE_PREFIX = "deb-doris-fast30"
RESOURCE_LABEL = "org.data-eng-bench.doris-fast30.run"
IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
TASK_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
ALLOWED_PROJECT_MODES = {"minimal", "self_contained", "base"}
ALLOWED_DB_TYPES = {"duckdb", "doris"}
ALLOWED_ORACLES = {"canonical", "fifo_doris"}
ALLOWED_TASK_ENV = {"DBT_PROJECT_DIR_DUCKDB", "DBT_PROFILES_DIR", "DBT_THREADS"}
SYSTEM_DATABASES = {
    "information_schema",
    "__internal_schema",
    "mysql",
}
ARTIFACT_PROJECTS = (
    "/app/dbt_project",
    "/app/dbt_models_duckdb",
    "/app/dbt_transforms",
)
SHARED_INPUT_PATHS = (
    SCRIPT_DIR / "Dockerfile",
    SCRIPT_DIR / "dbt-wrapper.py",
    SCRIPT_DIR / "duckdb.py",
    SCRIPT_DIR / "load_duckdb.py",
    SCRIPT_DIR / "run.py",
    SCRIPT_DIR / "tasks.yaml",
    REPO_ROOT / "configs" / "fast-30.txt",
)
VERIFIER_RESULT = re.compile(
    r"^Test results:\s*(\d+) passed,\s*(\d+) skipped,\s*(\d+) failed$",
    re.MULTILINE,
)


class ManifestError(ValueError):
    """Raised when the task manifest is unsafe or internally inconsistent."""


class CommandError(RuntimeError):
    """Raised when an orchestration command exits unsuccessfully."""

    def __init__(self, command: Sequence[str], returncode: int, output: str = ""):
        self.command = tuple(command)
        self.returncode = returncode
        self.output = output
        super().__init__(
            f"command exited {returncode}: {' '.join(command)}"
            + (f"\n{output.rstrip()}" if output else "")
        )


class TaskSpec:
    def __init__(
        self,
        task,
        project_mode,
        project_path,
        container_project_dir,
        db_type,
        environment,
        duplicate_fixtures,
        unique_fixtures,
        target_databases,
        oracle,
    ):
        self.task = task
        self.project_mode = project_mode
        self.project_path = project_path
        self.container_project_dir = container_project_dir
        self.db_type = db_type
        self.environment = environment
        self.duplicate_fixtures = duplicate_fixtures
        self.unique_fixtures = unique_fixtures
        self.target_databases = target_databases
        self.oracle = oracle

    @property
    def task_dir(self) -> Path:
        return REPO_ROOT / "tasks" / self.task

    @property
    def resolved_project_path(self) -> Optional[Path]:
        if self.project_path is None:
            return None
        return resolve_repo_path(self.project_path)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "task": self.task,
            "project_mode": self.project_mode,
            "project_path": self.project_path,
            "container_project_dir": self.container_project_dir,
            "db_type": self.db_type,
            "env": dict(self.environment),
            "fixtures": {
                "duplicate": list(self.duplicate_fixtures),
                "unique": list(self.unique_fixtures),
            },
            "target_databases": list(self.target_databases),
            "oracle": self.oracle,
        }


class TaskResources:
    def __init__(self, network, doris, runner):
        self.network = network
        self.doris = doris
        self.runner = runner
        self.network_created = False
        self.doris_created = False
        self.runner_created = False


class ManifestSpec:
    def __init__(self, tasks, fixture_image, fixture_path, fixture_size, fixture_sha256):
        self.tasks = tasks
        self.fixture_image = fixture_image
        self.fixture_path = fixture_path
        self.fixture_size = fixture_size
        self.fixture_sha256 = fixture_sha256

    def fixture_as_dict(self):
        return {
            "image": self.fixture_image,
            "container_path": self.fixture_path,
            "size_bytes": self.fixture_size,
            "sha256": self.fixture_sha256,
        }


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def resolve_repo_path(value: str) -> Path:
    candidate = (REPO_ROOT / value).resolve()
    try:
        candidate.relative_to(REPO_ROOT)
    except ValueError as error:
        raise ManifestError(f"path escapes the repository: {value!r}") from error
    return candidate


def require_mapping(value: Any, location: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{location} must be a mapping")
    return value


def require_keys(
    value: Dict[str, Any], required: Set[str], location: str
) -> None:
    missing = sorted(required - value.keys())
    unknown = sorted(value.keys() - required)
    if missing:
        raise ManifestError(f"{location} is missing keys: {', '.join(missing)}")
    if unknown:
        raise ManifestError(f"{location} has unknown keys: {', '.join(unknown)}")


def require_string_list(value: Any, location: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ManifestError(f"{location} must be a list of strings")
    if len(set(value)) != len(value):
        raise ManifestError(f"{location} contains duplicate values")
    return tuple(value)


def canonical_task_names() -> Tuple[str, ...]:
    path = REPO_ROOT / "configs" / "fast-30.txt"
    if not path.is_file():
        raise ManifestError(f"canonical task list does not exist: {path}")
    return tuple(line.strip() for line in path.read_text().splitlines() if line.strip())


def parse_task(raw: Any, index: int) -> TaskSpec:
    location = f"tasks[{index}]"
    entry = require_mapping(raw, location)
    require_keys(
        entry,
        {
            "task",
            "project_mode",
            "project_path",
            "container_project_dir",
            "db_type",
            "env",
            "fixtures",
            "target_databases",
            "oracle",
        },
        location,
    )
    for field in ("task", "project_mode", "db_type", "oracle"):
        if not isinstance(entry[field], str):
            raise ManifestError(f"{location}.{field} must be a string")
    task = entry["task"]
    if not TASK_NAME.fullmatch(task):
        raise ManifestError(f"{location}.task is not a safe task name: {task!r}")
    mode = entry["project_mode"]
    if mode not in ALLOWED_PROJECT_MODES:
        raise ManifestError(
            f"{location}.project_mode must be one of {sorted(ALLOWED_PROJECT_MODES)}"
        )
    project_path = entry["project_path"]
    if project_path is not None and not isinstance(project_path, str):
        raise ManifestError(f"{location}.project_path must be a string or null")
    if mode == "self_contained" and project_path is not None:
        raise ManifestError(f"{location}.project_path must be null for self_contained")
    if mode != "self_contained" and project_path is None:
        raise ManifestError(f"{location}.project_path is required for {mode}")
    container_project_dir = entry["container_project_dir"]
    if not isinstance(container_project_dir, str):
        raise ManifestError(f"{location}.container_project_dir must be a string")
    container_project_path = PurePosixPath(container_project_dir)
    if (
        not container_project_path.is_absolute()
        or container_project_path == PurePosixPath("/app")
        or PurePosixPath("/app") not in container_project_path.parents
        or ".." in container_project_path.parts
    ):
        raise ManifestError(
            f"{location}.container_project_dir must be a child of /app"
        )
    if mode == "base" and container_project_dir != "/app/dbt_models_duckdb":
        raise ManifestError(
            f"{location}.container_project_dir must be /app/dbt_models_duckdb for base"
        )
    db_type = entry["db_type"]
    if db_type not in ALLOWED_DB_TYPES:
        raise ManifestError(
            f"{location}.db_type must be one of {sorted(ALLOWED_DB_TYPES)}"
        )
    environment = require_mapping(entry["env"], f"{location}.env")
    unknown_environment = sorted(set(environment) - ALLOWED_TASK_ENV)
    if unknown_environment:
        raise ManifestError(
            f"{location}.env contains unsupported keys: "
            + ", ".join(unknown_environment)
        )
    if any(not isinstance(value, str) for value in environment.values()):
        raise ManifestError(f"{location}.env values must be strings")
    configured_project = environment.get("DBT_PROJECT_DIR_DUCKDB")
    if configured_project is not None and configured_project != container_project_dir:
        raise ManifestError(
            f"{location}.env.DBT_PROJECT_DIR_DUCKDB must match container_project_dir"
        )
    fixtures = require_mapping(entry["fixtures"], f"{location}.fixtures")
    require_keys(fixtures, {"duplicate", "unique"}, f"{location}.fixtures")
    duplicate = require_string_list(
        fixtures["duplicate"], f"{location}.fixtures.duplicate"
    )
    unique = require_string_list(fixtures["unique"], f"{location}.fixtures.unique")
    overlap = sorted(set(duplicate) & set(unique))
    if overlap:
        raise ManifestError(
            f"{location}.fixtures lists relations as both duplicate and unique: "
            + ", ".join(overlap)
        )
    for relation in (*duplicate, *unique):
        pieces = relation.split(".", 1)
        if len(pieces) != 2 or not all(IDENTIFIER.fullmatch(piece) for piece in pieces):
            raise ManifestError(
                f"{location}.fixtures has invalid SCHEMA.TABLE relation: {relation!r}"
            )
    targets = require_string_list(
        entry["target_databases"], f"{location}.target_databases"
    )
    for database in targets:
        if not IDENTIFIER.fullmatch(database):
            raise ManifestError(
                f"{location}.target_databases has invalid identifier: {database!r}"
            )
        if database.lower() in SYSTEM_DATABASES:
            raise ManifestError(
                f"{location}.target_databases contains system database {database!r}"
            )
    if "main" not in targets:
        raise ManifestError(
            f"{location}.target_databases must include main for the verifier connection"
        )
    oracle = entry["oracle"]
    if oracle not in ALLOWED_ORACLES:
        raise ManifestError(
            f"{location}.oracle must be one of {sorted(ALLOWED_ORACLES)}"
        )
    return TaskSpec(
        task=task,
        project_mode=mode,
        project_path=project_path,
        container_project_dir=container_project_dir,
        db_type=db_type,
        environment=dict(environment),
        duplicate_fixtures=duplicate,
        unique_fixtures=unique,
        target_databases=targets,
        oracle=oracle,
    )


def validate_task_files(spec: TaskSpec) -> None:
    for relative in ("solution/solve.sh", "tests/test.sh"):
        path = spec.task_dir / relative
        if not path.is_file():
            raise ManifestError(f"{spec.task}: required file does not exist: {path}")
    project_path = spec.resolved_project_path
    if project_path is not None:
        if not project_path.is_dir():
            raise ManifestError(
                f"{spec.task}: project_path is not a directory: {project_path}"
            )
        if not (project_path / "dbt_project.yml").is_file():
            raise ManifestError(
                f"{spec.task}: project_path lacks dbt_project.yml: {project_path}"
            )
    if spec.oracle == "fifo_doris":
        oracle = SCRIPT_DIR / "oracles" / spec.task / "expected_results_doris.txt"
        if not oracle.is_file():
            raise ManifestError(f"{spec.task}: Doris oracle does not exist: {oracle}")


def load_manifest(path: Path) -> ManifestSpec:
    try:
        raw = yaml.safe_load(path.read_text())
    except FileNotFoundError as error:
        raise ManifestError(f"manifest does not exist: {path}") from error
    except yaml.YAMLError as error:
        raise ManifestError(f"cannot parse {path}: {error}") from error
    document = require_mapping(raw, "manifest")
    require_keys(document, {"version", "fixture_source", "tasks"}, "manifest")
    if document["version"] != 1:
        raise ManifestError("manifest.version must be 1")
    fixture = require_mapping(document["fixture_source"], "manifest.fixture_source")
    require_keys(
        fixture,
        {"image", "container_path", "size_bytes", "sha256"},
        "manifest.fixture_source",
    )
    fixture_image = fixture["image"]
    if not isinstance(fixture_image, str) or not fixture_image:
        raise ManifestError("manifest.fixture_source.image must be a non-empty string")
    fixture_path = fixture["container_path"]
    if not isinstance(fixture_path, str):
        raise ManifestError("manifest.fixture_source.container_path must be a string")
    fixture_parts = PurePosixPath(fixture_path)
    if not fixture_parts.is_absolute() or ".." in fixture_parts.parts:
        raise ManifestError(
            "manifest.fixture_source.container_path must be an absolute normalized path"
        )
    fixture_size = fixture["size_bytes"]
    if isinstance(fixture_size, bool) or not isinstance(fixture_size, int) or fixture_size <= 0:
        raise ManifestError("manifest.fixture_source.size_bytes must be a positive integer")
    fixture_sha256 = fixture["sha256"]
    if not isinstance(fixture_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", fixture_sha256
    ):
        raise ManifestError(
            "manifest.fixture_source.sha256 must be a lowercase SHA-256 digest"
        )
    if not isinstance(document["tasks"], list):
        raise ManifestError("manifest.tasks must be a list")
    tasks = tuple(parse_task(entry, index) for index, entry in enumerate(document["tasks"]))
    names = tuple(task.task for task in tasks)
    if len(set(names)) != len(names):
        raise ManifestError("manifest.tasks contains duplicate task names")
    canonical = canonical_task_names()
    if names != canonical:
        missing = sorted(set(canonical) - set(names))
        extra = sorted(set(names) - set(canonical))
        details = []  # type: List[str]
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        if not missing and not extra:
            details.append("task order differs from configs/fast-30.txt")
        raise ManifestError("manifest does not match fast-30: " + "; ".join(details))
    for task in tasks:
        validate_task_files(task)
    return ManifestSpec(
        tasks, fixture_image, fixture_path, fixture_size, fixture_sha256
    )


def run_capture(command: Sequence[str], *, check: bool = True) -> str:
    completed = subprocess.run(
        command,
        universal_newlines=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if check and completed.returncode != 0:
        raise CommandError(command, completed.returncode, completed.stdout)
    return completed.stdout


def repository_metadata() -> Dict[str, Any]:
    commit = run_capture(("git", "-C", str(REPO_ROOT), "rev-parse", "HEAD")).strip()
    status = run_capture(
        (
            "git",
            "-C",
            str(REPO_ROOT),
            "status",
            "--porcelain",
            "--untracked-files=all",
        )
    )
    return {
        "commit": commit,
        "dirty": bool(status.strip()),
        "status_sha256": hashlib.sha256(status.encode()).hexdigest(),
    }


def metadata_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(resolved)


def docker(arguments: Sequence[str], *, check: bool = True) -> str:
    return run_capture(("docker", *arguments), check=check)


def run_logged(command: Sequence[str], log_path: Path) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w") as log:
        log.write("$ " + " ".join(command) + "\n")
        log.flush()
        process = subprocess.Popen(
            command,
            universal_newlines=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        if process.stdout is None:
            raise RuntimeError("failed to capture command output")
        try:
            for line in process.stdout:
                sys.stdout.write(line)
                sys.stdout.flush()
                log.write(line)
                log.flush()
        except BaseException:
            process.terminate()
            process.wait()
            raise
        return process.wait()


def docker_image_metadata(image: str) -> Dict[str, Any]:
    output = docker(("image", "inspect", image))
    document = json.loads(output)
    if len(document) != 1:
        raise RuntimeError(f"docker image inspect returned {len(document)} entries")
    inspected = document[0]
    return {
        "reference": image,
        "id": inspected.get("Id"),
        "repo_digests": inspected.get("RepoDigests") or [],
        "created": inspected.get("Created"),
    }


def make_resource_names(task_name: str) -> TaskResources:
    suffix = f"{os.getpid():x}-{secrets.token_hex(4)}"
    base = f"{RESOURCE_PREFIX}-{task_name[:30]}-{suffix}"
    return TaskResources(
        network=f"{base}-net",
        doris=f"{base}-doris",
        runner=f"{base}-runner",
    )


def wait_for_doris(container: str, timeout_seconds: int = 900) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_status = "unknown"
    while time.monotonic() < deadline:
        output = docker(
            (
                "inspect",
                "--format",
                "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
                container,
            ),
            check=False,
        ).strip()
        if output:
            last_status = output
        if last_status == "healthy":
            return
        if last_status in {"exited", "dead", "unhealthy"}:
            raise RuntimeError(f"Doris container became {last_status}")
        time.sleep(5)
    raise TimeoutError(
        f"Doris container did not become healthy within {timeout_seconds}s "
        f"(last status: {last_status})"
    )


def verify_disposable_doris(resources: TaskResources) -> None:
    inspected = json.loads(docker(("inspect", resources.doris)))
    if len(inspected) != 1:
        raise RuntimeError("could not inspect the task's Doris container")
    container = inspected[0]
    labels = container.get("Config", {}).get("Labels") or {}
    networks = container.get("NetworkSettings", {}).get("Networks") or {}
    mounts = container.get("Mounts") or []
    tmpfs = container.get("HostConfig", {}).get("Tmpfs") or {}
    if labels.get(RESOURCE_LABEL) != resources.network:
        raise RuntimeError("Doris container is missing the disposable run label")
    if resources.network not in networks:
        raise RuntimeError("Doris container is not attached to its isolated network")
    if mounts:
        raise RuntimeError("Doris container unexpectedly has persistent mounts")
    expected_tmpfs = dict(DORIS_TMPFS_MOUNTS)
    if tmpfs != expected_tmpfs:
        raise RuntimeError(
            f"Doris container has unexpected tmpfs mounts: {tmpfs!r}"
        )


def run_doris_probe(
    container: str,
    sql: str,
    expected_lines: Sequence[str],
    timeout_seconds: int = 120,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
    while time.monotonic() < deadline:
        try:
            output = docker(
                (
                    "exec",
                    container,
                    "mysql",
                    "-uroot",
                    "-h127.0.0.1",
                    "-P9030",
                    "-Nse",
                    sql,
                )
            )
        except CommandError as error:
            if not any(
                marker in error.output for marker in DORIS_TRANSIENT_STARTUP_ERRORS
            ):
                raise
            last_error = error.output.strip()
            time.sleep(2)
            continue
        results = output.splitlines()
        if results != list(expected_lines):
            raise RuntimeError(f"unexpected Doris execution probe result: {results!r}")
        return
    raise TimeoutError(f"Doris execution probe did not become ready: {last_error}")


def verify_doris_execution(container: str) -> None:
    run_doris_probe(
        container,
        'SELECT SUM(number) FROM numbers("number"="10")',
        ("45",),
    )
    storage_sql = """
DROP DATABASE IF EXISTS doris_fast30_probe FORCE;
CREATE DATABASE doris_fast30_probe;
CREATE TABLE doris_fast30_probe.storage_probe (id INT)
DUPLICATE KEY(id)
DISTRIBUTED BY HASH(id) BUCKETS 1
PROPERTIES ("replication_num"="1");
INSERT INTO doris_fast30_probe.storage_probe VALUES (1);
SELECT SUM(id) FROM doris_fast30_probe.storage_probe;
DROP DATABASE doris_fast30_probe FORCE;
""".strip()
    run_doris_probe(container, storage_sql, ("1",))


def wait_for_runner(container: str, timeout_seconds: int = 60) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        status = docker(
            ("inspect", "--format", "{{.State.Status}}", container), check=False
        ).strip()
        if status in {"exited", "dead"}:
            raise RuntimeError(f"runner container became {status}")
        ready = subprocess.run(
            ("docker", "exec", container, "test", "-f", "/tmp/entrypoint_ready"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if ready.returncode == 0:
            return
        time.sleep(1)
    raise TimeoutError(f"runner container was not ready within {timeout_seconds}s")


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def update_hash_from_path(digest: Any, root: Path, label: str) -> None:
    if root.is_file():
        digest.update(label.encode())
        digest.update(root.read_bytes())
        return
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(f"{label}/{path.relative_to(root).as_posix()}".encode())
        digest.update(path.read_bytes())


def shared_input_digest() -> str:
    digest = hashlib.sha256()
    for path in SHARED_INPUT_PATHS:
        update_hash_from_path(digest, path, path.relative_to(REPO_ROOT).as_posix())
    return digest.hexdigest()


def task_input_digest(spec: TaskSpec, shared_sha256: str) -> str:
    digest = hashlib.sha256()
    digest.update(shared_sha256.encode())
    digest.update(json.dumps(spec.as_dict(), sort_keys=True).encode())
    update_hash_from_path(digest, spec.task_dir / "solution", "solution")
    update_hash_from_path(digest, spec.task_dir / "tests", "tests")
    project = spec.resolved_project_path
    if project is not None:
        update_hash_from_path(digest, project, "project")
    if spec.oracle == "fifo_doris":
        update_hash_from_path(
            digest,
            SCRIPT_DIR / "oracles" / spec.task / "expected_results_doris.txt",
            "oracle",
        )
    return digest.hexdigest()


def container_tree_metadata(container: str, root: str) -> Dict[str, Any]:
    script = """
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
if not root.exists():
    print(json.dumps({"exists": False, "file_count": 0, "sha256": None}))
    raise SystemExit(0)
digest = hashlib.sha256()
files = sorted(path for path in root.rglob("*") if path.is_file())
for path in files:
    digest.update(path.relative_to(root).as_posix().encode())
    digest.update(path.read_bytes())
print(json.dumps({"exists": True, "file_count": len(files), "sha256": digest.hexdigest()}))
""".strip()
    output = docker(
        (
            "exec",
            container,
            "/opt/dbt-doris/bin/python",
            "-c",
            script,
            root,
        )
    )
    return json.loads(output)


def execution_input_digest(
    declared_sha256: str,
    project_input: Dict[str, Any],
    image_metadata: Dict[str, Any],
) -> str:
    payload = {
        "declared_sha256": declared_sha256,
        "images": image_metadata,
        "project_input": project_input,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def parse_verifier_result(log: Path) -> Optional[Dict[str, int]]:
    matches = VERIFIER_RESULT.findall(log.read_text())
    if not matches:
        return None
    passed, skipped, failed = (int(value) for value in matches[-1])
    return {
        "passed": passed,
        "skipped": skipped,
        "failed": failed,
        "total": passed + skipped + failed,
    }


def evidence_is_complete(
    host_reward: Optional[str],
    verifier_result: Optional[Dict[str, int]],
    evidence_errors: Sequence[str],
) -> bool:
    return host_reward == "1" and verifier_result is not None and not evidence_errors


def prepare_staging(
    spec: TaskSpec, root: Path
) -> Tuple[Path, Path, Optional[Path]]:
    solution = root / "solution"
    tests = root / "tests"
    shutil.copytree(spec.task_dir / "solution", solution)
    shutil.copytree(spec.task_dir / "tests", tests)
    project = None  # type: Optional[Path]
    if spec.project_mode == "minimal":
        project = root / "project"
        source = spec.resolved_project_path
        if source is None:
            raise AssertionError("minimal project has no project_path")
        shutil.copytree(source, project)
    if spec.oracle == "fifo_doris":
        destination = tests / "private_data" / "expected_results.txt"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(
            SCRIPT_DIR / "oracles" / spec.task / "expected_results_doris.txt",
            destination,
        )
    return solution, tests, project


def start_resources(
    spec: TaskSpec,
    fixture_path: str,
    resources: TaskResources,
    solution: Path,
    tests: Path,
    project: Optional[Path],
    runner_image: str,
    doris_image: str,
) -> None:
    docker(
        (
            "network",
            "create",
            "--label",
            f"{RESOURCE_LABEL}={resources.network}",
            resources.network,
        )
    )
    resources.network_created = True
    health_command = (
        "mysql -uroot -h127.0.0.1 -P9030 -Nse 'SHOW BACKENDS' 2>/dev/null "
        "| grep -w 127.0.0.1 | grep -w 9050 | grep -w true"
    )
    doris_command = [
        "run",
        "--detach",
        "--name",
        resources.doris,
        "--network",
        resources.network,
        "--network-alias",
        "doris",
        "--label",
        f"{RESOURCE_LABEL}={resources.network}",
        "--cpus",
        "4.0",
        "--memory",
        "12g",
    ]
    for destination, settings in DORIS_TMPFS_MOUNTS:
        doris_command.extend(("--tmpfs", f"{destination}:{settings}"))
    doris_command.extend(
        (
            "--env",
            "RECOVERY=false",
            "--env",
            "BROKER=false",
            "--health-cmd",
            health_command,
            "--health-start-period",
            "60s",
            "--health-interval",
            "5s",
            "--health-timeout",
            "5s",
            "--health-retries",
            "120",
            doris_image,
        )
    )
    docker(tuple(doris_command))
    resources.doris_created = True
    verify_disposable_doris(resources)
    wait_for_doris(resources.doris)
    verify_doris_execution(resources.doris)

    command = [
        "run",
        "--detach",
        "--name",
        resources.runner,
        "--network",
        resources.network,
        "--label",
        f"{RESOURCE_LABEL}={resources.network}",
        "--cpus",
        "4.0",
        "--memory",
        "8g",
        "--env",
        f"DB_TYPE={spec.db_type}",
        "--env",
        "DORIS_HOST=doris",
        "--env",
        "DORIS_PORT=9030",
        "--env",
        "DORIS_USER=root",
        "--env",
        "DORIS_PASSWORD=",
        "--env",
        "DORIS_TARGET_DATABASE=main",
        "--env",
        f"DUCKDB_PATH={fixture_path}",
        "--env",
        f"DBT_PROJECT_DIR_DUCKDB={spec.container_project_dir}",
        "--env",
        f"DBT_PROFILES_DIR={spec.container_project_dir}",
        "--env",
        "DBT_SEND_ANONYMOUS_USAGE_STATS=false",
        "--volume",
        f"{solution.resolve()}:/solution:ro",
        "--volume",
        f"{tests.resolve()}:/tests:ro",
        "--volume",
        f"{(SCRIPT_DIR / 'dbt-wrapper.py').resolve()}:/opt/doris-fast30/dbt-wrapper.py:ro",
        "--volume",
        f"{(SCRIPT_DIR / 'duckdb.py').resolve()}:/opt/doris-fast30/duckdb.py:ro",
        "--volume",
        f"{(SCRIPT_DIR / 'load_duckdb.py').resolve()}:/opt/doris-fast30/load_duckdb.py:ro",
    ]
    for key, value in sorted(spec.environment.items()):
        command.extend(("--env", f"{key}={value}"))
    command.extend((runner_image, "sleep", "infinity"))
    docker(tuple(command))
    resources.runner_created = True
    wait_for_runner(resources.runner)
    if project is not None:
        seed_project(resources.runner, spec.container_project_dir, project)


def seed_project(container: str, destination: str, source: Path) -> None:
    destination_path = PurePosixPath(destination)
    if (
        destination_path == PurePosixPath("/app")
        or PurePosixPath("/app") not in destination_path.parents
        or ".." in destination_path.parts
    ):
        raise AssertionError(f"unsafe container project destination: {destination!r}")
    docker(("exec", container, "rm", "-rf", destination))
    docker(("exec", container, "mkdir", "-p", str(destination_path.parent)))
    docker(("cp", str(source.resolve()), f"{container}:{destination}"))


def create_target_databases(resources: TaskResources, databases: Iterable[str]) -> None:
    for database in databases:
        if not IDENTIFIER.fullmatch(database) or database.lower() in SYSTEM_DATABASES:
            raise AssertionError(f"unsafe target database reached execution: {database!r}")
        docker(
            (
                "exec",
                resources.doris,
                "mysql",
                "-uroot",
                "-h127.0.0.1",
                "-P9030",
                "-e",
                f"CREATE DATABASE IF NOT EXISTS `{database}`",
            )
        )


def load_fixtures(
    resources: TaskResources,
    fixture_path: str,
    relations: Sequence[str],
    unique: bool,
    log: Path,
) -> int:
    if not relations:
        log.write_text("No fixtures in this class.\n")
        return 0
    command = [
        "docker",
        "exec",
        resources.runner,
        "/opt/dbt-doris/bin/python",
        "/opt/doris-fast30/load_duckdb.py",
        "--duckdb-path",
        fixture_path,
    ]
    if unique:
        command.append("--unique-key")
    command.extend(relations)
    return run_logged(command, log)


def container_path_exists(container: str, path: str) -> bool:
    completed = subprocess.run(
        ("docker", "exec", container, "test", "-e", path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def artifact_project_paths(spec: TaskSpec) -> Tuple[str, ...]:
    return tuple(dict.fromkeys((spec.container_project_dir, *ARTIFACT_PROJECTS)))


def copy_dbt_artifacts(spec: TaskSpec, container: str, output_dir: Path) -> None:
    for project_path in artifact_project_paths(spec):
        label = project_path[len("/app/") :].replace("/", "-")
        project_output = output_dir / label
        for relative in ("target", "logs"):
            source = f"{project_path}/{relative}"
            if container_path_exists(container, source):
                project_output.mkdir(parents=True, exist_ok=True)
                docker(("cp", f"{container}:{source}", str(project_output)))
        for filename in ("dbt_project.yml", "profiles.yml"):
            source = f"{project_path}/{filename}"
            if container_path_exists(container, source):
                project_output.mkdir(parents=True, exist_ok=True)
                docker(("cp", f"{container}:{source}", str(project_output / filename)))


def preserve_failed_projects(
    spec: TaskSpec, container: str, destination: Path
) -> None:
    for project_path in artifact_project_paths(spec):
        if not container_path_exists(container, project_path):
            continue
        label = project_path[len("/app/") :].replace("/", "-")
        destination.mkdir(parents=True, exist_ok=True)
        docker(("cp", f"{container}:{project_path}", str(destination / label)))


def collect_container_evidence(resources: TaskResources, output_dir: Path) -> None:
    if resources.doris_created:
        (output_dir / "doris.log").write_text(
            docker(("logs", resources.doris), check=False)
        )
    if resources.runner_created:
        (output_dir / "runner.log").write_text(
            docker(("logs", resources.runner), check=False)
        )
    names = []
    if resources.doris_created:
        names.append(resources.doris)
    if resources.runner_created:
        names.append(resources.runner)
    if names:
        output = docker(("inspect", *names), check=False)
        if output.strip():
            try:
                write_json(output_dir / "containers.json", json.loads(output))
            except json.JSONDecodeError:
                (output_dir / "containers.inspect.txt").write_text(output)


def docker_object_exists(kind: str, name: str) -> bool:
    completed = subprocess.run(
        ("docker", kind, "inspect", name),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode == 0


def cleanup_resources(resources: TaskResources, cleanup_log: Path) -> bool:
    messages = []  # type: List[str]
    clean = True
    for created, name in (
        (resources.runner_created, resources.runner),
        (resources.doris_created, resources.doris),
    ):
        if not created:
            continue
        output = docker(("rm", "--force", name), check=False)
        messages.append(f"docker rm --force {name}\n{output}")
        if docker_object_exists("container", name):
            clean = False
            messages.append(f"ERROR: container still exists: {name}")
    if resources.network_created:
        output = docker(("network", "rm", resources.network), check=False)
        messages.append(f"docker network rm {resources.network}\n{output}")
        if docker_object_exists("network", resources.network):
            clean = False
            messages.append(f"ERROR: network still exists: {resources.network}")
    cleanup_log.write_text("\n".join(messages))
    return clean


def runtime_versions(
    resources: TaskResources, fixture_path: str
) -> Dict[str, str]:
    commands = {
        "python": (
            "exec",
            resources.runner,
            "/opt/dbt-doris/bin/python",
            "--version",
        ),
        "dbt": (
            "exec",
            resources.runner,
            "/opt/dbt-doris/bin/dbt",
            "--version",
        ),
        "fixture_sha256": (
            "exec",
            resources.runner,
            "sha256sum",
            fixture_path,
        ),
        "fixture_size_bytes": (
            "exec",
            resources.runner,
            "stat",
            "-c",
            "%s",
            fixture_path,
        ),
        "doris_frontends": (
            "exec",
            resources.doris,
            "mysql",
            "-uroot",
            "-h127.0.0.1",
            "-P9030",
            "-Nse",
            "SHOW FRONTENDS",
        ),
        "doris_backends": (
            "exec",
            resources.doris,
            "mysql",
            "-uroot",
            "-h127.0.0.1",
            "-P9030",
            "-Nse",
            "SHOW BACKENDS",
        ),
        "mysql_protocol_version": (
            "exec",
            resources.doris,
            "mysql",
            "-uroot",
            "-h127.0.0.1",
            "-P9030",
            "-Nse",
            "SELECT VERSION()",
        ),
    }
    return {name: docker(command, check=False).strip() for name, command in commands.items()}


def verify_fixture_source(manifest: ManifestSpec, versions: Dict[str, str]) -> None:
    digest_output = versions["fixture_sha256"].split()
    actual_digest = digest_output[0] if digest_output else ""
    try:
        actual_size = int(versions["fixture_size_bytes"])
    except ValueError as error:
        raise RuntimeError("could not read fixture size from runner image") from error
    if actual_digest != manifest.fixture_sha256:
        raise RuntimeError(
            "fixture SHA-256 mismatch: expected {}, got {}".format(
                manifest.fixture_sha256, actual_digest or "<missing>"
            )
        )
    if actual_size != manifest.fixture_size:
        raise RuntimeError(
            "fixture size mismatch: expected {}, got {}".format(
                manifest.fixture_size, actual_size
            )
        )


def read_reward(logs: Path) -> Optional[str]:
    reward = logs / "verifier" / "reward.txt"
    if not reward.is_file():
        return None
    return reward.read_text().strip()


def read_container_reward(container: str) -> Optional[str]:
    output = docker(
        ("exec", container, "cat", "/logs/verifier/reward.txt"), check=False
    ).strip()
    return output if output in {"0", "1"} else None


def copy_verifier_logs(container: str, logs: Path) -> None:
    if container_path_exists(container, "/logs/verifier"):
        docker(("cp", f"{container}:/logs/verifier", str(logs)))


def run_task(
    spec: TaskSpec,
    manifest: ManifestSpec,
    output_dir: Path,
    runner_image: str,
    doris_image: str,
    image_metadata: Dict[str, Any],
    shared_sha256: str,
    keep_on_failure: bool,
) -> bool:
    task_output = output_dir / spec.task
    if task_output.exists():
        raise RuntimeError(f"refusing to overwrite task output: {task_output}")
    task_output.mkdir(parents=True)
    logs = task_output / "logs"
    logs.mkdir()
    resources = make_resource_names(spec.task)
    started_monotonic = time.monotonic()
    declared_input_sha256 = task_input_digest(spec, shared_sha256)
    metadata = {  # type: Dict[str, Any]
        "task": spec.task,
        "started_at": utc_now(),
        "status": "running",
        "declared_input_sha256": declared_input_sha256,
        "input_sha256": None,
        "manifest_entry": spec.as_dict(),
        "fixture_source": manifest.fixture_as_dict(),
        "images": image_metadata,
        "resources": {
            "network": resources.network,
            "doris": resources.doris,
            "runner": resources.runner,
        },
        "stages": {},
    }
    write_json(task_output / "runtime.json", metadata)
    write_json(task_output / "task.json", spec.as_dict())
    success = False
    failure = None  # type: Optional[str]
    staging_root = Path(tempfile.mkdtemp(prefix=f"{RESOURCE_PREFIX}-{spec.task}-"))
    try:
        solution, tests, project = prepare_staging(spec, staging_root)
        start_resources(
            spec,
            manifest.fixture_path,
            resources,
            solution,
            tests,
            project,
            runner_image,
            doris_image,
        )
        project_input = container_tree_metadata(
            resources.runner, spec.container_project_dir
        )
        metadata["project_input"] = project_input
        metadata["input_sha256"] = execution_input_digest(
            declared_input_sha256,
            project_input,
            image_metadata,
        )
        metadata["versions"] = runtime_versions(resources, manifest.fixture_path)
        verify_fixture_source(manifest, metadata["versions"])
        create_target_databases(resources, spec.target_databases)
        duplicate_exit = load_fixtures(
            resources,
            manifest.fixture_path,
            spec.duplicate_fixtures,
            False,
            logs / "load-duplicate.log",
        )
        metadata["stages"]["load_duplicate"] = duplicate_exit
        unique_exit = load_fixtures(
            resources,
            manifest.fixture_path,
            spec.unique_fixtures,
            True,
            logs / "load-unique.log",
        )
        metadata["stages"]["load_unique"] = unique_exit
        if duplicate_exit != 0 or unique_exit != 0:
            raise RuntimeError("fixture loading failed")

        print(f"[{spec.task}] running canonical solution", flush=True)
        solve_exit = run_logged(
            ("docker", "exec", resources.runner, "/bin/bash", "/solution/solve.sh"),
            logs / "solve.log",
        )
        metadata["stages"]["solve"] = solve_exit
        print(f"[{spec.task}] running canonical verifier", flush=True)
        verify_exit = run_logged(
            ("docker", "exec", resources.runner, "/bin/bash", "/tests/test.sh"),
            logs / "verify.log",
        )
        metadata["stages"]["verify"] = verify_exit
        verifier_result = parse_verifier_result(logs / "verify.log")
        metadata["verifier"] = verifier_result
        reward = read_container_reward(resources.runner)
        metadata["reward"] = reward
        success = (
            solve_exit == 0
            and verify_exit == 0
            and reward == "1"
            and verifier_result is not None
        )
        if not success:
            failure = (
                f"solve={solve_exit}, verify={verify_exit}, reward={reward!r}, "
                f"verifier={verifier_result!r}"
            )
    except (CommandError, OSError, RuntimeError, TimeoutError) as error:
        failure = str(error)
    finally:
        evidence_errors = []  # type: List[str]
        if resources.runner_created:
            try:
                copy_verifier_logs(resources.runner, logs)
                copy_dbt_artifacts(spec, resources.runner, task_output / "artifacts")
            except (CommandError, OSError) as error:
                evidence_errors.append(str(error))
            if not success and keep_on_failure:
                try:
                    preserve_failed_projects(
                        spec,
                        resources.runner,
                        task_output / "failed-container-projects",
                    )
                except (CommandError, OSError) as error:
                    metadata["failed_project_error"] = str(error)
        try:
            collect_container_evidence(resources, task_output)
        except (CommandError, OSError) as error:
            evidence_errors.append(str(error))
        host_reward = read_reward(logs)
        verifier_result = metadata.get("verifier")
        if not evidence_is_complete(host_reward, verifier_result, evidence_errors):
            success = False
            failure = (
                f"{failure + '; ' if failure else ''}incomplete evidence: "
                f"host_reward={host_reward!r}, verifier={verifier_result!r}, "
                f"errors={evidence_errors!r}"
            )
        metadata["evidence_errors"] = evidence_errors
        try:
            cleanup_ok = cleanup_resources(resources, task_output / "cleanup.log")
        except (CommandError, OSError) as error:
            cleanup_ok = False
            (task_output / "cleanup.log").write_text(str(error) + "\n")
        if not cleanup_ok:
            success = False
            cleanup_failure = "one or more task-scoped Docker resources could not be removed"
            failure = f"{failure + '; ' if failure else ''}{cleanup_failure}"
        if not success and keep_on_failure:
            shutil.copytree(staging_root, task_output / "staging")
        try:
            shutil.rmtree(staging_root)
        except OSError as error:
            success = False
            failure = f"{failure + '; ' if failure else ''}staging cleanup failed: {error}"

        reward_path = logs / "verifier" / "reward.txt"
        if not reward_path.is_file():
            reward_path.parent.mkdir(parents=True, exist_ok=True)
            reward_path.write_text("0\n")
        metadata["reward"] = read_reward(logs)
        if success and metadata["reward"] != "1":
            raise AssertionError("passed task must preserve reward 1 on the host")
        metadata["status"] = "passed" if success else "failed"
        metadata["failure"] = failure
        metadata["finished_at"] = utc_now()
        metadata["duration_seconds"] = round(
            time.monotonic() - started_monotonic, 3
        )
        write_json(task_output / "runtime.json", metadata)
    print(
        f"[{spec.task}] {'PASS' if success else 'FAIL'} "
        f"(reward={metadata['reward']})",
        flush=True,
    )
    return success


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run canonical fast-30 solutions against isolated Doris containers."
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--task",
        action="append",
        default=[],
        metavar="NAME",
        help="run one task; repeat to select more than one (default: all)",
    )
    parser.add_argument("--list", action="store_true", help="list manifest tasks and exit")
    parser.add_argument(
        "--validate-only",
        "--dry-run",
        dest="validate_only",
        action="store_true",
        help="validate files and selections without changing Docker state",
    )
    parser.add_argument(
        "--keep-on-failure",
        action="store_true",
        help="preserve failed host staging and project copies; Docker resources are still removed",
    )
    parser.add_argument(
        "--require-clean",
        action="store_true",
        help="refuse to run unless the repository worktree is clean",
    )
    parser.add_argument("--runner-image", default=DEFAULT_RUNNER_IMAGE)
    parser.add_argument("--doris-image", default=DEFAULT_DORIS_IMAGE)
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="result directory (default: experiments/doris-fast30/runs/<timestamp>)",
    )
    return parser


def aggregate_verifier_results(
    output_dir: Path, results: Dict[str, bool]
) -> Dict[str, int]:
    aggregate = {
        "tasks_with_counts": 0,
        "passed": 0,
        "skipped": 0,
        "failed": 0,
        "total": 0,
    }
    for task_name in results:
        runtime_path = output_dir / task_name / "runtime.json"
        if not runtime_path.is_file():
            continue
        runtime = json.loads(runtime_path.read_text())
        verifier = runtime.get("verifier")
        if not isinstance(verifier, dict):
            continue
        aggregate["tasks_with_counts"] += 1
        for key in ("passed", "skipped", "failed", "total"):
            aggregate[key] += int(verifier[key])
    return aggregate


def select_tasks(
    tasks: Tuple[TaskSpec, ...], names: Sequence[str]
) -> Tuple[TaskSpec, ...]:
    if not names:
        return tasks
    if len(set(names)) != len(names):
        raise ManifestError("--task contains duplicate selections")
    by_name = {task.task: task for task in tasks}
    unknown = [name for name in names if name not in by_name]
    if unknown:
        raise ManifestError("unknown --task selection: " + ", ".join(unknown))
    selected = set(names)
    return tuple(task for task in tasks if task.task in selected)


def default_output_dir() -> Path:
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return SCRIPT_DIR / "runs" / timestamp


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest.resolve())
        tasks = manifest.tasks
        selected = select_tasks(tasks, args.task)
    except ManifestError as error:
        parser.error(str(error))
    if args.list:
        for task in tasks:
            print(task.task)
        return 0
    if args.validate_only:
        print(f"Validated {len(tasks)} manifest tasks; selected {len(selected)} task(s).")
        for task in selected:
            print(
                f"{task.task}: {task.project_mode}, {task.db_type}, "
                f"{len(task.duplicate_fixtures)} duplicate + "
                f"{len(task.unique_fixtures)} unique fixture(s), {task.oracle} oracle"
            )
        return 0

    try:
        repository = repository_metadata()
    except CommandError as error:
        parser.error(str(error))
    if args.require_clean and repository["dirty"]:
        parser.error("--require-clean needs a clean git worktree")
    shared_sha256 = shared_input_digest()

    try:
        docker(("version", "--format", "{{.Server.Version}}"))
        images = {
            "runner": docker_image_metadata(args.runner_image),
            "doris": docker_image_metadata(args.doris_image),
        }
    except (CommandError, json.JSONDecodeError) as error:
        parser.error(str(error))

    output_dir = (args.output_dir or default_output_dir()).resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        parser.error(f"output directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    run_metadata = {
        "started_at": utc_now(),
        "manifest": metadata_path(args.manifest),
        "manifest_sha256": hashlib.sha256(args.manifest.read_bytes()).hexdigest(),
        "shared_input_sha256": shared_sha256,
        "fixture_source": manifest.fixture_as_dict(),
        "selected_tasks": [task.task for task in selected],
        "images": images,
        "repository": repository,
    }
    write_json(output_dir / "run.json", run_metadata)

    results = {}  # type: Dict[str, bool]
    interrupted = False
    try:
        for index, task in enumerate(selected, start=1):
            print(f"\n[{index}/{len(selected)}] {task.task}", flush=True)
            results[task.task] = run_task(
                task,
                manifest,
                output_dir,
                args.runner_image,
                args.doris_image,
                images,
                shared_sha256,
                args.keep_on_failure,
            )
    except KeyboardInterrupt:
        interrupted = True
        print("\nInterrupted; current task resources were cleaned up.", file=sys.stderr)
    run_metadata["finished_at"] = utc_now()
    run_metadata["results"] = results
    run_metadata["passed"] = sum(results.values())
    run_metadata["failed"] = len(results) - sum(results.values())
    run_metadata["interrupted"] = interrupted
    run_metadata["verifier"] = aggregate_verifier_results(output_dir, results)
    write_json(output_dir / "run.json", run_metadata)
    print(
        f"Results: {run_metadata['passed']} passed, {run_metadata['failed']} failed; "
        f"artifacts: {output_dir}",
        flush=True,
    )
    return 130 if interrupted else (0 if all(results.values()) else 1)


if __name__ == "__main__":
    raise SystemExit(main())
