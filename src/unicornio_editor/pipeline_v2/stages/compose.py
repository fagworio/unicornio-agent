from typing import Any, Callable


class ComposeStage:
    def __init__(self, composer: Callable[[dict[str, Any], dict[str, Any], dict[str, Any]], dict[str, Any]]):
        self.composer = composer

    def __call__(self, context: dict[str, Any], editorial: dict[str, Any], media: dict[str, Any]) -> dict[str, Any]:
        return self.composer(context, editorial, media)
