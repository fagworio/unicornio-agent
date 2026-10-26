#!/usr/bin/env python3
"""Provision, exercise, report, and destroy an isolated real WordPress E2E."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
COMPOSE_FILE = ROOT / "tests/wordpress/e2e/compose.yaml"
FAULT_PLUGIN = ROOT / "tests/wordpress/e2e/mu-plugins/unicornio-e2e-faults.php"
MU_PLUGINS = (
    ROOT / "wp/mu-plugins/unicornio-rest-meta.php",
    ROOT / "wp/mu-plugins/unicornio-status-awaiting-human.php",
    FAULT_PLUGIN,
)
SCENARIOS = (
    "test_pending_transitions_to_awaiting_human_with_rest_readback",
    "test_existing_awaiting_human_without_changes_avoids_post",
    "test_awaiting_human_metadata_divergence_is_written_and_confirmed",
    "test_writer_is_idempotent_across_repeated_real_rest_calls",
    "test_controlled_status_post_failure_is_recorded_without_false_status_change",
    "test_interruption_after_status_post_before_readback_is_uncertain",
    "test_divergent_status_readback_does_not_confirm_writer",
    "test_historical_journal_without_writer_started_remains_compatible",
    "test_media_registry_v5_matches_confirmed_writer_transaction",
    "test_legacy_prewriter_isolation_uses_real_wordpress_readbacks_without_writes",
)
SCENARIO_OPERATIONS = {
    "pending_transitions_to_awaiting_human_with_rest_readback": ["pending_transition"],
    "existing_awaiting_human_without_changes_avoids_post": ["noop"],
    "awaiting_human_metadata_divergence_is_written_and_confirmed": ["metadata_divergence"],
    "writer_is_idempotent_across_repeated_real_rest_calls": ["idempotency"],
    "controlled_status_post_failure_is_recorded_without_false_status_change": ["post_failure"],
    "interruption_after_status_post_before_readback_is_uncertain": ["interrupt"],
    "divergent_status_readback_does_not_confirm_writer": ["divergent"],
    "historical_journal_without_writer_started_remains_compatible": ["historical_journal_compatibility"],
    "media_registry_v5_matches_confirmed_writer_transaction": ["registry"],
    "legacy_prewriter_isolation_uses_real_wordpress_readbacks_without_writes": ["prewriter_isolation"],
}
POST_FIXTURES = {
    "pending_transition": ("pending", False),
    "noop": ("awaiting_human", True),
    "metadata_divergence": ("awaiting_human", False),
    "idempotency": ("pending", False),
    "post_failure": ("pending", False),
    "interrupt": ("pending", False),
    "divergent": ("pending", False),
    "historical": ("awaiting_human", False),
    "registry": ("pending", False),
    "isolation_target": ("pending", True),
    "isolation_committed_a": ("pending", True),
    "isolation_committed_b": ("pending", True),
    "isolation_not_executed_a": ("pending", True),
    "isolation_not_executed_b": ("pending", True),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def command(args: list[str], *, env=None, input_text=None, timeout=180, check=True):
    result = subprocess.run(
        args,
        cwd=ROOT,
        env=env,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode:
        safe = result.stdout
        for key in ("WP_E2E_DB_PASSWORD", "WP_E2E_DB_ROOT_PASSWORD", "WP_E2E_APP_PASSWORD"):
            value = (env or os.environ).get(key, "")
            if value:
                safe = safe.replace(value, "[REDACTED]")
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(args[:4])}\n{safe[-6000:]}")
    return result


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def compose_env(project: str, port: int) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "WP_E2E_DB_NAME": "wordpress_e2e",
        "WP_E2E_DB_USER": "wp_e2e",
        "WP_E2E_DB_PASSWORD": secrets.token_urlsafe(28),
        "WP_E2E_DB_ROOT_PASSWORD": secrets.token_urlsafe(28),
        "WP_E2E_PORT": str(port),
    })
    env["COMPOSE_PROJECT_NAME"] = project
    return env


def compose_args(project: str, *args: str) -> list[str]:
    return ["docker", "compose", "--project-name", project, "--file", str(COMPOSE_FILE), *args]


def wp_cli(project: str, env: dict[str, str], *args: str, input_text=None) -> str:
    result = command(
        compose_args(project, "--profile", "tools", "exec", "-T", "wpcli", "wp", "--path=/var/www/html", *args),
        env=env,
        input_text=input_text,
        timeout=120,
    )
    return result.stdout.strip()


def copy_mu_plugins(project: str, env: dict[str, str]) -> None:
    command(compose_args(project, "exec", "-T", "wordpress", "mkdir", "-p", "/var/www/html/wp-content/mu-plugins"), env=env)
    for source in MU_PLUGINS:
        command(compose_args(project, "cp", str(source), f"wordpress:/var/www/html/wp-content/mu-plugins/{source.name}"), env=env)
        command(compose_args(project, "exec", "-T", "wordpress", "chmod", "0644", f"/var/www/html/wp-content/mu-plugins/{source.name}"), env=env)
        command(compose_args(project, "exec", "-T", "wordpress", "php", "-l", f"/var/www/html/wp-content/mu-plugins/{source.name}"), env=env)


def image_digests(project: str, env: dict[str, str]) -> dict[str, list[str]]:
    result = {}
    for image in ("wordpress:7.1.3-php8.3-apache", "wordpress:cli-2.12.0-php8.3", "mysql:8.4"):
        inspect = command(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image], env=env, check=False)
        result[image] = json.loads(inspect.stdout.strip()) if inspect.returncode == 0 and inspect.stdout.strip() else []
    return result


def parse_junit(path: Path) -> dict:
    if not path.is_file():
        return {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "cases": []}
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall(".//testsuite"))
    totals = {key: 0 for key in ("tests", "failures", "errors", "skipped")}
    cases = []
    for suite in suites:
        for key in totals:
            totals[key] += int(suite.attrib.get(key, "0"))
        for case in suite.findall("testcase"):
            status = "passed"
            diagnostic = ""
            failure = case.find("failure")
            error = case.find("error")
            if failure is not None:
                status = "failed"
                diagnostic = str(failure.attrib.get("message") or "").splitlines()[0][:240]
            elif error is not None:
                status = "error"
                diagnostic = str(error.attrib.get("message") or "").splitlines()[0][:240]
            elif case.find("skipped") is not None:
                status = "skipped"
            cases.append({
                "name": case.attrib.get("name", ""),
                "classname": case.attrib.get("classname", ""),
                "status": status,
                "diagnostic": diagnostic,
            })
    return totals | {"cases": cases}


def scenario_results(e2e_junit: dict, observations: dict) -> list[dict]:
    cases_by_name = {item["name"]: item["status"] for item in e2e_junit["cases"]}
    observations_list = observations.get("observations", [])
    by_scenario: dict[str, list[dict]] = {}
    for item in observations_list:
        by_scenario.setdefault(str(item.get("scenario") or ""), []).append(item)
    results = []
    for nodeid in SCENARIOS:
        name = nodeid.removeprefix("test_")
        matching = next((status for case, status in cases_by_name.items() if case == nodeid or case.startswith(nodeid + "[")), "missing")
        results.append({
            "scenario": name,
            "result": matching,
            "observations": [
                item
                for operation in SCENARIO_OPERATIONS[name]
                for item in by_scenario.get(operation, [])
            ],
        })
    return results


def main() -> int:
    report_path = Path(os.environ.get("WP_E2E_REPORT_PATH", "artifacts/wordpress-e2e-report.json"))
    report_path = report_path if report_path.is_absolute() else ROOT / report_path
    report_path.parent.mkdir(parents=True, exist_ok=True)
    scratch = Path(tempfile.mkdtemp(prefix="unicornio-wp-e2e-"))
    e2e_junit_path = scratch / "wordpress-e2e-junit.xml"
    suite_junit_path = scratch / "full-suite-junit.xml"
    observations_path = scratch / "wordpress-e2e-observations.json"
    project = f"unicornio-wp-e2e-{os.environ.get('GITHUB_RUN_ID', 'local')}-{os.environ.get('GITHUB_RUN_ATTEMPT', '1')}-{uuid.uuid4().hex[:8]}".lower()
    port = free_port()
    env = compose_env(project, port)
    full_sha = command(["git", "rev-parse", "HEAD"]).stdout.strip()
    tree_sha = command(["git", "rev-parse", "HEAD^{tree}"]).stdout.strip()
    working_tree_changes = command(["git", "status", "--porcelain", "--untracked-files=all"]).stdout.strip()
    expected_sha = os.environ.get("WP_E2E_EXPECTED_SHA") or os.environ.get("GITHUB_SHA") or full_sha
    report = {
        "classification": "E2E_NAO_HOMOLOGADO",
        "generated_at": utc_now(),
        "repository": os.environ.get("GITHUB_REPOSITORY", "local"),
        "workflow": os.environ.get("GITHUB_WORKFLOW", "local"),
        "workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF", ""),
        "run_id": os.environ.get("GITHUB_RUN_ID", "local"),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
        "tested_sha": full_sha,
        "expected_sha": expected_sha,
        "tree_sha": tree_sha,
        "working_tree_clean": not bool(working_tree_changes),
        "working_tree_change_count": len([line for line in working_tree_changes.splitlines() if line.strip()]),
        "wordpress": {},
        "php": {},
        "wp_cli": {},
        "images": {},
        "scenarios": [],
        "e2e_suite": {},
        "full_suite": {},
        "environment": {"ephemeral": True, "ephemeral_environment_destroyed": False},
        "errors": [],
    }
    setup_ok = False
    e2e_status = 1
    suite_status = 1
    try:
        command(compose_args(project, "--profile", "tools", "pull"), env=env, timeout=600)
        command(compose_args(project, "up", "-d", "--wait", "--wait-timeout", "180"), env=env, timeout=240)
        command(compose_args(project, "--profile", "tools", "up", "-d", "wpcli"), env=env, timeout=120)
        copy_mu_plugins(project, env)
        db_admin_password = secrets.token_urlsafe(30)
        admin_input = db_admin_password + "\n"
        wp_cli(
            project, env, "core", "install",
            f"--url=http://127.0.0.1:{port}",
            "--title=Unicornio Agent E2E",
            "--admin_user=e2e-admin",
            "--admin_email=e2e-admin@example.invalid",
            "--skip-email",
            "--prompt=admin_password",
            input_text=admin_input,
        )
        # The REST client deliberately uses the canonical /wp-json/ route.
        # A fresh WordPress install may default to plain permalinks, so set
        # a real rewrite structure before exercising the production client.
        wp_cli(project, env, "rewrite", "structure", "/%postname%/", "--hard")
        with urlopen(f"http://127.0.0.1:{port}/wp-json/", timeout=20) as response:
            rest_root = json.loads(response.read().decode("utf-8"))
            if response.status != 200 or not isinstance(rest_root, dict):
                raise RuntimeError("ephemeral WordPress REST root did not return JSON")
        app_password = wp_cli(
            project, env, "user", "application-password", "create",
            "e2e-admin", "GitHub Actions E2E", "--porcelain",
        ).splitlines()[-1].strip()
        if not app_password:
            raise RuntimeError("WP-CLI did not return an ephemeral Application Password")

        ids = {}
        from unicornio_editor.pipeline_v2.model import (
            BlockerCode, LifecycleState, MediaProgress, Phase, WorkState,
        )
        state = WorkState(
            state=LifecycleState.HUMAN_REQUIRED,
            phase=Phase.MEDIA,
            blocker=BlockerCode.FEATURED_MISSING,
            relevance_approved=True,
            media=MediaProgress(required=0),
        )
        state_json = json.dumps(state.to_dict(), ensure_ascii=False, separators=(",", ":"))
        for key, (status, exact_state) in POST_FIXTURES.items():
            post_id = wp_cli(
                project, env, "post", "create",
                "--post_type=post", f"--post_status={status}",
                f"--post_title=E2E {key}", "--post_content=Ephemeral integration fixture.",
                "--porcelain",
            ).splitlines()[-1].strip()
            ids[key] = int(post_id)
            if exact_state:
                wp_cli(project, env, "post", "meta", "update", post_id, "_hermes_work_state", state_json)
            if key.startswith("isolation_"):
                wp_cli(
                    project, env, "post", "meta", "update", post_id,
                    "_hermes_human_reopened_at", "2026-10-25T00:00:00Z",
                )
                wp_cli(
                    project, env, "post", "meta", "update", post_id,
                    "_hermes_human_reopened_from", "awaiting_human",
                )
        app_env = env.copy()
        app_env.update({
            "PYTHONPATH": str(ROOT / "src"),
            "WP_E2E_ENABLED": "1",
            "WP_E2E_URL": f"http://127.0.0.1:{port}",
            "WP_E2E_USER": "e2e-admin",
            "WP_E2E_APP_PASSWORD": app_password,
            "WP_E2E_POST_IDS": json.dumps(ids, sort_keys=True),
            "WP_E2E_OBSERVATIONS_PATH": str(observations_path),
        })
        e2e_run = command(
            [sys.executable, "-m", "pytest", "-q", "tests/wordpress/e2e/test_writer.py", f"--junitxml={e2e_junit_path}"],
            env=app_env,
            timeout=600,
            check=False,
        )
        e2e_status = e2e_run.returncode
        report["e2e_suite"] = parse_junit(e2e_junit_path)
        report["e2e_suite"]["exit_code"] = e2e_status
        observations = {}
        if observations_path.is_file():
            observations = json.loads(observations_path.read_text(encoding="utf-8"))
        report["scenarios"] = scenario_results(report["e2e_suite"], observations)
        report["wordpress"] = {
            "version": wp_cli(project, env, "core", "version"),
            "url_scope": "127.0.0.1 ephemeral container only",
        }
        report["php"] = {
            "version": command(compose_args(project, "exec", "-T", "wordpress", "php", "-r", "echo PHP_VERSION;"), env=env).stdout.strip(),
        }
        report["wp_cli"] = {"version": wp_cli(project, env, "--version")}
        report["images"] = image_digests(project, env)
        report["environment"]["posts_created"] = len(ids)
        report["environment"]["wordpress_state_snapshots"] = sum(
            int(bool(snapshot.get("before"))) + int(bool(snapshot.get("after")))
            for snapshot in observations.get("observations", [])
        )
        report["environment"]["ephemeral_environment_destroyed"] = True
        setup_ok = True
    except Exception as exc:
        report["errors"].append({"stage": "e2e_setup_or_execution", "type": type(exc).__name__, "detail": str(exc)[-6000:]})
    finally:
        down = command(compose_args(project, "--profile", "tools", "down", "--volumes", "--remove-orphans"), env=env, timeout=180, check=False)
        containers = command(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"], env=env, check=False)
        if containers.stdout.strip():
            command(["docker", "rm", "-f", *containers.stdout.split()], env=env, check=False)
        volumes = command(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"], env=env, check=False)
        if volumes.stdout.strip():
            command(["docker", "volume", "rm", *volumes.stdout.split()], env=env, check=False)
        networks = command(["docker", "network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"], env=env, check=False)
        if networks.stdout.strip():
            command(["docker", "network", "rm", *networks.stdout.split()], env=env, check=False)
        containers = command(["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={project}"], env=env, check=False)
        volumes = command(["docker", "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"], env=env, check=False)
        networks = command(["docker", "network", "ls", "-q", "--filter", f"label=com.docker.compose.project={project}"], env=env, check=False)
        cleanup_ok = (
            down.returncode == 0
            and not containers.stdout.strip()
            and not volumes.stdout.strip()
            and not networks.stdout.strip()
        )
        report["environment"]["ephemeral_environment_destroyed"] = cleanup_ok
        report["environment"]["remaining_containers"] = [line for line in containers.stdout.splitlines() if line.strip()]
        report["environment"]["remaining_volumes"] = [line for line in volumes.stdout.splitlines() if line.strip()]
        report["environment"]["remaining_networks"] = [line for line in networks.stdout.splitlines() if line.strip()]
        if not cleanup_ok:
            report["errors"].append({"stage": "ephemeral_cleanup", "type": "CleanupError", "detail": down.stdout[-3000:]})
        suite_env = os.environ.copy()
        suite_env["PYTHONPATH"] = str(ROOT / "src")
        suite_env["WP_E2E_ENABLED"] = "0"
        suite_env.pop("WP_E2E_URL", None)
        suite_env.pop("WP_E2E_USER", None)
        suite_env.pop("WP_E2E_APP_PASSWORD", None)
        suite_run = command(
            [sys.executable, "-m", "pytest", "-q", f"--junitxml={suite_junit_path}"],
            env=suite_env,
            timeout=1200,
            check=False,
        )
        suite_status = suite_run.returncode
        report["full_suite"] = parse_junit(suite_junit_path)
        report["full_suite"]["exit_code"] = suite_status
        report["environment"]["local_checkout"] = True
        report["environment"]["tested_expected_sha_match"] = full_sha == expected_sha
        report["e2e_observations"] = {}
        if observations_path.is_file():
            try:
                report["e2e_observations"] = json.loads(observations_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                report["errors"].append({"stage": "observation_parse", "type": "InvalidObservations"})
        try:
            report["build"] = {
                "python_compileall_exit_code": command([sys.executable, "-m", "compileall", "-q", "src", "tests", "scripts"], env=suite_env, check=False).returncode,
            }
        except Exception:
            report["build"] = {"python_compileall_exit_code": 1}
        all_scenarios = (
            len(report.get("e2e_suite", {}).get("cases", [])) == len(SCENARIOS)
            and report.get("e2e_suite", {}).get("failures") == 0
            and report.get("e2e_suite", {}).get("errors") == 0
            and report.get("e2e_suite", {}).get("skipped") == 0
            and len(report.get("scenarios", [])) == len(SCENARIOS)
            and all(item.get("result") == "passed" for item in report.get("scenarios", []))
        )
        suite_ok = (
            suite_status == 0
            and report.get("full_suite", {}).get("failures") == 0
            and report.get("full_suite", {}).get("errors") == 0
        )
        if (
            setup_ok and e2e_status == 0 and all_scenarios and suite_ok
            and cleanup_ok and full_sha == expected_sha
            and not bool(working_tree_changes)
            and report.get("build", {}).get("python_compileall_exit_code") == 0
        ):
            report["classification"] = "E2E_HOMOLOGADO"
        report["completed_at"] = utc_now()
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        shutil.rmtree(scratch)

    print(json.dumps({
        "classification": report["classification"],
        "report_path": str(report_path),
        "tested_sha": full_sha,
        "tree_sha": tree_sha,
        "e2e_exit_code": e2e_status,
        "full_suite_exit_code": suite_status,
        "cleanup": report["environment"]["ephemeral_environment_destroyed"],
    }, ensure_ascii=False))
    return 0 if report["classification"] == "E2E_HOMOLOGADO" else 1


if __name__ == "__main__":
    raise SystemExit(main())
