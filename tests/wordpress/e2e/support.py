from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from unicornio_editor.config import Config
from unicornio_editor.media.registry import connect as connect_registry, record_v2_observation
from unicornio_editor.pipeline_v2.model import (
    BlockerCode,
    LifecycleState,
    MediaProgress,
    Outcome,
    Phase,
    WorkState,
)
from unicornio_editor.pipeline_v2.runtime import WordPressWriterV2
from unicornio_editor.wordpress import WordPressClient


def human_required_state() -> WorkState:
    return WorkState(
        state=LifecycleState.HUMAN_REQUIRED,
        phase=Phase.MEDIA,
        blocker=BlockerCode.FEATURED_MISSING,
        relevance_approved=True,
        media=MediaProgress(required=0),
    )


class WordPressE2ECase:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.url = os.environ["WP_E2E_URL"].rstrip("/")
        self.user = os.environ["WP_E2E_USER"]
        self.password = os.environ["WP_E2E_APP_PASSWORD"]
        self.ids = {str(key): int(value) for key, value in json.loads(os.environ["WP_E2E_POST_IDS"]).items()}
        self.state = human_required_state()
        self.state_json = json.dumps(self.state.to_dict(), ensure_ascii=False, separators=(",", ":"))
        self.client = WordPressClient(Config(
            content_source="wordpress",
            wordpress_url=self.url,
            wordpress_api_base="/wp-json/wp/v2",
            app_user=self.user,
            app_password=self.password,
            dry_run=False,
            http_timeout=20.0,
        ))
        self.writer = WordPressWriterV2(self.client, self.root)
        self.observations: list[dict] = []

    def post_id(self, scenario: str) -> int:
        return self.ids[scenario]

    def _auth_headers(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.user}:{self.password}".encode()).decode("ascii")
        return {"Authorization": f"Basic {token}", "Accept": "application/json"}

    def _control(self, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        headers = self._auth_headers()
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.url + path,
            data=data,
            headers=headers,
            method="POST" if data is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise AssertionError(f"ephemeral WordPress control endpoint returned HTTP {exc.code}") from None
        if not isinstance(decoded, dict):
            raise AssertionError("ephemeral WordPress control endpoint returned invalid JSON")
        return decoded

    @staticmethod
    def _snapshot(post: dict) -> dict:
        meta = post.get("meta") if isinstance(post.get("meta"), dict) else {}
        return {
            "post_id": int(post.get("id") or 0),
            "status": str(post.get("status") or ""),
            "hermes_state": str(meta.get("_hermes_state") or ""),
            "hermes_work_state": str(meta.get("_hermes_work_state") or ""),
        }

    def _journal_summary(self, post_id: int) -> dict:
        path = self.root / "work" / "v2-journal" / f"{post_id}.json"
        if not path.is_file():
            return {"present": False}
        payload = json.loads(path.read_text(encoding="utf-8"))
        fields = (
            "status", "post_id", "run_id", "cycle_id", "cycle_round",
            "authorization_id", "protocol_version", "writer_started",
            "writer_status", "writer_outcome", "readback",
            "media_application_status", "html_readback_status",
        )
        return {"present": True, **{key: payload.get(key) for key in fields if key in payload}}

    def _registry_summary(self, run_id: str) -> dict:
        path = self.root / "work" / "media_registry.sqlite"
        if not path.is_file():
            return {"present": False}
        with connect_registry(self.root) as db:
            schema_row = db.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
            operation = db.execute(
                "SELECT post_id, run_id, operation_status, write_started, readback_confirmed, outcome FROM media_operations WHERE run_id=?",
                (run_id,),
            ).fetchone()
            media_run = db.execute(
                "SELECT post_id, queries_attempted, acquisition_outcome FROM media_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            return {
                "present": True,
                "schema_version": int(schema_row[0]) if schema_row else None,
                "media_operation": dict(operation) if operation else None,
                "media_run": dict(media_run) if media_run else None,
            }

    def invoke(self, scenario: str, *, fault: str | None = None) -> dict:
        post_id = self.post_id(scenario)
        before_post = self.client.get_post(post_id)
        before = self._snapshot(before_post)
        if fault:
            armed = self._control("/wp-json/unicornio-e2e/v1/fault", {"post_id": post_id, "mode": fault})
            assert armed.get("armed") is True
        run_id = f"wp-e2e-{scenario}-{uuid.uuid4().hex}"
        result = None
        error = None
        try:
            result = self.writer.commit(
                post_id,
                {"post": before_post, "v2_run_id": run_id},
                self.state,
                Outcome.human_required(Phase.MEDIA, BlockerCode.FEATURED_MISSING),
            )
        except Exception as exc:
            error = {
                "type": type(exc).__name__,
                "status_code": getattr(exc, "status_code", None),
                "api_code": str(getattr(exc, "api_code", "") or ""),
            }
        after_error = None
        try:
            after = self._snapshot(self.client.get_post(post_id))
        except Exception as exc:
            after = None
            after_error = {"type": type(exc).__name__, "status_code": getattr(exc, "status_code", None)}

        journal = self._journal_summary(post_id)
        operation_status = "failed" if error else "completed"
        record_v2_observation(
            self.root,
            post_id=post_id,
            run_id=run_id,
            state=self.state,
            phase="media",
            outcome="human_required" if result else ("writer_error" if error else "writer_result_unavailable"),
            operation_status=operation_status,
            write_started=bool(journal.get("writer_started")),
            readback_confirmed=bool(result and result.get("readback")),
        )
        audit = self._control(f"/wp-json/unicornio-e2e/v1/audit?post_id={post_id}")
        observation = {
            "scenario": scenario,
            "post_id": post_id,
            "run_id": run_id,
            "before": before,
            "after": after,
            "after_readback_error": after_error,
            "writer_result": result,
            "writer_error": error,
            "journal": journal,
            "rest_events": audit.get("events", []),
            "registry_v5": self._registry_summary(run_id),
        }
        self.observations.append(observation)
        return observation

    def current_post(self, post_id: int) -> dict:
        return self._snapshot(self.client.get_post(post_id))

    def record_wordpress_readback(self, scenario: str, post_id: int, journal: dict) -> None:
        snapshot_before = self._snapshot(self.client.get_post(post_id))
        snapshot_after = self._snapshot(self.client.get_post(post_id))
        audit = self._control(f"/wp-json/unicornio-e2e/v1/audit?post_id={post_id}")
        self.observations.append({
            "scenario": scenario,
            "post_id": post_id,
            "before": snapshot_before,
            "after": snapshot_after,
            "journal": journal,
            "rest_events": audit.get("events", []),
        })
