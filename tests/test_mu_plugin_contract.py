from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_human_reopen_markers_are_rest_readable_but_not_rest_writable():
    source = (ROOT / "wp/mu-plugins/unicornio-rest-meta.php").read_text(encoding="utf-8")
    registration = source.split("foreach ( array( '_hermes_human_reopened_at'", 1)[1]
    registration = registration.split("} );", 1)[0]
    assert "show_in_rest'      => true" in registration
    assert "auth_callback'     => '__return_false'" in registration
    assert "auth_callback'     => '__return_true'" not in registration


def test_status_transition_writes_reopen_marker_and_consumes_stale_marker():
    source = (ROOT / "wp/mu-plugins/unicornio-status-awaiting-human.php").read_text(encoding="utf-8")
    assert "update_post_meta( $post->ID, '_hermes_human_reopened_at'" in source
    assert "update_post_meta( $post->ID, '_hermes_human_reopened_from', 'awaiting_human' )" in source
    assert "if ( $new_status === 'awaiting_human' && $old_status !== 'awaiting_human' )" in source
    assert "delete_post_meta( $post->ID, '_hermes_human_reopened_at' )" in source
    assert "delete_post_meta( $post->ID, '_hermes_human_reopened_from' )" in source
