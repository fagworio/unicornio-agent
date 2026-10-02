import json
from pathlib import Path

from unicornio_editor.pipeline_v2.legacy import LegacyStateLoader, from_legacy_state
from unicornio_editor.pipeline_v2.scheduler import next_action
from unicornio_editor.pipeline_v2.shadow import compare_work_state


FIXTURES = Path(__file__).parent / "fixtures" / "v2_cases.json"


def load_case(case):
    loader = LegacyStateLoader(lambda _: {"accepted_media": case.get("assets", []), "featured": case.get("featured")})
    meta = {"_hermes_state": case["v1"].get("state"), "_hermes_partial_kind": case["v1"].get("partial_kind", ""), "_hermes_media_required": case["v1"].get("required", case["v1"].get("partial_required", 0)), "_hermes_media_completed": case["v1"].get("completed", case["v1"].get("partial_completed", 0)), "_hermes_media_missing": case["v1"].get("missing", case["v1"].get("partial_missing", 0)), "_hermes_last_error": case["v1"].get("last_error", "")}
    return loader.load(case["post_id"], meta)



def test_historical_fixtures_preserve_assets_and_next_action():
    cases = json.loads(FIXTURES.read_text())
    assert len(cases) == 10
    for case in cases:
        state = load_case(case)
        expected = case["expected"]
        if "state" in expected:
            assert state.state.value == expected["state"]
        if "blocker" in expected:
            assert state.blocker.value == expected["blocker"]
        if "accepted" in expected:
            assert state.media.accepted == expected["accepted"]
            assert state.media.missing == expected["missing"]
        assert next_action(state) == expected["next_action"]


def test_114849_legacy_fixture_is_featured_only_and_preserves_four_assets():
    case = next(item for item in json.loads(FIXTURES.read_text()) if item["post_id"] == 114849)
    state = load_case(case)
    assert [item.media_id for item in state.media.inline] == [101, 102, 103, 104]
    assert next_action(state) == "resolve_featured"


def test_deterministic_shadow_compares_state_progress_ids_slots_and_next_action():
    reports = []
    for case in json.loads(FIXTURES.read_text()):
        state = load_case(case)
        v1 = case["v1"]
        expected = {**case["expected"], "phase": "validate" if v1["state"] == "ready" else ("editorial" if v1["state"] == "blocked" else "media")}
        report = compare_work_state(
            case["post_id"], v1["state"], state,
            expected=expected,
            expected_ids=[item["media_id"] for item in case.get("assets", [])],
            expected_slots=[item.get("slot", item.get("paragraph_index", i + 1)) for i, item in enumerate(case.get("assets", []))],
            expected_action=expected["next_action"],
            actual_action=next_action(state),
            expected_featured={"rejected": "vision_rejected", "vision": "vision_rejected", "failed": "invalid"}.get(case.get("featured", {}).get("status"), case.get("featured", {}).get("status")) if case.get("featured") else None,
        )
        reports.append(report)
    assert len(reports) == 10
    assert all(report["equivalent"] for report in reports), reports
    assert all(not report["mismatches"] for report in reports)
