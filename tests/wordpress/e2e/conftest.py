from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


OBSERVATIONS: list[dict] = []
sys.path.insert(0, str(Path(__file__).resolve().parent))


def pytest_collection_modifyitems(config, items):
    if os.environ.get("WP_E2E_ENABLED") == "1":
        return
    skip = pytest.mark.skip(reason="requires the isolated WordPress E2E runner")
    for item in items:
        if "wordpress/e2e" in str(item.fspath).replace("\\", "/"):
            item.add_marker(skip)


def pytest_sessionfinish(session, exitstatus):
    path = os.environ.get("WP_E2E_OBSERVATIONS_PATH")
    if not path:
        return
    with open(path, "w", encoding="utf-8") as stream:
        json.dump(
            {"exit_status": int(exitstatus), "observations": OBSERVATIONS},
            stream,
            ensure_ascii=False,
            indent=2,
        )
        stream.write("\n")


@pytest.fixture
def wordpress_e2e_case(tmp_path):
    if os.environ.get("WP_E2E_ENABLED") != "1":
        pytest.skip("requires the isolated WordPress E2E runner")
    from support import WordPressE2ECase

    case = WordPressE2ECase(tmp_path)
    yield case
    OBSERVATIONS.extend(case.observations)
