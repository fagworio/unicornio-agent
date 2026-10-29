from .model import BlockerCode, MediaProgress, Phase


class StageError(RuntimeError):
    """Expected operational failure that must return through the classifier."""

    def __init__(
        self,
        blocker: BlockerCode,
        phase: Phase,
        detail: str,
        *,
        human_required: bool = False,
        media_progress: MediaProgress | None = None,
        failure_class: str | None = None,
        retryable: bool | None = None,
        http_status: int | None = None,
    ):
        super().__init__(detail)
        self.blocker = blocker
        self.phase = phase
        self.detail = detail
        self.human_required = human_required
        self.media_progress = media_progress
        self.failure_class = failure_class
        self.retryable = retryable
        self.http_status = http_status
