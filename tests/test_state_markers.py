from pathlib import Path

from unicornio_editor.state import STATE_UNCERTAIN, build_state_markers


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


def test_monitor_script_has_stable_output_without_epoch_suffix():
    script = (ROOT / "hermes" / "monitor.sh").read_text(encoding="utf-8")
    assert "loop_epoch" not in script
    assert "out=\"${out}|loop_epoch=" not in script
