"""Validated runtime configuration with fail-closed write safety."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlparse


class ConfigError(ValueError):
    """Raised when runtime configuration is unsafe or malformed."""


@dataclass(frozen=True, repr=False)
class Config:
    content_source: str
    wordpress_url: str
    wordpress_api_base: str
    app_user: str = ""
    app_password: str = field(default="", repr=False)
    dry_run: bool = True
    batch_limit: int = 3
    http_timeout: float = 15.0
    lock_ttl: int = 900
    min_relevance_confidence: float = 0.80
    min_skip_confidence: float = 0.90
    site_topics: tuple[str, ...] = ()
    publish_enabled: bool = False
    publish_limit: int = 0
    vision_enabled: bool = False
    vision_api_key: str = field(default="", repr=False)
    vision_base_url: str = ""
    vision_model: str = "gpt-4o-mini"

    def __repr__(self) -> str:
        return (
            "Config("
            f"content_source={self.content_source!r}, "
            f"wordpress_url={self.wordpress_url!r}, "
            f"wordpress_api_base={self.wordpress_api_base!r}, "
            f"app_user={self.app_user!r}, dry_run={self.dry_run!r}, "
            f"batch_limit={self.batch_limit!r}, http_timeout={self.http_timeout!r}, "
            f"lock_ttl={self.lock_ttl!r}, "
            f"min_relevance_confidence={self.min_relevance_confidence!r}, "
            f"min_skip_confidence={self.min_skip_confidence!r}, "
            f"site_topics={len(self.site_topics)} tópicos, "
            f"publish_enabled={self.publish_enabled!r}, "
            f"publish_limit={self.publish_limit!r}, "
            f"vision_enabled={self.vision_enabled!r}, "
            f"vision_model={self.vision_model!r})"
        )


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if not value:
        return default
    if value.lower() in {"1", "true", "yes", "on"}:
        return True
    if value.lower() in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be a boolean")


def _int(name: str, default: int, minimum: int, maximum: int) -> int:
    value = _env(name)
    try:
        parsed = int(value) if value else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _float(name: str, default: float, minimum: float, maximum: float) -> float:
    value = _env(name)
    try:
        parsed = float(value) if value else default
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if not minimum <= parsed <= maximum:
        raise ConfigError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _validate_url(name: str, value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConfigError(f"{name} must be an absolute HTTP(S) URL")
    return value.rstrip("/")


def load_config() -> Config:
    content_source = _env("CONTENT_SOURCE", "mock").lower()
    if content_source not in {"mock", "wordpress"}:
        raise ConfigError("CONTENT_SOURCE must be mock or wordpress")

    wordpress_url = _validate_url(
        "WORDPRESS_URL", _env("WORDPRESS_URL", "http://wordpress.dvl.to:8080")
    )
    api_base = _env("WORDPRESS_API_BASE", "/wp-json/wp/v2")
    if not api_base.startswith("/") or "?" in api_base or "#" in api_base:
        raise ConfigError("WORDPRESS_API_BASE must be a safe path")
    api_base = "/" + api_base.strip("/")

    dry_run = _bool("EDITOR_DRY_RUN", True)
    app_user = _env("WORDPRESS_APP_USER")
    app_password = _env("WORDPRESS_APP_PASSWORD")
    if not dry_run and (not app_user or not app_password):
        raise ConfigError("write mode requires WORDPRESS_APP_USER and WORDPRESS_APP_PASSWORD")

    return Config(
        content_source=content_source,
        wordpress_url=wordpress_url,
        wordpress_api_base=api_base,
        app_user=app_user,
        app_password=app_password,
        dry_run=dry_run,
        batch_limit=_int("EDITOR_BATCH_LIMIT", 3, 1, 100),
        http_timeout=_float("EDITOR_HTTP_TIMEOUT", 15.0, 1.0, 120.0),
        lock_ttl=_int("EDITOR_LOCK_TTL", 900, 30, 86400),
        min_relevance_confidence=_float(
            "EDITOR_MIN_RELEVANCE_CONFIDENCE", 0.80, 0.0, 1.0
        ),
        min_skip_confidence=_float("EDITOR_MIN_SKIP_CONFIDENCE", 0.90, 0.0, 1.0),
        site_topics=_topics("SITE_TOPICS"),
        publish_enabled=_bool("PUBLISH_ENABLED", False),
        publish_limit=_int("PUBLISH_LIMIT", 0, 0, 100),
        vision_enabled=_bool("EDITOR_VISION_ENABLED", False),
        vision_api_key=_env("EDITOR_VISION_API_KEY"),
        vision_base_url=_validate_url(
            "EDITOR_VISION_BASE_URL", _env("EDITOR_VISION_BASE_URL", "https://api.openai.com/v1")
        ),
        vision_model=_env("EDITOR_VISION_MODEL", "gpt-4o-mini"),
    )


def _topics(name: str) -> tuple[str, ...]:
    """Comma-separated editorial topics; empty when unset (gate off)."""
    return tuple(
        topic.strip()
        for topic in _env(name).split(",")
        if topic.strip()
    )
