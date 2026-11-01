"""Small, deterministic adapters for the configured AI backends.

The Hermes installation exposes its configured provider/model through the
``hermes -z`` one-shot command.  This module deliberately invokes that
command without a shell, so the editorial pipeline can use the same OAuth
provider as Hermes without copying credentials into Unicornio.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class AIProviderError(RuntimeError):
    """Raised when an AI backend cannot produce a trustworthy response."""

    def __init__(self, message: str, *, failure_class: str = "provider_error", retryable: bool = True) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.retryable = bool(retryable)


def hermes_command_available(command: str = "hermes") -> bool:
    """Return whether the configured Hermes executable can be resolved."""

    return bool(shutil.which(str(command).strip()))


def call_hermes_oneshot(
    prompt: str,
    *,
    command: str = "hermes",
    model: str = "",
    toolsets: str = "",
    timeout: float = 60.0,
    cwd: Path | str | None = None,
) -> tuple[str, dict[str, Any]]:
    """Run Hermes once and return its final text plus usage metadata.

    ``hermes -z`` is the canonical non-interactive interface.  No shell is
    used and the prompt is passed as one argv item, avoiding command
    interpolation.  Usage is optional because older Hermes versions may not
    implement ``--usage-file`` consistently; the model response itself is
    still required.
    """

    executable = str(command or "hermes").strip()
    if not executable or not hermes_command_available(executable):
        raise AIProviderError(
            f"executável Hermes não encontrado: {executable or 'hermes'}",
            failure_class="configuration_error",
            retryable=False,
        )
    argv = [executable, "-z", str(prompt)]
    if str(model or "").strip():
        argv[1:1] = ["-m", str(model).strip()]
    if str(toolsets or "").strip():
        argv[1:1] = ["-t", str(toolsets).strip()]
    usage_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(prefix="unicornio-hermes-usage-", suffix=".json", delete=False) as handle:
            usage_path = Path(handle.name)
        argv.extend(["--usage-file", str(usage_path)])
        try:
            completed = subprocess.run(
                argv,
                cwd=str(cwd) if cwd is not None else None,
                capture_output=True,
                text=True,
                timeout=max(1.0, float(timeout)),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AIProviderError(
                "Hermes excedeu o timeout da chamada editorial",
                failure_class="timeout",
                retryable=True,
            ) from exc
        except OSError as exc:
            raise AIProviderError(
                f"falha ao iniciar Hermes: {exc}",
                failure_class="transport_error",
                retryable=True,
            ) from exc
        output = (completed.stdout or "").strip()
        if completed.returncode != 0:
            detail = (completed.stderr or output or "processo Hermes terminou com erro").strip()
            raise AIProviderError(
                f"Hermes terminou com código {completed.returncode}: {detail[:500]}",
                failure_class="provider_error",
                retryable=True,
            )
        if not output:
            raise AIProviderError(
                "Hermes não retornou texto",
                failure_class="invalid_response",
                retryable=True,
            )
        usage: dict[str, Any] = {}
        if usage_path and usage_path.is_file():
            try:
                parsed = json.loads(usage_path.read_text(encoding="utf-8"))
                if isinstance(parsed, dict):
                    usage = parsed
            except (OSError, ValueError):
                usage = {}
        return output, usage
    finally:
        if usage_path is not None:
            try:
                usage_path.unlink(missing_ok=True)
            except OSError:
                pass


__all__ = ["AIProviderError", "call_hermes_oneshot", "hermes_command_available"]
