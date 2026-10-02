from typing import Any, Callable


class ValidateStage:
    def __init__(self, validator: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]):
        self.validator = validator

    def __call__(self, context: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
        return self.validator(context, candidate)
