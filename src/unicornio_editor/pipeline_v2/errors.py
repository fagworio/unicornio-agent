from .model import BlockerCode, Phase


class StageError(RuntimeError):
    """Expected operational failure that must return through the classifier."""

    def __init__(self, blocker: BlockerCode, phase: Phase, detail: str):
        super().__init__(detail)
        self.blocker = blocker
        self.phase = phase
        self.detail = detail
