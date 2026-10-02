from typing import Any, Callable


class EditorialStage:
    def __init__(self, provider: Callable[[dict[str, Any], Any], dict[str, Any]]):
        self.provider = provider

    def __call__(self, context: dict[str, Any], state: Any) -> dict[str, Any]:
        return self.provider(context, state)
