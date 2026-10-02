import json
from pathlib import Path

from unicornio_editor.pipeline_v2.legacy import LegacyStateLoader, from_legacy_state
from unicornio_editor.pipeline_v2.scheduler import next_action


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
