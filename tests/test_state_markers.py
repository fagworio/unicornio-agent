from pathlib import Path

from unicornio_editor.state import STATE_PARTIAL, STATE_UNCERTAIN, build_state_markers, read_state, retry_eligible


ROOT = Path(__file__).resolve().parents[1]


def test_uncertain_preserves_retry_cooldown():
    retry_at = "2026-10-01T15:00:00+00:00"
    markers = build_state_markers(
        STATE_UNCERTAIN,
        attempts=1,
        next_retry_at=retry_at,
        last_error="sem evidência suficiente",
    )
    assert markers["_hermes_next_retry_at"] == retry_at


def test_partial_persists_progress_and_is_retryable_after_cooldown():
    retry_at = "2026-10-01T15:00:00+00:00"
    markers = build_state_markers(
        STATE_PARTIAL,
        next_retry_at=retry_at,
        partial_kind="media",
        partial_required=6,
        partial_completed=4,
        partial_missing=2,
        processing_passes=1,
        no_progress_attempts=0,
    )
    state = read_state({"meta": markers})
    assert state["state"] == STATE_PARTIAL
    assert state["partial_required"] == 6
    assert state["partial_completed"] == 4
    assert state["partial_missing"] == 2
    assert state["no_progress_attempts"] == 0
    assert retry_eligible(state)


def test_monitor_script_has_stable_output_without_epoch_suffix():
    script = (ROOT / "hermes" / "monitor.sh").read_text(encoding="utf-8")
    assert "loop_epoch" not in script
    assert "out=\"${out}|loop_epoch=" not in script
