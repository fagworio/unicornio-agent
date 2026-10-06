from .model import BlockerCode, Phase


class StageError(RuntimeError):
    """Expected operational failure that must return through the classifier."""

    def __init__(self, blocker: BlockerCode, phase: Phase, detail: str, *, human_required: bool = False):
        super().__init__(detail)
        self.blocker = blocker
        self.phase = phase
        self.detail = detail
        self.human_required = human_required
