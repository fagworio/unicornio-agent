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


CURRENT_RETRY_POLICY_VERSION = 3


@dataclass(frozen=True)
class RetryInfo:
    attempts: int = 0
    no_progress: int = 0
    next_at: str | None = None
    policy_version: int = 1
    phase_attempts: int = 0

    def __post_init__(self) -> None:
        if (
            self.attempts < 0
            or self.no_progress < 0
            or self.policy_version < 1
            or self.phase_attempts < 0
        ):
            raise ValueError("retry counters cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempts": self.attempts,
            "no_progress": self.no_progress,
            "next_at": self.next_at,
            "policy_version": self.policy_version,
            "phase_attempts": self.phase_attempts,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "RetryInfo":
        value = value or {}
        return cls(
            int(value.get("attempts", 0)),
            int(value.get("no_progress", 0)),
            value.get("next_at"),
            int(value.get("policy_version", 1)),
            int(value.get("phase_attempts", 0)),
        )


class FeaturedStatus(_ValueEnum):
    MISSING = "missing"
    VALID = "valid"
    INVALID = "invalid"
    VISION_REJECTED = "vision_rejected"


@dataclass(frozen=True)
class FeaturedProgress:
    status: FeaturedStatus = FeaturedStatus.MISSING
    media_id: int | None = None
    media_url: str | None = None
    # Identity is part of the accepted V2 state.  Older checkpoints simply
    # deserialize with empty values and are materialised lazily by the media
    # resolver before they can be used as a visual baseline.
    sha256: str = ""
    phash: str = ""
    visual_group_id: str = ""
    visual_verification: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, FeaturedStatus):
            object.__setattr__(self, "status", FeaturedStatus(self.status))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value, "media_id": self.media_id,
            "media_url": self.media_url, "sha256": self.sha256,
            "phash": self.phash, "visual_group_id": self.visual_group_id,
            "visual_verification": self.visual_verification,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "FeaturedProgress":
        value = value or {}
        verification = value.get("visual_verification")
        return cls(
            value.get("status", "missing"), value.get("media_id"), value.get("media_url"),
            str(value.get("sha256") or ""), str(value.get("phash") or ""),
            str(value.get("visual_group_id") or ""),
            dict(verification) if isinstance(verification, dict) else {},
        )


@dataclass(frozen=True)
class MediaSearchProgress:
    """Auditable result of the deterministic media search for this post."""

    completed: bool = False
    exhausted: bool = False
    queries_attempted: int = 0
    engines_attempted: tuple[str, ...] = ()
    engines_disabled: tuple[str, ...] = ()
    candidates_seen: int = 0
    candidates_rejected: int = 0
    distinct_valid_frames: int = 0
    queries_planned: int = 0
    queries_completed: int = 0
    completion_reason: str = ""

    def __post_init__(self) -> None:
        if min(
            self.queries_attempted,
            self.candidates_seen,
            self.candidates_rejected,
            self.distinct_valid_frames,
            self.queries_planned,
            self.queries_completed,
        ) < 0:
            raise ValueError("media search counters cannot be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "completed": self.completed,
            "exhausted": self.exhausted,
            "queries_attempted": self.queries_attempted,
            "engines_attempted": list(self.engines_attempted),
            "engines_disabled": list(self.engines_disabled),
            "candidates_seen": self.candidates_seen,
            "candidates_rejected": self.candidates_rejected,
            "distinct_valid_frames": self.distinct_valid_frames,
            "queries_planned": self.queries_planned,
            "queries_completed": self.queries_completed,
            "completion_reason": self.completion_reason,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "MediaSearchProgress":
        value = value or {}
        engines = value.get("engines_attempted") or ()
        if not isinstance(engines, (list, tuple)):
            engines = ()
        disabled = value.get("engines_disabled") or ()
        if not isinstance(disabled, (list, tuple)):
            disabled = ()
        return cls(
            completed=bool(value.get("completed", False)),
            exhausted=bool(value.get("exhausted", False)),
            queries_attempted=int(value.get("queries_attempted", 0)),
            engines_attempted=tuple(str(item) for item in engines),
            engines_disabled=tuple(str(item) for item in disabled),
            candidates_seen=int(value.get("candidates_seen", 0)),
            candidates_rejected=int(value.get("candidates_rejected", 0)),
            distinct_valid_frames=int(value.get("distinct_valid_frames", 0)),
            queries_planned=int(value.get("queries_planned", 0)),
            queries_completed=int(value.get("queries_completed", 0)),
            completion_reason=str(value.get("completion_reason") or ""),
        )


@dataclass(frozen=True)
class InlineMedia:
    media_id: int
    media_url: str
    slot: int
    alt_text: str = ""
    credit_text: str = ""
    subject: str = ""
    item_number: int | None = None
    section_heading: str = ""
    section_slot: int | None = None
    width: int = 1200
    height: int = 800
    phash: str = ""
    sha256: str = ""
    visual_group_id: str = ""
    visual_verification: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.media_id < 1 or self.slot < 0:
            raise ValueError("media_id must be positive and slot cannot be negative")
        if self.section_slot is not None and self.section_slot < 0:
            raise ValueError("section_slot cannot be negative")
        if self.width <= 0 or self.height <= 0:
            raise ValueError("media dimensions must be positive")

    def to_dict(self) -> dict[str, Any]:
        return {
            "media_id": self.media_id,
            "media_url": self.media_url,
            "slot": self.slot,
            "alt_text": self.alt_text,
            "credit_text": self.credit_text,
            "subject": self.subject,
            "item_number": self.item_number,
            "section_heading": self.section_heading,
            "section_slot": self.section_slot,
            "width": self.width,
            "height": self.height,
            "phash": self.phash,
            "sha256": self.sha256,
            "visual_group_id": self.visual_group_id,
            "visual_verification": self.visual_verification,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "InlineMedia":
        item_number = value.get("item_number")
        section_slot = value.get("section_slot")
        return cls(
            int(value["media_id"]),
            str(value.get("media_url", "")),
            int(value.get("slot", value.get("paragraph_index", 0))),
            str(value.get("alt_text", "")),
            str(value.get("credit_text", "")),
            str(value.get("subject", "")),
            int(item_number) if item_number is not None else None,
            str(value.get("section_heading", "")),
            int(section_slot) if section_slot is not None else None,
            int(value.get("width", 1200)),
            int(value.get("height", 800)),
            str(value.get("phash") or ""),
            str(value.get("sha256") or ""),
            str(value.get("visual_group_id") or ""),
            dict(value.get("visual_verification") or {}),
        )


@dataclass(frozen=True)
class MediaProgress:
    required: int = 0
    inline: tuple[InlineMedia, ...] = ()
    featured: FeaturedProgress = field(default_factory=FeaturedProgress)
    accepted_count: int | None = None
    search: MediaSearchProgress = field(default_factory=MediaSearchProgress)
    enrichment_round: int = 0
    waiver_applied: bool = False
    waiver_reason: str = ""

    def __post_init__(self) -> None:
        if self.required < 0:
            raise ValueError("media counts cannot be negative")
        ids = [item.media_id for item in self.inline]
        slots = [item.slot for item in self.inline]
        if len(ids) != len(set(ids)) or len(slots) != len(set(slots)):
            raise ValueError("duplicate inline media or slot")
        if self.accepted_count is not None and (
            self.accepted_count < len(self.inline)
        ):
            raise ValueError("accepted_count must cover inline assets")
        if self.enrichment_round < 0:
            raise ValueError("enrichment_round cannot be negative")

    @property
    def accepted(self) -> int:
        return len(self.inline) if self.accepted_count is None else self.accepted_count

    @property
    def missing(self) -> int:
        return max(0, self.required - self.accepted)

    def to_dict(self) -> dict[str, Any]:
        return {
            "inline": {
                "required": self.required,
                "accepted": [item.to_dict() for item in self.inline],
                "accepted_count": self.accepted,
                "missing": self.missing,
            },
            "featured": self.featured.to_dict(),
            "search": self.search.to_dict(),
            "enrichment_round": self.enrichment_round,
            "waiver": {
                "applied": self.waiver_applied,
                "reason": self.waiver_reason,
            },
            "visual_identity_policy": 4,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "MediaProgress":
        value = value or {}
        inline = value.get("inline", value)
        assets = inline.get("accepted", []) if isinstance(inline, dict) else []
        if isinstance(assets, int):
            assets = []
        accepted_count = inline.get("accepted_count") if isinstance(inline, dict) else None
        parsed_count = int(accepted_count) if accepted_count is not None else None
        if parsed_count is not None and parsed_count == len(assets):
            parsed_count = None
        waiver = value.get("waiver") or {}
        return cls(
            int(inline.get("required", 0)),
            tuple(InlineMedia.from_dict(item) for item in assets),
            FeaturedProgress.from_dict(value.get("featured")),
            parsed_count,
            MediaSearchProgress.from_dict(value.get("search")),
            int(value.get("enrichment_round", 0)),
            bool(waiver.get("applied", value.get("waiver_applied", False))),
            str(waiver.get("reason", value.get("waiver_reason", "")) or ""),
        )


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
    detail: str | None = None

    @classmethod
    def ready(cls) -> "Outcome":
        return cls(OutcomeType.READY)

    @classmethod
    def retry(cls, phase: Phase, blocker: BlockerCode, next_at: str | None = None) -> "Outcome":
        return cls(OutcomeType.RETRY, phase, blocker, next_at)

    @classmethod
    def human_required(cls, phase: Phase | None = None, blocker: BlockerCode | None = None, detail: str | None = None) -> "Outcome":
        return cls(OutcomeType.HUMAN_REQUIRED, phase, blocker, None, detail)

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
        if self.detail is not None:
            result["detail"] = self.detail
        return result
