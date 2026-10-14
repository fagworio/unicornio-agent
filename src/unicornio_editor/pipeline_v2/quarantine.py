"""Shared fail-closed policy for posts isolated from automated workflows."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class QuarantinePolicyError(RuntimeError):
    """The quarantine policy is absent or malformed; writes are unsafe."""


class PostQuarantinedError(RuntimeError):
    """An automated operation was requested for a quarantined post."""


def load_quarantine(path: str | Path) -> tuple[dict[int, dict[str, str]], str | None]:
    """Load and validate the explicit quarantine policy without modifying it."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {}, f"V2 quarantine policy could not be read: {type(exc).__name__}"
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload["version"] != 1
    ):
        return {}, "V2 quarantine schema version must be 1"
    rows = payload.get("posts")
    releases = payload.get("release_history")
    if not isinstance(rows, list) or not isinstance(releases, list):
        return {}, "V2 quarantine posts and release_history must be lists"
    result: dict[int, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            return {}, "V2 quarantine contains a non-object post"
        post_id = row.get("post_id")
        if type(post_id) is not int:
            return {}, "V2 quarantine post_id must be an integer"
        reason_code = row.get("reason_code")
        reason = row.get("reason")
        if not isinstance(reason_code, str) or not isinstance(reason, str):
            return {}, "V2 quarantine reason_code and reason must be strings"
        reason_code = reason_code.strip()
        reason = reason.strip()
        if post_id < 1 or post_id in result or not reason_code or not reason:
            return {}, "V2 quarantine has duplicate/invalid post or missing reason"
        result[post_id] = {"reason_code": reason_code, "reason": reason}
    for release in releases:
        if (
            not isinstance(release, dict)
            or not isinstance(release.get("authorization_id"), str)
            or not release["authorization_id"].strip()
            or not isinstance(release.get("authorized_by"), str)
            or not release["authorized_by"].strip()
            or not isinstance(release.get("reason"), str)
            or not release["reason"].strip()
        ):
            return {}, "V2 quarantine release history requires authorization_id, authorized_by and reason"
        if type(release.get("post_id")) is not int or release["post_id"] < 1:
            return {}, "V2 quarantine release history has invalid post_id"
    return result, None


def quarantine_for_root(root: str | Path) -> dict[int, dict[str, str]]:
    """Return policy entries or fail closed if the policy cannot be trusted."""
    path = Path(root) / "config" / "v2-quarantine.json"
    quarantines, error = load_quarantine(path)
    if error:
        raise QuarantinePolicyError(error)
    return quarantines


def assert_post_not_quarantined(
    root: str | Path, post_id: int, *, operation: str
) -> None:
    """Block any automated write against an isolated post before side effects."""
    entry = quarantine_for_root(root).get(int(post_id))
    if entry:
        raise PostQuarantinedError(
            f"post {post_id} is quarantined for {operation}: {entry['reason_code']}"
        )


def filter_quarantined_posts(
    root: str | Path, posts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Exclude quarantined posts from agent-facing queues and cards."""
    quarantines = quarantine_for_root(root)
    return [
        post for post in posts
        if not isinstance(post.get("id"), int) or post["id"] not in quarantines
    ]
