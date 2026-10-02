"""Serializable, side-effect-free V2 pipeline domain types."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class _ValueEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class LifecycleState(_ValueEnum):
    PENDING = "pending"
    READY = "ready"
    HUMAN_REQUIRED = "human_required"
    SKIPPED = "skipped"
    PUBLISHED = "published"


class Phase(_ValueEnum):
    RELEVANCE = "relevance"
    EDITORIAL = "editorial"
    MEDIA = "media"
    COMPOSE = "compose"
    VALIDATE = "validate"
    PUBLISH = "publish"


class BlockerCode(_ValueEnum):
    RELEVANCE_UNCERTAIN = "relevance_uncertain"
    TEXT_QUALITY = "text_quality"
    SEO = "seo"
    STRUCTURE = "structure"
    SOURCE = "source"
    TRAILER = "trailer"
    SCHEMA = "schema"
    INLINE_MISSING = "inline_missing"
    FEATURED_MISSING = "featured_missing"
    FEATURED_INVALID = "featured_invalid"
    FEATURED_VISION = "featured_vision"
    MEDIA_ORIGIN = "media_origin"
    MEDIA_DUPLICATE = "media_duplicate"
    MEDIA_INVALID = "media_invalid"
    PROVIDER_ERROR = "provider_error"
    WORDPRESS_ERROR = "wordpress_error"
    MANIFEST_INVALID = "manifest_invalid"
    INTERNAL_ERROR = "internal_error"


class OutcomeType(_ValueEnum):
    READY = "ready"
    RETRY = "retry"
    HUMAN_REQUIRED = "human_required"
    SKIPPED = "skipped"


@dataclass(frozen=True)
class RetryInfo:
    attempts: int = 0
    no_progress: int = 0
    next_at: str | None = None

    def __post_init__(self) -> None:
        if self.attempts < 0 or self.no_progress < 0:
            raise ValueError("retry counters cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {"attempts": self.attempts, "no_progress": self.no_progress, "next_at": self.next_at}

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "RetryInfo":
        value = value or {}
        return cls(int(value.get("attempts", 0)), int(value.get("no_progress", 0)), value.get("next_at"))


@dataclass(frozen=True)
class FeaturedProgress:
    status: str = "missing"
    media_id: int | None = None
    media_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "media_id": self.media_id, "media_url": self.media_url}

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "FeaturedProgress":
        value = value or {}
        return cls(value.get("status", "missing"), value.get("media_id"), value.get("media_url"))


@dataclass(frozen=True)
class InlineMedia:
    media_id: int
    media_url: str
    slot: int
    alt_text: str = ""
    credit_text: str = ""

    def __post_init__(self) -> None:
        if self.media_id < 1 or self.slot < 1:
            raise ValueError("media_id and slot must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {"media_id": self.media_id, "media_url": self.media_url, "slot": self.slot, "alt_text": self.alt_text, "credit_text": self.credit_text}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InlineMedia":
        return cls(int(value["media_id"]), str(value.get("media_url", "")), int(value.get("slot", value.get("paragraph_index", 0))), str(value.get("alt_text", "")), str(value.get("credit_text", "")))


@dataclass(frozen=True)
class MediaProgress:
    required: int = 0
    inline: tuple[InlineMedia, ...] = ()
    featured: FeaturedProgress = field(default_factory=FeaturedProgress)

    def __post_init__(self) -> None:
        if self.required < 0:
            raise ValueError("media counts cannot be negative")
        ids = [item.media_id for item in self.inline]
        slots = [item.slot for item in self.inline]
        if len(ids) != len(set(ids)) or len(slots) != len(set(slots)):
            raise ValueError("duplicate inline media or slot")
        if len(self.inline) > self.required:
            raise ValueError("accepted cannot exceed required")

    @property
    def accepted(self) -> int:
        return len(self.inline)

    @property
    def missing(self) -> int:
        return max(0, self.required - self.accepted)

    def to_dict(self) -> dict[str, Any]:
        return {"inline": {"required": self.required, "accepted": [item.to_dict() for item in self.inline], "missing": self.missing}, "featured": self.featured.to_dict()}

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "MediaProgress":
        value = value or {}
        inline = value.get("inline", value)
        assets = inline.get("accepted", []) if isinstance(inline, dict) else []
        if isinstance(assets, int):
            assets = []
        return cls(int(inline.get("required", 0)), tuple(InlineMedia.from_dict(item) for item in assets), FeaturedProgress.from_dict(value.get("featured")))


@dataclass(frozen=True)
class WorkState:
    state: LifecycleState = LifecycleState.PENDING
    phase: Phase = Phase.RELEVANCE
    blocker: BlockerCode | None = None
    retry: RetryInfo = field(default_factory=RetryInfo)
    relevance_approved: bool = False
    media: MediaProgress = field(default_factory=MediaProgress)
    version: int = 2

    def __post_init__(self) -> None:
        if self.version != 2:
            raise ValueError("unsupported work state version")
        if self.state is LifecycleState.READY and (self.blocker is not None or not self.relevance_approved):
            raise ValueError("READY requires approved relevance and no blocker")
        if self.state is LifecycleState.PUBLISHED and self.blocker is not None:
            raise ValueError("PUBLISHED requires no blocker")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "state": self.state.value,
            "phase": self.phase.value,
            "blocker": self.blocker.value if self.blocker else None,
            "retry": self.retry.to_dict(),
            "editorial": {"relevance_approved": self.relevance_approved},
            "media": self.media.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "WorkState":
        editorial = value.get("editorial", {})
        blocker = value.get("blocker")
        return cls(
            state=LifecycleState(value.get("state", LifecycleState.PENDING.value)),
            phase=Phase(value.get("phase", Phase.RELEVANCE.value)),
            blocker=BlockerCode(blocker) if blocker else None,
            retry=RetryInfo.from_dict(value.get("retry")),
            relevance_approved=bool(editorial.get("relevance_approved", False)),
            media=MediaProgress.from_dict(value.get("media")),
            version=int(value.get("version", 2)),
        )


@dataclass(frozen=True)
class Outcome:
    type: OutcomeType
    phase: Phase | None = None
    blocker: BlockerCode | None = None
    next_at: str | None = None

    @classmethod
    def ready(cls) -> "Outcome":
        return cls(OutcomeType.READY)

    @classmethod
    def retry(cls, phase: Phase, blocker: BlockerCode, next_at: str | None = None) -> "Outcome":
        return cls(OutcomeType.RETRY, phase, blocker, next_at)

    @classmethod
    def human_required(cls, phase: Phase | None = None, blocker: BlockerCode | None = None) -> "Outcome":
        return cls(OutcomeType.HUMAN_REQUIRED, phase, blocker)

    @classmethod
    def skipped(cls) -> "Outcome":
        return cls(OutcomeType.SKIPPED)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"type": self.type.value}
        if self.phase is not None:
            result["phase"] = self.phase.value
        if self.blocker is not None:
            result["blocker"] = self.blocker.value
        if self.next_at is not None:
            result["next_at"] = self.next_at
        return result
