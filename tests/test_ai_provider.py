import subprocess
from unittest import mock

import pytest

from unicornio_editor.ai_provider import AIProviderError, call_hermes_oneshot


def test_hermes_timeout_is_passed_without_artificial_one_second_floor():
    process = mock.Mock(returncode=0)
    process.communicate.return_value = ("ok", "")
    with mock.patch("unicornio_editor.ai_provider.hermes_command_available", return_value=True), \
            mock.patch("unicornio_editor.ai_provider.subprocess.Popen", return_value=process) as popen:
        call_hermes_oneshot("prompt", timeout=0.01)

    process.communicate.assert_called_once_with(timeout=0.01)
    assert popen.call_args.kwargs["text"] is True


def test_hermes_nonpositive_timeout_fails_before_starting_process():
    with mock.patch("unicornio_editor.ai_provider.hermes_command_available", return_value=True), \
            mock.patch("unicornio_editor.ai_provider.subprocess.Popen") as popen:
        with pytest.raises(AIProviderError, match="timeout"):
            call_hermes_oneshot("prompt", timeout=0)

    popen.assert_not_called()


def test_hermes_timeout_exposes_process_timing_metadata():
    process = mock.Mock(returncode=-9)
    process.communicate.side_effect = [
        subprocess.TimeoutExpired(["hermes"], 0.01),
        ("", ""),
    ]
    with mock.patch("unicornio_editor.ai_provider.hermes_command_available", return_value=True), \
            mock.patch("unicornio_editor.ai_provider.subprocess.Popen", return_value=process):
        with pytest.raises(AIProviderError) as error:
            call_hermes_oneshot("prompt", timeout=0.01)

    assert error.value.failure_class == "timeout"
    assert error.value.metadata["configured_timeout_seconds"] == 0.01
    assert "process_spawn_elapsed_ms" in error.value.metadata
    assert "provider_call_elapsed_ms" in error.value.metadata
