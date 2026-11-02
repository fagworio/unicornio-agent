import subprocess
from unittest import mock

import pytest

from unicornio_editor.ai_provider import AIProviderError, call_hermes_oneshot


def test_hermes_timeout_is_passed_without_artificial_one_second_floor():
    completed = subprocess.CompletedProcess(["hermes"], 0, stdout="ok", stderr="")
    with mock.patch("unicornio_editor.ai_provider.hermes_command_available", return_value=True), \
            mock.patch("unicornio_editor.ai_provider.subprocess.run", return_value=completed) as run:
        call_hermes_oneshot("prompt", timeout=0.01)

    assert run.call_args.kwargs["timeout"] == 0.01


def test_hermes_nonpositive_timeout_fails_before_starting_process():
    with mock.patch("unicornio_editor.ai_provider.hermes_command_available", return_value=True), \
            mock.patch("unicornio_editor.ai_provider.subprocess.run") as run:
        with pytest.raises(AIProviderError, match="timeout"):
            call_hermes_oneshot("prompt", timeout=0)

    run.assert_not_called()
