from typing import Any, Callable


class MediaStage:
    def __init__(self, resolver: Callable[[dict[str, Any], Any, dict[str, Any]], dict[str, Any]]):
        self.resolver = resolver

    def __call__(self, context: dict[str, Any], state: Any, editorial: dict[str, Any]) -> dict[str, Any]:
        return self.resolver(context, state, editorial)
