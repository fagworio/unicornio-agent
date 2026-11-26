"""Batch context contracts: independent envelopes and fail-soft items."""

import io
import json
import re
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest import mock

from unicornio_editor.batch import (
    BatchAlreadyInitializedError,
    BatchError,
    build_media_resolve_batch_input,
    load_media_resolve_batch,
    merge_media_manifest_into_editorial,
    prepare_batch,
    resolve_batch_editorial_defaults,
    validate_batch_id,
)
from unicornio_editor.config import Config
from unicornio_editor.batch import load_editorial_batch
from unicornio_editor.locking import LockManager
from unicornio_editor.cli import (
    _apply_editorial_batch,
    _media_candidate,
    main,
    preflight_apply_candidate,
)
from unicornio_editor.wordpress import WordPressError


class BatchClient:
    def __init__(self, posts):
        self.posts = posts

    def get_post(self, post_id):
        if post_id not in self.posts:
            raise ValueError(f"post {post_id} ausente")
        return self.posts[post_id]

    def update_post(self, post_id, payload):
        self.posts[post_id].setdefault("meta", {}).update(payload.get("meta", {}))
        if "status" in payload:
            self.posts[post_id]["status"] = payload["status"]
        return self.posts[post_id]

    def move_to_status(self, post_id, status):
        self.posts[post_id]["status"] = status
        return self.posts[post_id]


def _post(post_id, title):
    return {
        "id": post_id,
        "status": "pending",
        "title": {"raw": title},
        "date": "2026-09-24T12:00:00",
        "link": f"https://wp.test/?p={post_id}",
        "content": {"raw": "<article><p>Texto original sobre videogame.</p></article>"},
        "meta": {"original_link": "https://source.test/news"},
        "featured_media": 0,
    }


class BatchContextTests(unittest.TestCase):
    def config(self):
        return Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=True)

    def test_prepare_batch_collision_preserves_all_existing_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = BatchClient({1: _post(1, "Post um")})
            first = prepare_batch(client, self.config(), root, [1], batch_id="batch-fixed")
            batch_dir = Path(first["manifest"]).parent
            (batch_dir / "editorial.output.json").write_bytes(b"sentinel output")
            before = {path.name: path.read_bytes() for path in batch_dir.iterdir()}
            with mock.patch.object(client, "get_post") as get_post, \
                    mock.patch("unicornio_editor.batch.session_budget.skipped_locked_posts") as ledger:
                for ids in ([1], [2]):
                    with self.assertRaises(BatchAlreadyInitializedError) as conflict:
                        prepare_batch(client, self.config(), root, ids, batch_id="batch-fixed")
                    self.assertEqual(conflict.exception.batch_id, "batch-fixed")
                with mock.patch("unicornio_editor.batch.new_batch_id", return_value="batch-fixed"), \
                        self.assertRaises(BatchAlreadyInitializedError):
                    prepare_batch(client, self.config(), root, [2])
            get_post.assert_not_called()
            ledger.assert_not_called()
            self.assertEqual({path.name: path.read_bytes() for path in batch_dir.iterdir()}, before)
            self.assertFalse((root / "backups" / "2").exists())

    def test_prepare_batch_refuses_empty_or_incomplete_reservations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = mock.Mock()
            batches = root / "work" / "batches"
            empty = batches / "empty"
            empty.mkdir(parents=True)
            partial = batches / "partial"
            partial.mkdir()
            (partial / "post-1.json").write_bytes(b"sentinel incomplete")
            for batch_id in ("empty", "partial"):
                with self.subTest(batch_id=batch_id), self.assertRaises(BatchAlreadyInitializedError):
                    prepare_batch(client, self.config(), root, [2], batch_id=batch_id)
            self.assertEqual(list(empty.iterdir()), [])
            self.assertEqual((partial / "post-1.json").read_bytes(), b"sentinel incomplete")
            self.assertFalse((partial / "editorial.input.json").exists())
            self.assertEqual(client.mock_calls, [])

    def test_prepare_batch_reserves_before_wordpress_so_parallel_attempt_cannot_enter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entered = Event()
            release = Event()

            class WaitingClient(BatchClient):
                def get_post(self, post_id):
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError("test did not release the first preparation")
                    return super().get_post(post_id)

            primary = WaitingClient({1: _post(1, "Post um")})
            competing = mock.Mock()
            with ThreadPoolExecutor(max_workers=1) as workers:
                first = workers.submit(prepare_batch, primary, self.config(), root, [1], batch_id="shared")
                try:
                    self.assertTrue(entered.wait(5))
                    self.assertTrue((root / "work" / "batches" / "shared").is_dir())
                    with self.assertRaises(BatchAlreadyInitializedError):
                        prepare_batch(competing, self.config(), root, [2], batch_id="shared")
                    self.assertEqual(competing.mock_calls, [])
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=5)["prepared"], 1)
            batch_dir = root / "work" / "batches" / "shared"
            self.assertEqual(json.loads((batch_dir / "manifest.json").read_text())["requested_ids"], [1])
            self.assertFalse((batch_dir / "post-2.json").exists())

    def test_prepare_batch_cli_generates_distinct_ids_and_reports_local_conflict(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = BatchClient({1: _post(1, "Post um")})
            args = ["prepare-batch", "1", "--root", str(root)]
            runs = []
            with mock.patch("unicornio_editor.cli.load_config", return_value=self.config()), \
                    mock.patch("unicornio_editor.cli.WordPressClient", return_value=client):
                for _ in range(2):
                    stdout = io.StringIO()
                    with redirect_stdout(stdout):
                        self.assertEqual(main(args), 0)
                    runs.append(json.loads(stdout.getvalue()))
                before = Path(runs[0]["editorial_input"]).read_bytes()
                stdout, stderr = io.StringIO(), io.StringIO()
                with mock.patch.object(client, "get_post") as get_post, \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    self.assertEqual(main(args + ["--batch-id", runs[0]["batch_id"]]), 2)
                get_post.assert_not_called()
            self.assertNotEqual(runs[0]["batch_id"], runs[1]["batch_id"])
            for run in runs:
                self.assertRegex(run["batch_id"], r"^batch-\d{8}T\d{6}Z-[0-9a-f]{8}$")
                self.assertEqual(Path(run["editorial_input"]).parent.name, run["batch_id"])
            self.assertEqual(stdout.getvalue(), "")
            error = json.loads(stderr.getvalue())
            self.assertEqual(error["error_code"], "batch_already_initialized")
            self.assertEqual(error["batch_id"], runs[0]["batch_id"])
            self.assertFalse(error["retryable"])
            self.assertNotIn("resume_batch_id", error)
            self.assertEqual(Path(runs[0]["editorial_input"]).read_bytes(), before)

    def test_post_locks_allow_distinct_posts_to_progress_independently(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = LockManager(Path(directory) / "work" / "locks", ttl=900)
            with manager.acquire(1) as first, manager.acquire(2) as second:
                first.assert_owned()
                second.assert_owned()

    def test_prepare_batch_writes_manifest_and_one_context_per_post(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = BatchClient({1: _post(1, "Post um"), 2: _post(2, "Post dois")})
            result = prepare_batch(
                client, self.config(), root, [1, 2], batch_id="batch-test-001"
            )

            self.assertEqual(result["batch_id"], "batch-test-001")
            self.assertEqual(result["prepared"], 2)
            self.assertEqual(result["failed"], 0)
            manifest = Path(result["manifest"])
            self.assertTrue(manifest.exists())
            data = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["requested_ids"], [1, 2])
            editorial_input = Path(result["editorial_input"])
            self.assertTrue(editorial_input.exists())
            editorial = json.loads(editorial_input.read_text(encoding="utf-8"))
            self.assertEqual([item["post_id"] for item in editorial["posts"]], [1, 2])
            for post_id in (1, 2):
                context_file = root / "work" / "batches" / "batch-test-001" / f"post-{post_id}.json"
                context = json.loads(context_file.read_text(encoding="utf-8"))
                self.assertEqual(context["post_id"], post_id)
                self.assertEqual(context["batch_id"], "batch-test-001")
                self.assertEqual(context["status"], "pending")
                self.assertIn("cleaned_html", context)
                self.assertIn("source_language", context)
                self.assertFalse(context["localization"]["required"])
                self.assertIn("requirements", context)
                self.assertIn("relevance_policy", context)
                self.assertEqual(context["relevance_policy"]["allowed_topics"], [])
                # O envelope de AUDITORIA carrega os fatos de midia, mas o
                # envelope enviado ao modelo nao — sem isso o modelo sem
                # ferramentas responde needs_retry por "faltam imagens".
                media_stage = context["requirements"]["media_stage"]
                self.assertEqual(media_stage["stage"], "media-resolve-batch")
                self.assertEqual(media_stage["editorial_media_plan"], [])
                model_context = json.loads(
                    (root / "work" / "batches" / "batch-test-001" / "editorial.input.json")
                    .read_text(encoding="utf-8")
                )["posts"][0]
                self.assertNotIn("images", model_context)
                self.assertNotIn("featured", model_context)
                self.assertIn("relevance_policy", model_context)
                self.assertNotIn(
                    "required_inline_images", model_context["requirements"]["media_stage"]
                )
                self.assertEqual(
                    model_context["requirements"]["media_stage"]["editorial_media_plan"], []
                )
                self.assertTrue((root / "backups" / str(post_id) / "prepared.json").exists())

    def test_media_resolve_query_decodes_html_entities_in_game_and_title(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_id = "batch-query-entities"
            batch_dir = root / "work" / "batches" / batch_id
            batch_dir.mkdir(parents=True)
            (batch_dir / "post-7.json").write_text(json.dumps({
                "post_id": 7,
                "title": "Pokémon &amp; Bros.",
                "cleaned_html": "<p>Pokémon &amp; Bros. chega ao console.</p>",
                "featured": {"action": "provide"},
            }), encoding="utf-8")
            editorial = {
                "batch_id": batch_id,
                "results": [{
                    "post_id": 7,
                    "status": "ok",
                    "editorial": {
                        "title": "Pokémon &amp; Bros.",
                        "game_name": "Pokémon &amp; Bros.",
                        "cleaned_html": None,
                        "site_relevance": {"decision": "process"},
                    },
                }],
            }

            request = build_media_resolve_batch_input(editorial, root)["posts"][0]

            self.assertEqual(request["subject"], "Pokémon & Bros.")
            self.assertEqual(request["query"], "Pokémon & Bros.")

    def test_media_resolve_rejects_ambiguous_multi_game_non_list_article(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_id = "batch-separate-game-subjects"
            batch_dir = root / "work" / "batches" / batch_id
            batch_dir.mkdir(parents=True)
            (batch_dir / "post-8.json").write_text(json.dumps({
                "post_id": 8,
                "title": "Pokémon &amp; Bros. and Hades II",
                "cleaned_html": "<p>Pokémon &amp; Bros. and Hades II.</p>",
                "featured": {"action": "provide"},
            }), encoding="utf-8")
            editorial = {
                "batch_id": batch_id,
                "results": [{
                    "post_id": 8,
                    "status": "ok",
                    "editorial": {
                        "title": None,
                        "game_name": "Pokémon &amp; Bros.; Hades II",
                        "cleaned_html": None,
                        "site_relevance": {"decision": "process"},
                    },
                }],
            }

            result = build_media_resolve_batch_input(editorial, root)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["count"], 0)
            self.assertEqual(result["blocked_posts"][0]["error_code"], "multi_game_media_targets_unbound")
            self.assertFalse((batch_dir / "media.input.json").exists())

    def test_multi_game_listicle_media_targets_each_game_h2_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_id = "batch-multi-game-h2-subjects"
            batch_dir = root / "work" / "batches" / batch_id
            batch_dir.mkdir(parents=True)
            html = (
                "<h2>1. Game One: aventura</h2><p>Descrição completa do primeiro jogo.</p>"
                "<h2>2. Game Two: estratégia</h2><p>Descrição completa do segundo jogo.</p>"
            )
            (batch_dir / "post-9.json").write_text(json.dumps({
                "post_id": 9,
                "title": "2 jogos para aproveitar",
                "cleaned_html": html,
                "categories": ["Jogos"],
                "featured": {"action": "provide"},
            }), encoding="utf-8")
            editorial = {
                "batch_id": batch_id,
                "results": [{
                    "post_id": 9,
                    "status": "ok",
                    "editorial": {
                        "title": None,
                        "game_name": "Game One; Game Two",
                        "categories": ["Jogos"],
                        "cleaned_html": None,
                        "site_relevance": {"decision": "process"},
                    },
                }],
            }

            request = build_media_resolve_batch_input(editorial, root)["posts"][0]
            targets = request["targets"]

            self.assertEqual(request["subject"], "2 jogos para aproveitar")
            self.assertEqual(targets[0]["query"], "Game One: aventura 2 jogos para aproveitar")
            self.assertEqual(targets[1]["query"], "Game Two: estratégia 2 jogos para aproveitar")
            self.assertEqual(targets[-1]["role"], "featured")
            self.assertEqual(targets[-1]["query"], "2 jogos para aproveitar")

            editorial["results"][0]["editorial"]["game_name"] = "Game One; Unlisted Game"
            blocked = build_media_resolve_batch_input(editorial, root)
            self.assertEqual(blocked["status"], "blocked")
            self.assertEqual(blocked["blocked_posts"][0]["post_id"], 9)
            self.assertFalse((batch_dir / "media.input.json").exists())

    def test_prepare_batch_reports_locked_post_and_continues_sibling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = BatchClient({1: _post(1, "Post um"), 2: _post(2, "Post dois")})
            lock_manager = LockManager(root / "work" / "locks", ttl=900)
            with lock_manager.acquire(1):
                result = prepare_batch(
                    client, self.config(), root, [1, 2], batch_id="batch-lock-isolation"
                )
            self.assertEqual(
                [item["status"] for item in result["items"]],
                ["lock_blocked", "prepared"],
            )
            self.assertFalse((root / "backups" / "1").exists())
            self.assertTrue((root / "backups" / "2" / "prepared.json").exists())

    def test_prepare_batch_holds_post_lock_before_first_wordpress_read(self):
        from unicornio_editor.locking import LockError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = LockManager(root / "work" / "locks", ttl=900)
            events = []
            original_acquire = LockManager.acquire

            def tracked_acquire(lock_manager, post_id):
                try:
                    handle = original_acquire(lock_manager, post_id)
                except LockError:
                    events.append(("lock_blocked", post_id))
                    raise
                events.append(("lock_acquired", post_id))
                return handle

            class LockCheckingClient(BatchClient):
                def get_post(self, post_id):
                    events.append(("read", post_id))
                    try:
                        with manager.acquire(post_id):
                            pass
                    except LockError:
                        events.append(("probe_blocked", post_id))
                    else:
                        events.append(("probe_acquired", post_id))
                        raise AssertionError("WordPress post read occurred without its lock")
                    return super().get_post(post_id)

            with mock.patch.object(LockManager, "acquire", new=tracked_acquire):
                result = prepare_batch(
                    LockCheckingClient({1: _post(1, "Post um")}),
                    self.config(),
                    root,
                    [1],
                    batch_id="batch-read-under-lock",
                )

            self.assertEqual(result["prepared"], 1)
            read_indices = [index for index, event in enumerate(events) if event == ("read", 1)]
            acquired_indices = [
                index for index, event in enumerate(events) if event == ("lock_acquired", 1)
            ]
            self.assertTrue(read_indices)
            self.assertTrue(all(any(acquired < read for acquired in acquired_indices) for read in read_indices))

    def test_five_post_sequence_skips_first_locked_and_processes_four_in_bounded_batches(self):
        from unicornio_editor.locking import LockError
        from unicornio_editor.session_budget import status as session_status
        from unicornio_editor.workflow import WorkflowError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = replace(self.config(), dry_run=False, max_posts_touched_per_run=5)
            client = BatchClient({post_id: _post(post_id, f"Post {post_id}") for post_id in range(1, 6)})
            policy = root / "config" / "v2-quarantine.json"
            policy.parent.mkdir(parents=True, exist_ok=True)
            policy.write_text(json.dumps({"version": 1, "posts": [], "release_history": []}))
            writes = []
            prepare_results = []
            apply_results = []

            def apply_one(_client, _config, post_root, post_id, _editorial, **kwargs):
                try:
                    with LockManager(post_root / "work" / "locks", ttl=900).acquire(post_id) as lock:
                        lock.assert_owned()
                        kwargs["on_work_started"]()
                        writes.append(post_id)
                        return {
                            "post_id": post_id,
                            "status": "ready",
                            "dry_run": False,
                            "wordpress_changed": True,
                        }
                except LockError as exc:
                    raise WorkflowError(f"post {post_id} is already being processed") from exc

            with LockManager(root / "work" / "locks", ttl=900).acquire(1):
                for batch_number, ids in enumerate(([1, 2], [3, 4], [5]), start=1):
                    self.assertLessEqual(len(ids), 2)
                    batch_id = f"batch-five-{batch_number}"
                    prepared = prepare_batch(client, config, root, ids, batch_id=batch_id)
                    prepare_results.extend(prepared["items"])
                    eligible = [
                        {"post_id": int(item["post_id"]), "status": "ok", "editorial": {"placeholder": True}}
                        for item in prepared["items"]
                        if item.get("status") == "prepared"
                    ]
                    if not eligible:
                        continue
                    with mock.patch(
                        "unicornio_editor.cli.preflight_apply_candidate", return_value=(True, "")
                    ), mock.patch("unicornio_editor.cli.apply_editorial", side_effect=apply_one):
                        applied = _apply_editorial_batch(
                            client,
                            config,
                            root,
                            {"schema_version": 1, "batch_id": batch_id, "items": eligible},
                            dry_run=False,
                            compact=False,
                        )
                    apply_results.extend(applied["posts"])

            self.assertEqual([item["status"] for item in prepare_results], [
                "lock_blocked", "prepared", "prepared", "prepared", "prepared"
            ])
            self.assertEqual([item["status"] for item in apply_results], ["ready"] * 4)
            self.assertEqual(writes, [2, 3, 4, 5])
            self.assertEqual(len(writes), len(set(writes)))
            self.assertEqual(session_status(root, config)["posts_touched"], [2, 3, 4, 5])
            self.assertEqual(session_status(root, config)["posts_reserved"], [])

    def test_media_request_counts_inline_deficit_plus_missing_featured(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_id = "batch-media-count"
            batch_dir = root / "work" / "batches" / batch_id
            batch_dir.mkdir(parents=True)
            prepared = {
                "post_id": 1,
                "title": "Post comum sobre videogames",
                "cleaned_html": "<p>Texto original sobre videogames.</p>",
                "entities": ["videogames"],
                "featured": {"action": "provide", "valid": False},
            }
            (batch_dir / "post-1.json").write_text(json.dumps(prepared), encoding="utf-8")
            editorial = {
                "batch_id": batch_id,
                "results": [{
                    "post_id": 1,
                    "status": "ok",
                    "editorial": {
                        "title": None,
                        "game_name": None,
                        "cleaned_html": None,
                    },
                }],
            }

            result = build_media_resolve_batch_input(editorial, root)

            self.assertEqual(result["count"], 1)
            media_input = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
            request = media_input["posts"][0]
            self.assertEqual(request["inline_deficit"], 2)
            self.assertTrue(request["featured_required"])
            self.assertEqual(request["needed"], 3)

    def test_long_normal_article_requests_six_inline_plus_featured(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_id = "batch-long-media-count"
            batch_dir = root / "work" / "batches" / batch_id
            batch_dir.mkdir(parents=True)
            long_html = "<p>" + "videogame " * 1100 + "</p>"
            (batch_dir / "post-1.json").write_text(json.dumps({
                "post_id": 1,
                "title": "Post longo sobre videogames",
                "cleaned_html": long_html,
                "entities": ["videogames"],
                "featured": {"action": "provide", "valid": False},
            }), encoding="utf-8")
            editorial = {
                "batch_id": batch_id,
                "results": [{
                    "post_id": 1,
                    "status": "ok",
                    "editorial": {"title": None, "game_name": None, "cleaned_html": None},
                }],
            }

            result = build_media_resolve_batch_input(editorial, root)

            request = result["posts"][0]
            self.assertEqual(request["required_inline_images"], 6)
            self.assertEqual(request["inline_deficit"], 6)
            self.assertEqual(request["needed"], 7)
            normalized = load_media_resolve_batch(Path(result["path"]))
            self.assertEqual(normalized["posts"][0]["needed"], 7)

    def test_listicle_media_request_scales_past_seven_and_binds_each_heading(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_id = "batch-list-media"
            batch_dir = root / "work" / "batches" / batch_id
            batch_dir.mkdir(parents=True)
            html = "<p>Introdução sobre recomendações.</p>" + "".join(
                f"<h2>{index}. Jogo {index}: experiência distinta</h2>"
                f"<p>Descrição editorial completa do jogo número {index}, com contexto e detalhes.</p>"
                for index in range(1, 11)
            )
            (batch_dir / "post-55.json").write_text(json.dumps({
                "post_id": 55,
                "title": "10 jogos para conhecer",
                "cleaned_html": html,
                "entities": ["jogos"],
                "featured": {"action": "provide", "valid": False},
            }), encoding="utf-8")
            editorial = {
                "batch_id": batch_id,
                "results": [{
                    "post_id": 55,
                    "status": "ok",
                    "editorial": {
                        "title": None,
                        "game_name": None,
                        "categories": ["Jogos"],
                        "cleaned_html": None,
                    },
                }],
            }

            result = build_media_resolve_batch_input(editorial, root)
            request = result["posts"][0]

            self.assertEqual(request["required_inline_images"], 10)
            self.assertEqual(request["needed"], 11)
            self.assertEqual(len(request["targets"]), 11)
            inline_targets = [row for row in request["targets"] if row["role"] == "inline"]
            self.assertEqual(len(inline_targets), 10)
            self.assertEqual(inline_targets[0]["item_title"], "Jogo 1: experiência distinta")
            self.assertEqual(inline_targets[0]["item_identity"], "root:number-1")
            self.assertEqual(
                inline_targets[0]["query"],
                "Jogo 1: experiência distinta 10 jogos para conhecer",
            )
            self.assertEqual(
                inline_targets[1]["query"],
                "Jogo 2: experiência distinta 10 jogos para conhecer",
            )
            self.assertEqual(inline_targets[-1]["item_identity"], "root:number-10")
            self.assertEqual(request["targets"][-1]["role"], "featured")
            normalized = load_media_resolve_batch(Path(result["path"]))
            self.assertEqual(len(normalized["posts"][0]["targets"]), 11)

    def test_listicle_word_quota_extras_are_placed_after_item_descriptions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_id = "batch-list-extra-media"
            batch_dir = root / "work" / "batches" / batch_id
            batch_dir.mkdir(parents=True)
            long_description = "palavra editorial " * 180
            html = (
                "<h2>1. Jogo Um: análise detalhada</h2><p>" + long_description + "</p>"
                + "<h2>2. Jogo Dois: análise detalhada</h2><p>" + long_description + "</p>"
            )
            (batch_dir / "post-56.json").write_text(json.dumps({
                "post_id": 56,
                "title": "2 jogos para conhecer",
                "cleaned_html": html,
                "entities": ["jogos"],
                "featured": {"action": "ok", "valid": True},
            }), encoding="utf-8")
            editorial = {
                "batch_id": batch_id,
                "results": [{
                    "post_id": 56,
                    "status": "ok",
                    "editorial": {"title": None, "game_name": None, "cleaned_html": None},
                }],
            }

            request = build_media_resolve_batch_input(editorial, root)["posts"][0]

            self.assertEqual(request["required_inline_images"], 4)
            self.assertEqual(request["inline_deficit"], 4)
            self.assertEqual(len(request["targets"]), 4)
            extras = [row for row in request["targets"] if row.get("placement") == "after_list_item_description"]
            self.assertEqual(len(extras), 2)
            normalized = load_media_resolve_batch(
                root / "work" / "batches" / batch_id / "media.input.json"
            )
            self.assertEqual(len(normalized["posts"][0]["targets"]), 4)

    def test_media_resolver_runs_a_subject_query_per_list_target(self):
        import unicornio_editor.cli as cli

        targets = [
            {
                "target_id": "root:number-1",
                "role": "inline",
                "subject": "Metroid Prime 4",
                "query": "Metroid Prime 4 artwork",
                "paragraph_index": 0,
                "item_identity": "root:number-1",
                "item_title": "Metroid Prime 4",
                "section_heading": "Metroid Prime 4",
                "section_slot": 0,
                "replacement_reserve": 1,
            },
            {
                "target_id": "root:number-2",
                "role": "inline",
                "subject": "Hades II",
                "query": "Hades II artwork",
                "paragraph_index": 1,
                "item_identity": "root:number-2",
                "item_title": "Hades II",
                "section_heading": "Hades II",
                "section_slot": 1,
                "replacement_reserve": 1,
            },
        ]
        batch = {
            "schema_version": 1,
            "batch_id": "list-target-resolve",
            "posts": [{
                "post_id": 55,
                "subject": "Games list",
                "query": "Games list",
                "needed": 2,
                "required_inline_images": 2,
                "inline_deficit": 2,
                "featured_required": False,
                "replacement_reserve": 0,
                "targets": targets,
            }],
        }
        resolved_subjects = []

        def resolve(_client, _config, _root, sub_batch, **_kwargs):
            request = sub_batch["posts"][0]
            resolved_subjects.append(request["subject"])
            candidate = {
                "candidate_id": "same-provider-id",
                "direct_image_url": f"https://cdn.test/{request['subject'].replace(' ', '-')}.webp",
                "source_page_url": f"https://source.test/{request['subject'].replace(' ', '-')}",
                "credit_text": "Crédito da imagem: Fonte."
            }
            return {
                "posts": [{
                    "post_id": 55,
                    "reuse": [],
                    "candidates": [candidate],
                    "audit_candidates": [candidate],
                    "search": {"completion_reason": "TARGET_REACHED"},
                }],
                "vision_items": [{
                    "candidate_id": "same-provider-id",
                    "image_url": candidate["direct_image_url"],
                    "subject": request["subject"],
                }],
                "economics": {
                    "search_queries": 1,
                    "external_http_requests": 2,
                    "candidates_examined": 1,
                    "candidates_rejected": 0,
                },
            }

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "unicornio_editor.cli._resolve_media_batch", side_effect=resolve
        ):
            result = cli._resolve_media_targets_batch(
                object(),
                Config("wordpress", "http://wp.test", "/wp-json/wp/v2"),
                Path(directory),
                batch,
                full=True,
            )

        post = result["posts"][0]
        self.assertEqual(resolved_subjects, ["Metroid Prime 4", "Hades II"])
        self.assertEqual([row["item_identity"] for row in post["targets"]], [
            "root:number-1", "root:number-2",
        ])
        candidate_ids = [
            row["candidates"][0]["candidate_id"] for row in post["targets"]
        ]
        self.assertEqual(len(set(candidate_ids)), 2)
        self.assertEqual(result["economics"]["search_queries"], 2)

    def test_media_resolve_dispatch_uses_target_resolver_for_list_batches(self):
        import unicornio_editor.cli as cli

        batch = {
            "schema_version": 1,
            "batch_id": "list-dispatch",
            "posts": [{"post_id": 55, "targets": [{"target_id": "root:number-1"}]}],
        }
        with mock.patch(
            "unicornio_editor.cli._resolve_media_targets_batch",
            return_value={"resolver": "targets"},
        ) as target_resolver, mock.patch(
            "unicornio_editor.cli._resolve_media_batch",
            return_value={"resolver": "flat"},
        ) as flat_resolver:
            result = cli._resolve_media_batch_dispatch(
                object(), Config("wordpress", "http://wp.test", "/wp-json/wp/v2"),
                Path("."), batch, full=True,
            )

        self.assertEqual(result["resolver"], "targets")
        target_resolver.assert_called_once()
        flat_resolver.assert_not_called()

    def test_prepare_batch_exposes_uncertain_retry_context_and_allowed_topics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            post = _post(1, "Post sobre jogos")
            post["meta"] = {
                "_hermes_state": "uncertain",
                "_hermes_attempts": "1",
                "_hermes_last_error": "relevancia anterior inconclusiva",
                "original_link": "https://source.test/jogos",
            }
            config = replace(self.config(), site_topics=("games", "anime"))
            prepare_batch(BatchClient({1: post}), config, root, [1], batch_id="batch-retry-context")
            context = json.loads(
                (root / "work" / "batches" / "batch-retry-context" / "editorial.input.json")
                .read_text(encoding="utf-8")
            )["posts"][0]
            self.assertEqual(context["retry_mode"], "uncertain_second_pass")
            self.assertEqual(context["previous_relevance_reason"], "relevancia anterior inconclusiva")
            self.assertEqual(context["relevance_policy"]["allowed_topics"], ["games", "anime"])

    def test_prepare_batch_isolates_missing_post_and_keeps_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = BatchClient({1: _post(1, "Post um")})
            result = prepare_batch(
                client, self.config(), root, [1, 999], batch_id="batch-partial"
            )
            self.assertEqual(result["prepared"], 1)
            self.assertEqual(result["failed"], 1)
            self.assertEqual(result["items"][0]["status"], "prepared")
            self.assertEqual(result["items"][1]["status"], "error")
            self.assertTrue(
                (root / "work" / "batches" / "batch-partial" / "post-1.json").exists()
            )

    def test_batch_ids_are_safe(self):
        self.assertEqual(validate_batch_id("batch-abc_01"), "batch-abc_01")
        with self.assertRaises(BatchError):
            validate_batch_id("../outside")

    def test_prepare_batch_accepts_string_title_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            post = _post(115222, "Título string do WordPress")
            post["title"] = "Título string do WordPress"
            result = prepare_batch(BatchClient({115222: post}), self.config(), Path(directory), [115222], batch_id="batch-string-title")
            assert result["prepared"] == 1
            payload = json.loads(Path(result["editorial_input"]).read_text(encoding="utf-8"))
            assert payload["posts"][0]["title"] == "Título string do WordPress"

    def test_prepare_batch_rejects_invalid_title_type_as_contract_error(self):
        with tempfile.TemporaryDirectory() as directory:
            post = _post(115228, "Título")
            post["title"] = ["Título"]
            result = prepare_batch(BatchClient({115228: post}), self.config(), Path(directory), [115228], batch_id="batch-invalid-title")
            assert result["failed"] == 1
            assert result["items"][0]["error_class"] == "contract"

    def test_editorial_batch_rejects_duplicate_ids_before_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "editorial.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "batch_id": "batch-input",
                        "items": [
                            {"post_id": 1, "editorial": {}},
                            {"post_id": 1, "editorial": {}},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(BatchError):
                load_editorial_batch(path)

    def test_editorial_batch_accepts_single_inference_results_with_partial_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "editorial-results.json"
            path.write_text(json.dumps({
                "batch_id": "batch-results",
                "results": [
                    {"post_id": 1, "status": "ok", "editorial": {}},
                    {"post_id": 2, "status": "needs_retry", "reason": "fato"},
                ],
            }), encoding="utf-8")
            batch = load_editorial_batch(path)
            self.assertEqual(batch["items"][1]["status"], "needs_retry")
            self.assertEqual(batch["items"][1]["reason"], "fato")

    def test_editorial_batch_preserves_only_allowlisted_failure_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "editorial-errors.json"
            path.write_text(json.dumps({
                "batch_id": "batch-errors",
                "results": [
                    {
                        "post_id": 1,
                        "status": "needs_retry",
                        "failure_class": "api_http_error",
                        "retryable": False,
                        "http_status": 401,
                    },
                    {
                        "post_id": 2,
                        "status": "needs_retry",
                        "failure_class": "secret-provider-detail",
                        "retryable": "false",
                        "http_status": 700,
                    },
                    {
                        "post_id": 3,
                        "status": "needs_retry",
                        "failure_class": "rate_limited",
                        "retryable": True,
                        "http_status": 429,
                    },
                ],
            }), encoding="utf-8")

            items = load_editorial_batch(path)["items"]

        assert items[0]["failure_class"] == "api_http_error"
        assert items[0]["retryable"] is False
        assert items[0]["http_status"] == 401
        assert items[1]["failure_class"] == ""
        assert items[1]["retryable"] is True
        assert items[1]["http_status"] is None
        assert items[2]["failure_class"] == "rate_limited"
        assert items[2]["retryable"] is True
        assert items[2]["http_status"] == 429

    def test_media_resolve_batch_is_bounded_and_normalized(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "media.json"
            path.write_text(json.dumps({
                "batch_id": "media-test",
                "posts": [{
                    "post_id": 1,
                    "subject": "Obra",
                    "needed": 2,
                    "replacement_reserve": 2,
                }],
            }), encoding="utf-8")
            batch = load_media_resolve_batch(path)
            self.assertEqual(batch["posts"][0]["query"], "Obra")
            self.assertEqual(batch["posts"][0]["limit"], 3)
            self.assertEqual(batch["posts"][0]["role"], "")
            self.assertEqual(batch["posts"][0]["replacement_reserve"], 2)

    def test_media_resolve_batch_preserves_known_media_role(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "media.json"
            path.write_text(json.dumps({
                "batch_id": "media-role-test",
                "posts": [{
                    "post_id": 1,
                    "subject": "Obra",
                    "needed": 2,
                    "role": "inline",
                    "featured_required": False,
                }],
            }), encoding="utf-8")
            batch = load_media_resolve_batch(path)
        self.assertEqual(batch["posts"][0]["role"], "inline")
        self.assertFalse(batch["posts"][0]["featured_required"])

    def test_media_resolve_batch_rejects_more_than_two_posts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "media.json"
            path.write_text(json.dumps({
                "batch_id": "media-test",
                "posts": [
                    {"post_id": 1, "subject": "A"},
                    {"post_id": 2, "subject": "B"},
                    {"post_id": 3, "subject": "C"},
                ],
            }), encoding="utf-8")
            with self.assertRaises(BatchError):
                load_media_resolve_batch(path)

    def test_preflight_rejects_process_without_allowed_topic_before_media(self):
        config = replace(
            Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False),
            site_topics=("games", "anime"),
        )
        editorial = {
            "site_relevance": {
                "decision": "process",
                "confidence": 1.0,
                "reason": "parece editorial",
                "matched_topics": ["celebridades"],
            },
            "media_plan": [],
            "needs_trailer": False,
            "trailer_url": None,
            "game_name": None,
        }
        allowed, reason = preflight_apply_candidate(
            BatchClient({1: _post(1, "Post")}), config, 1, editorial
        )
        self.assertFalse(allowed)
        self.assertIn("matched_topics permitido", reason)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False)
            post = _post(1, "Post publicado")
            post["status"] = "publish"
            batch = {"schema_version": 1, "batch_id": "batch-preflight-status", "items": [{"post_id": 1, "editorial": {}}]}
            result = _apply_editorial_batch(BatchClient({1: post}), config, root, batch, dry_run=False, compact=True)
            self.assertEqual(result["posts"][0]["status"], "preflight_blocked")
            self.assertEqual(result["session"]["posts_touched"], [])

    def test_apply_batch_preflight_blocks_invalid_editorial_without_consuming_slot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False)
            batch = {"schema_version": 1, "batch_id": "batch-preflight-editorial", "items": [{"post_id": 1, "editorial": {}}]}
            result = _apply_editorial_batch(BatchClient({1: _post(1, "Post pendente")}), config, root, batch, dry_run=False, compact=True)
            self.assertEqual(result["posts"][0]["status"], "preflight_blocked")
            self.assertEqual(result["session"]["posts_touched"], [])

    def test_apply_batch_marks_one_lock_blocked_and_continues_sibling(self):
        from unicornio_editor.locking import LockError
        from unicornio_editor.workflow import WorkflowError

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False)
            batch = {
                "schema_version": 1,
                "batch_id": "batch-lock-continue",
                "items": [
                    {"post_id": 1, "editorial": {"placeholder": 1}},
                    {"post_id": 2, "editorial": {"placeholder": 2}},
                ],
            }

            def apply_one(_client, _config, _root, post_id, _editorial, **kwargs):
                if post_id == 1:
                    try:
                        raise LockError("post 1 is already locked")
                    except LockError as lock_error:
                        raise WorkflowError("post 1 is already being processed") from lock_error
                kwargs["on_work_started"]()
                return {"post_id": post_id, "status": "ready", "wordpress_changed": True}

            with mock.patch(
                "unicornio_editor.cli.preflight_apply_candidate", return_value=(True, "")
            ), mock.patch("unicornio_editor.cli.apply_editorial", side_effect=apply_one):
                result = _apply_editorial_batch(
                    BatchClient({1: _post(1, "Post um"), 2: _post(2, "Post dois")}),
                    config,
                    root,
                    batch,
                    dry_run=False,
                    compact=False,
                )

        self.assertEqual([row["status"] for row in result["posts"]], ["lock_blocked", "ready"])
        self.assertEqual(result["posts"][0]["error_code"], "post_lock_blocked")
        self.assertEqual(result["lock_blocked"], 1)
        self.assertEqual(result["session"]["posts_touched"], [2])
        self.assertEqual(result["session"]["posts_reserved"], [])

    def test_apply_batch_releases_reservation_when_work_never_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False)
            batch = {
                "schema_version": 1,
                "batch_id": "batch-no-start",
                "items": [{"post_id": 1, "editorial": {"placeholder": True}}],
            }
            fake_result = {
                "post_id": 1,
                "status": "noop",
                "dry_run": False,
                "wordpress_changed": False,
                "checklist": {"items": []},
                "media_plan_results": [],
            }
            with mock.patch(
                "unicornio_editor.cli.preflight_apply_candidate",
                return_value=(True, ""),
            ), mock.patch("unicornio_editor.cli.apply_editorial", return_value=fake_result):
                result = _apply_editorial_batch(
                    BatchClient({1: _post(1, "Post pendente")}),
                    config,
                    root,
                    batch,
                    dry_run=False,
                    compact=True,
                )
            self.assertEqual(result["session"]["posts_touched"], [])
            self.assertEqual(result["session"]["posts_reserved"], [])

    def test_uncertain_second_pass_releases_reservation_when_lock_stage_never_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False)
            batch = {
                "schema_version": 1,
                "batch_id": "batch-uncertain-no-start",
                "items": [{
                    "post_id": 1,
                    "status": "needs_retry",
                    "retry_mode": "uncertain_second_pass",
                    "reason": "evidência insuficiente",
                }],
            }
            with mock.patch("unicornio_editor.cli.read_state", return_value={"state": "uncertain", "attempts": 1}), mock.patch(
                "unicornio_editor.cli.uncertain_second_pass_eligible", return_value=True
            ), mock.patch(
                "unicornio_editor.cli.mark_uncertain",
                return_value={"post_id": 1, "status": "uncertain", "state": "uncertain", "wordpress_changed": False},
            ):
                result = _apply_editorial_batch(
                    BatchClient({1: _post(1, "Post pendente")}),
                    config,
                    root,
                    batch,
                    dry_run=False,
                    compact=True,
                )
            self.assertEqual(result["session"]["posts_touched"], [])
            self.assertEqual(result["session"]["posts_reserved"], [])

    def test_uncertain_second_pass_locks_before_read_and_continues_after_blocked_item(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False)
            posts = {}
            for post_id in (1, 2):
                post = _post(post_id, f"Post {post_id} incerto")
                post["meta"] = {"_hermes_state": "uncertain", "_hermes_attempts": "1"}
                posts[post_id] = post
            events = []

            class EventClient(BatchClient):
                def get_post(self, post_id):
                    events.append(("read", post_id))
                    return super().get_post(post_id)

            batch = {
                "schema_version": 1,
                "batch_id": "batch-uncertain-lock-order",
                "items": [
                    {"post_id": post_id, "status": "needs_retry", "retry_mode": "uncertain_second_pass"}
                    for post_id in (1, 2)
                ],
            }
            original_acquire = LockManager.acquire

            def tracked_acquire(manager, post_id):
                events.append(("acquire", post_id))
                return original_acquire(manager, post_id)

            def fake_mark_uncertain(_client, _config, _root, post_id, _reason, **kwargs):
                lock_handle = kwargs.get("lock_handle")
                events.append(("mark", post_id, lock_handle is not None))
                if lock_handle is not None:
                    lock_handle.assert_owned()
                kwargs["on_work_started"]()
                return {
                    "post_id": post_id,
                    "state": "awaiting_human",
                    "wordpress_changed": True,
                }

            with LockManager(root / "work" / "locks", ttl=900).acquire(1):
                events.clear()
                with mock.patch.object(LockManager, "acquire", new=tracked_acquire), mock.patch(
                    "unicornio_editor.cli.uncertain_second_pass_eligible", return_value=True
                ), mock.patch(
                    "unicornio_editor.cli.mark_uncertain", side_effect=fake_mark_uncertain
                ):
                    result = _apply_editorial_batch(
                        EventClient(posts), config, root, batch, dry_run=False, compact=True
                    )

            self.assertEqual([row["status"] for row in result["posts"]], ["lock_blocked", "awaiting_human"])
            self.assertEqual(result["lock_blocked"], 1)
            self.assertEqual(result["session"]["posts_touched"], [2])
            self.assertNotIn(("read", 1), events)
            self.assertLess(events.index(("acquire", 2)), events.index(("read", 2)))
            self.assertIn(("mark", 2, True), events)

    def test_uncertain_second_pass_does_not_commit_touch_after_lock_is_lost(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False)
            post = _post(9, "Post incerto")
            post["meta"] = {"_hermes_state": "uncertain", "_hermes_attempts": "1"}
            batch = {
                "schema_version": 1,
                "batch_id": "batch-uncertain-lock-lost",
                "items": [{
                    "post_id": 9,
                    "status": "needs_retry",
                    "retry_mode": "uncertain_second_pass",
                }],
            }

            def lose_lock_before_work(_client, _config, _root, _post_id, _reason, **kwargs):
                kwargs["lock_handle"].release()
                kwargs["on_work_started"]()
                return {"post_id": 9, "state": "awaiting_human", "wordpress_changed": True}

            with mock.patch(
                "unicornio_editor.cli.uncertain_second_pass_eligible", return_value=True
            ), mock.patch(
                "unicornio_editor.cli.mark_uncertain", side_effect=lose_lock_before_work
            ):
                result = _apply_editorial_batch(
                    BatchClient({9: post}), config, root, batch, dry_run=False, compact=True
                )

            self.assertEqual(result["posts"][0]["status"], "lock_blocked")
            self.assertEqual(result["session"]["posts_touched"], [])
            self.assertEqual(result["session"]["posts_reserved"], [])

    def test_apply_batch_isolates_wordpress_error_during_preflight(self):
        class FlakyClient(BatchClient):
            def get_post(self, post_id):
                if post_id == 2:
                    raise WordPressError("transient REST failure")
                return _post(post_id, f"Post {post_id}")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False)
            batch = {
                "schema_version": 1,
                "batch_id": "batch-preflight-error",
                "items": [
                    {"post_id": 1, "editorial": {}},
                    {"post_id": 2, "editorial": {}},
                    {"post_id": 3, "editorial": {}},
                ],
            }
            def fake_result(client, config, root, post_id, payload, **kwargs):
                callback = kwargs.get("on_work_started")
                if callback is not None:
                    callback()
                return {
                    "post_id": post_id,
                    "status": "ready",
                    "dry_run": False,
                    "wordpress_changed": True,
                    "checklist": {"items": []},
                    "media_plan_results": [],
                }

            with mock.patch("unicornio_editor.cli.validate_editorial", return_value={}), mock.patch(
                "unicornio_editor.cli.apply_editorial", side_effect=fake_result
            ) as applied:
                result = _apply_editorial_batch(
                    FlakyClient({}), config, root, batch, dry_run=False, compact=True
                )
            self.assertEqual([item["status"] for item in result["posts"]], ["ready", "preflight_error", "ready"])
            self.assertEqual(result["preflight_errors"], 1)
            self.assertEqual(result["preflight_blocked"], 0)
            self.assertEqual(applied.call_args_list[0].args[3], 1)
            self.assertEqual(applied.call_args_list[1].args[3], 3)
            self.assertEqual(result["session"]["posts_touched"], [1, 3])

    def test_apply_editorial_batch_isolates_results_per_post(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch = {
                "schema_version": 1,
                "batch_id": "batch-apply-test",
                "items": [
                    {"post_id": 1, "editorial": {}},
                    {"post_id": 2, "editorial": {}},
                ],
            }
            config = self.config()
            fake_result = lambda client, config, root, post_id, payload: {
                "post_id": post_id,
                "status": "ready",
                "dry_run": True,
                "wordpress_changed": False,
                "checklist": {"items": []},
                "media_plan_results": [],
            }
            with mock.patch("unicornio_editor.cli.apply_editorial", side_effect=fake_result):
                result = _apply_editorial_batch(
                    mock.Mock(), config, root, batch, dry_run=True, compact=True
                )
            self.assertEqual(result["processed"], 2)
            self.assertEqual([item["post_id"] for item in result["posts"]], [1, 2])
            self.assertTrue(
                (root / "work" / "batches" / "batch-apply-test" / "apply.manifest.json").exists()
            )

    def test_editorial_batch_keeps_needs_retry_item_out_of_apply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch = {
                "schema_version": 1,
                "batch_id": "batch-partial-editorial",
                "items": [
                    {"post_id": 1, "status": "ok", "editorial": {}},
                    {"post_id": 2, "status": "needs_retry", "reason": "fato inconsistente"},
                ],
            }
            config = self.config()
            fake_result = lambda client, config, root, post_id, payload: {
                "post_id": post_id,
                "status": "ready",
                "dry_run": True,
                "wordpress_changed": False,
                "checklist": {"items": []},
                "media_plan_results": [],
            }
            with mock.patch("unicornio_editor.cli.apply_editorial", side_effect=fake_result) as applied:
                result = _apply_editorial_batch(
                    mock.Mock(), config, root, batch, dry_run=True, compact=True
                )
            self.assertEqual(result["processed"], 2)
            self.assertEqual(result["needs_retry"], 1)
            self.assertEqual(applied.call_count, 1)
            self.assertEqual(result["posts"][1]["status"], "needs_retry")

    def test_uncertain_second_pass_needs_retry_escalates_and_consumes_touch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            post = _post(9, "Post incerto")
            post["meta"] = {"_hermes_state": "uncertain", "_hermes_attempts": "1"}
            client = BatchClient({9: post})
            batch = {
                "schema_version": 1,
                "batch_id": "batch-uncertain-retry",
                "items": [{
                    "post_id": 9,
                    "status": "needs_retry",
                    "retry_mode": "uncertain_second_pass",
                    "reason": "segunda avaliação sem decisão",
                }],
            }
            result = _apply_editorial_batch(
                client, Config("wordpress", "http://wp.test", "/wp-json/wp/v2", dry_run=False),
                root, batch, dry_run=False, compact=True,
            )
            self.assertEqual(result["posts"][0]["state"], "awaiting_human")
            self.assertEqual(client.posts[9]["meta"]["_hermes_attempts"], "2")
            self.assertEqual(client.posts[9]["status"], "awaiting_human")

    def test_retry_same_batch_does_not_reapply_ready_item(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "backups" / "1" / "apply.latest.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"batch_id": "batch-idempotent", "status": "ready"}), encoding="utf-8")
            batch = {
                "schema_version": 1,
                "batch_id": "batch-idempotent",
                "items": [{"post_id": 1, "editorial": {}}],
            }
            with mock.patch("unicornio_editor.cli.apply_editorial") as applied:
                result = _apply_editorial_batch(
                    mock.Mock(), self.config(), root, batch, dry_run=False, compact=True
                )
            self.assertEqual(result["noop"], 1)
            self.assertFalse(applied.called)
            self.assertTrue(result["posts"][0]["idempotent"])

    def test_stale_uncertain_batch_item_is_noop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            post = _post(1, "Post incerto")
            post["meta"] = {"_hermes_state": "awaiting_human", "_hermes_attempts": "2"}
            client = BatchClient({1: post})
            batch = {
                "schema_version": 1,
                "batch_id": "batch-stale-uncertain",
                "items": [{"post_id": 1, "status": "needs_retry", "retry_mode": "uncertain_second_pass"}],
            }
            result = _apply_editorial_batch(client, self.config(), root, batch, dry_run=False, compact=True)
            self.assertEqual(result["posts"][0]["status"], "noop")
            self.assertTrue(result["posts"][0]["stale"])
            self.assertEqual(client.posts[1]["meta"]["_hermes_attempts"], "2")

    def test_media_manifest_is_joined_into_empty_editorial_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_id = "batch-media-join"
            manifest = root / "media.manifest.json"
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "batch_id": batch_id,
                "posts": [{
                    "post_id": 1,
                    "subject": "Hollow Knight Silksong",
                    "query": "Hollow Knight Silksong gameplay",
                    "needed": 3,
                    "reuse": [],
                    "candidates": [
                        {"url": "https://img.test/featured.webp", "source": "https://source.test/article", "verdict": "deterministic_match", "candidate_id": "c1"},
                        {"url": "https://img.test/inline-one.webp", "source": "https://source.test/article", "verdict": "deterministic_match", "candidate_id": "c2"},
                        {"url": "https://img.test/inline-two.webp", "source": "https://source.test/article", "verdict": "deterministic_match", "candidate_id": "c3"},
                    ],
                }],
            }), encoding="utf-8")
            editorial = {
                "post_id": 1,
                "status": "ok",
                "editorial": {
                    "cleaned_html": "".join(
                        f"<p>Paragrafo substantivo {index} com bastante texto editorial para validar o espaçamento das imagens.</p>"
                        for index in range(8)
                    ),
                    "media_plan": [],
                },
            }
            merged, report = merge_media_manifest_into_editorial({
                "schema_version": 1,
                "batch_id": batch_id,
                "items": [editorial],
            }, manifest)

            plan = merged["items"][0]["editorial"]["media_plan"]
            self.assertEqual(len(plan), 3)
            self.assertTrue(plan[0]["is_featured"])
            self.assertFalse(plan[1]["is_featured"])
            self.assertEqual(plan[0]["source_page_url"], "https://source.test/article")
            self.assertEqual(plan[0]["candidate_id"], "c1")
            self.assertGreaterEqual(plan[2]["paragraph_index"] - plan[1]["paragraph_index"], 3)
            self.assertEqual(report["assembled"], 1)

    def test_apply_batch_isolates_four_inline_images_with_only_three_safe_slots(self):
        paragraphs = (
            "<p>Texto editorial substantivo sobre videogame e lançamento importante, com detalhes"
            " confirmados sobre plataformas, características e o conteúdo da notícia.</p>"
        )
        splittable = (
            "<p>Uma parte substantiva sobre videogame com detalhes confirmados sobre as plataformas "
            "e todas as novidades apresentadas neste lançamento importante. "
            "A segunda parte da notícia preserva o texto editorial e apresenta outros detalhes "
            "sobre as características já confirmadas ao longo da reportagem.</p>"
        )
        candidate_post = 116068
        sibling_post = 116070
        results = [{
            "post_id": post_id,
            "editorial": {
                "site_relevance": {
                    "decision": "process",
                    "confidence": 0.99,
                    "reason": "Notícia sobre videogame",
                    "matched_topics": ["games"],
                },
                "cleaned_html": body,
                "media_plan": [],
                "game_name": None,
                "needs_trailer": False,
                "trailer_url": None,
                "seo": {
                    "title": "Notícia sobre videogame e lançamento importante",
                    "meta_description": (
                        "Uma descrição suficientemente longa sobre o conteúdo de videogame, "
                        "seus detalhes, plataformas e contexto para o leitor entender a notícia."
                    ),
                    "focus_keyword": "videogame",
                },
            },
        } for post_id, body in [(candidate_post, paragraphs * 9), (sibling_post, splittable * 9)]]
        config = replace(self.config(), dry_run=False, max_posts_touched_per_run=1)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_path = root / "editorial.json"
            batch_path.write_text(json.dumps({"batch_id": "slot-isolation", "results": results}))
            manifest = root / "media.manifest.json"
            manifest.write_text(json.dumps({
                "batch_id": "slot-isolation",
                "posts": [{
                    "post_id": post_id,
                    "subject": "Videogame",
                    "needed": 4,
                    "featured_required": False,
                    "candidates": [{
                        "url": f"https://img.test/{post_id}/{index}.webp",
                        "source": "https://source.test/article",
                        "verdict": "deterministic_match",
                    } for index in range(4)],
                } for post_id in (candidate_post, sibling_post)],
            }))
            client = BatchClient({post_id: _post(post_id, "Videogame")
                                  for post_id in (candidate_post, sibling_post)})
            original_blocked_post = json.dumps(client.get_post(candidate_post), sort_keys=True)
            applied = []

            def apply_one(wp, _config, _root, post_id, editorial, *, on_work_started,
                          media_layout_source_signature, **_):
                applied.append(post_id)
                self.assertTrue(media_layout_source_signature)
                self.assertEqual(editorial["cleaned_html"].count("<p>"), 11)
                slots = [row["paragraph_index"] for row in editorial["media_plan"]]
                self.assertEqual(len(slots), 4)
                self.assertTrue(all(b - a >= 3 for a, b in zip(slots, slots[1:])))
                on_work_started()
                wp.update_post(post_id, {"meta": {"_hermes_state": "ready"}})
                return {"post_id": post_id, "status": "ready", "wordpress_changed": True}

            stdout, stderr = io.StringIO(), io.StringIO()
            with mock.patch("unicornio_editor.cli.load_config", return_value=config), mock.patch(
                "unicornio_editor.cli.WordPressClient", return_value=client
            ), mock.patch("unicornio_editor.cli._carregar_env_do_projeto"), mock.patch(
                "unicornio_editor.cli.apply_editorial", side_effect=apply_one
            ), redirect_stdout(stdout), redirect_stderr(stderr):
                exit_code = main(["apply-batch", str(batch_path), "--root", str(root), "--compact"])

            self.assertEqual(exit_code, 0, stderr.getvalue())
            summary = json.loads(stdout.getvalue())
            self.assertEqual([row["post_id"] for row in summary["posts"]], [candidate_post, sibling_post])
            blocked, ready = summary["posts"]
            self.assertEqual(blocked["status"], "preflight_blocked")
            self.assertEqual(blocked["error_code"], "insufficient_paragraph_slots")
            self.assertEqual(blocked["action_code"], "editorial_restructure_required")
            self.assertIn("requested=4, available=3, minimum_spacing=3", blocked["reason"])
            self.assertFalse(blocked["wordpress_changed"])
            self.assertEqual(ready["status"], "ready", ready)
            self.assertEqual(summary["ready"], 1)
            self.assertEqual(summary["preflight_blocked"], 1)
            self.assertEqual(summary["session"]["posts_touched"], [sibling_post])
            self.assertEqual(summary["session"]["posts_reserved"], [])
            self.assertEqual(summary["media_plan_assembly"]["layout_blocked"][0]["post_id"], candidate_post)
            self.assertEqual(summary["media_plan_assembly"]["normalization"][0]["post_id"], sibling_post)
            self.assertEqual(json.dumps(client.get_post(candidate_post), sort_keys=True), original_blocked_post)
            self.assertFalse((root / "backups" / str(candidate_post)).exists())
            self.assertEqual(applied, [sibling_post])

    def test_fresh_flat_article_uses_two_safe_splits_to_place_four_inline_images(self):
        first = (
            "Uma parte substantiva sobre videogame com detalhes confirmados sobre as plataformas "
            "e todas as novidades apresentadas neste lançamento importante."
        )
        second = (
            "A segunda parte da notícia preserva o texto editorial e apresenta outros detalhes "
            "sobre as características já confirmadas ao longo da reportagem."
        )
        anchors = '<p id="secao-fixa">Detalhes <a href="/fonte">da fonte original</a>.</p>'
        original_body = "".join(f"<p>{first} {second}</p>" for _ in range(8)) + anchors
        expected_text = " ".join(re.sub(r"<[^>]+>", " ", original_body).split())
        batch = {
            "batch_id": "safe-layout-normalization",
            "items": [{"post_id": 1, "editorial": {"cleaned_html": original_body, "media_plan": []}}],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "media.manifest.json"
            manifest.write_text(json.dumps({
                "batch_id": batch["batch_id"],
                "posts": [{
                    "post_id": 1,
                    "subject": "Videogame",
                    "needed": 4,
                    "featured_required": False,
                    "candidates": [{
                        "url": f"https://img.test/inline-{index}.webp",
                        "source": "https://source.test/article",
                        "verdict": "deterministic_match",
                    } for index in range(4)],
                }],
            }))
            for name in ("fresh", "partial-marker", "partial-artifact", "existing-image", "listicle",
                         "opaque-paragraph"):
                with self.subTest(name=name):
                    post = _post(1, "Videogame")
                    partial_file = root / "backups" / "1" / "editorial.partial.json"
                    if name == "partial-marker":
                        post["meta"]["_hermes_state"] = "partial"
                    if name == "partial-artifact":
                        partial_file.parent.mkdir(parents=True)
                        partial_file.write_text("{}")
                    if name == "existing-image":
                        post["content"]["raw"] += '<img src="https://img.test/existing.webp">'
                    body = original_body
                    if name == "listicle":
                        body = "<h2>1. Parte um</h2>" + body + "<h2>2. Parte dois</h2>"
                    if name == "opaque-paragraph":
                        body = "<!-- " + body + " -->"
                    batch["items"][0]["editorial"]["cleaned_html"] = body
                    resolved = resolve_batch_editorial_defaults(BatchClient({1: post}), batch, root=root)
                    merged, report = merge_media_manifest_into_editorial(resolved, manifest)
                    item = merged["items"][0]
                    if name == "partial-artifact":
                        partial_file.unlink()
                    if name != "fresh":
                        self.assertTrue(report["layout_blocked"], name)
                        self.assertFalse(report["normalization"])
                        self.assertEqual(item["editorial"]["cleaned_html"], body)
                        self.assertNotIn("media_layout_source_signature", item)
                        continue
                    result_body = item["editorial"]["cleaned_html"]
                    self.assertEqual(" ".join(re.sub(r"<[^>]+>", " ", result_body).split()), expected_text)
                    self.assertIn(anchors, result_body)
                    self.assertNotEqual(result_body, original_body)
                    plan = item["editorial"]["media_plan"]
                    slots = [row["paragraph_index"] for row in plan]
                    self.assertEqual(len(slots), 4)
                    self.assertTrue(all(b - a >= 3 for a, b in zip(slots, slots[1:])))
                    audit = report["normalization"][0]
                    self.assertEqual((audit["original_paragraphs"], audit["final_paragraphs"]), (9, 11))
                    self.assertEqual(audit["original_words"], audit["final_words"])
                    self.assertEqual(len(audit["splits"]), 2)
                    self.assertEqual(item["media_layout_source_signature"], item["fresh_layout_source_signature"])

    def test_media_manifest_still_rejects_other_unsafe_layout_errors(self):
        batch = {
            "batch_id": "invalid-layout",
            "items": [
                {
                    "post_id": 1,
                    "editorial": {"cleaned_html": "<p>Texto sem lead suficiente.</p>", "media_plan": []},
                },
                {"post_id": 2, "editorial": {"media_plan": [{"candidate_id": "unchanged"}]}},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "media.manifest.json"
            manifest.write_text(json.dumps({
                "batch_id": "invalid-layout",
                "posts": [{
                    "post_id": 1,
                    "subject": "Videogame",
                    "needed": 1,
                    "featured_required": False,
                    "candidates": [{
                        "url": "https://img.test/one.webp",
                        "source": "https://source.test/article",
                        "verdict": "deterministic_match",
                    }],
                }],
            }))

            with self.assertRaisesRegex(BatchError, "two substantive paragraphs") as caught:
                merge_media_manifest_into_editorial(batch, manifest)
        self.assertEqual(caught.exception.__cause__.code, "insufficient_substantive_paragraphs")

    def test_inline_only_media_manifest_never_promotes_first_inline_to_featured(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_id = "batch-inline-only"
            manifest = root / "media.manifest.json"
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "batch_id": batch_id,
                "posts": [{
                    "post_id": 1,
                    "subject": "Hollow Knight Silksong",
                    "query": "Hollow Knight Silksong",
                    "needed": 2,
                    "role": "inline",
                    "featured_required": False,
                    "reuse": [],
                    "candidates": [
                        {"url": f"https://img.test/inline-{index}.webp",
                         "source": "https://source.test/article",
                         "verdict": "deterministic_match",
                         "candidate_id": f"inline-{index}"}
                        for index in range(2)
                    ],
                }],
            }), encoding="utf-8")
            editorial = {
                "post_id": 1,
                "status": "ok",
                "editorial": {
                    "cleaned_html": "".join(
                        f"<p>Paragrafo substantivo {index} com texto suficiente para validar a inserção das imagens no artigo.</p>"
                        for index in range(8)
                    ),
                    "media_plan": [],
                },
            }

            merged, _ = merge_media_manifest_into_editorial({
                "schema_version": 1,
                "batch_id": batch_id,
                "items": [editorial],
            }, manifest)

            plan = merged["items"][0]["editorial"]["media_plan"]
            self.assertEqual(len(plan), 2)
            self.assertTrue(all(item["is_featured"] is False for item in plan))

    def test_compact_candidate_preserves_only_origin_evidence_fields(self):
        candidate = _media_candidate({
            "candidate_id": "opaque-cdn-1",
            "direct_image_url": "https://cdn.test/991.webp",
            "source_page_url": "https://publisher.test/article-123",
            "credit_text": "Crédito da imagem: Publisher.",
            "evidence_score": 9,
            "evidence": {
                "subject": "Metroid Prime 4",
                "verdict": "deterministic_match",
                "score": 9,
                "local_score": 5,
                "matched": ["page_title", "alt_original"],
                "evidence": {
                    "page_title": "Metroid Prime 4 key art",
                    "alt_original": "Metroid Prime 4 artwork",
                    "query": "private query should not be persisted as evidence",
                },
            },
        })

        self.assertEqual(candidate["source_page_url"], "https://publisher.test/article-123")
        self.assertEqual(candidate["origin_evidence"]["fields"]["alt_original"], "Metroid Prime 4 artwork")
        self.assertNotIn("query", candidate["origin_evidence"]["fields"])

    def test_manifest_bridge_carries_origin_evidence_into_the_media_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_id = "batch-origin-evidence"
            manifest = root / "media.manifest.json"
            evidence = {
                "verdict": "deterministic_match",
                "score": 9,
                "local_score": 5,
                "subject": "Metroid Prime 4",
                "matched": ["page_title", "alt_original"],
                "fields": {
                    "page_title": "Metroid Prime 4 key art",
                    "alt_original": "Metroid Prime 4 artwork",
                    "page_url": "https://publisher.test/article-123",
                },
            }
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "batch_id": batch_id,
                "posts": [{
                    "post_id": 1,
                    "subject": "Metroid Prime 4",
                    "query": "Metroid Prime 4",
                    "needed": 1,
                    "featured_required": True,
                    "candidates": [{
                        "url": "https://cdn.test/991.webp",
                        "source": "https://publisher.test/article-123",
                        "verdict": "deterministic_match",
                        "candidate_id": "opaque-cdn-1",
                        "origin_evidence": evidence,
                    }],
                }],
            }), encoding="utf-8")
            editorial = {
                "post_id": 1,
                "status": "ok",
                "editorial": {"cleaned_html": "", "media_plan": []},
            }

            merged, _ = merge_media_manifest_into_editorial({
                "schema_version": 1,
                "batch_id": batch_id,
                "items": [editorial],
            }, manifest)

            planned = merged["items"][0]["editorial"]["media_plan"][0]
            self.assertEqual(planned["origin_evidence"]["fields"]["page_title"], "Metroid Prime 4 key art")

    def test_manifest_keeps_replacement_candidates_outside_required_slots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_id = "batch-media-reserve"
            manifest = root / "media.manifest.json"
            candidates = [
                {
                    "url": f"https://img.test/candidate-{index}.webp",
                    "source": "https://source.test/article",
                    "verdict": "deterministic_match",
                    "candidate_id": f"candidate-{index}",
                }
                for index in range(3)
            ]
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "batch_id": batch_id,
                "posts": [{
                    "post_id": 1,
                    "subject": "Game",
                    "query": "Game",
                    "needed": 1,
                    "replacement_reserve": 2,
                    "featured_required": False,
                    "candidates": candidates,
                }],
            }), encoding="utf-8")
            editorial = {
                "post_id": 1,
                "status": "ok",
                "editorial": {
                    "cleaned_html": "".join(
                        f"<p>Paragrafo substantivo {index} com texto editorial suficiente para validar espaços de mídia no artigo e manter a leitura fluida.</p>"
                        for index in range(8)
                    ),
                    "media_plan": [],
                },
            }

            merged, _ = merge_media_manifest_into_editorial({
                "schema_version": 1,
                "batch_id": batch_id,
                "items": [editorial],
            }, manifest)

            item = merged["items"][0]
            self.assertEqual(len(item["editorial"]["media_plan"]), 1)
            self.assertEqual(len(item["media_fallback_candidates"]), 2)

    def test_manifest_bridge_preserves_list_item_identity_and_scoped_reserves(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            batch_id = "batch-list-target-bridge"
            manifest = root / "media.manifest.json"
            html = (
                "<h2>1. Metroid Prime 4: exploração</h2><p>Descrição do primeiro item.</p>"
                "<h2>2. Hades II: ação</h2><p>Descrição do segundo item.</p>"
            )

            def candidate(target_id, name):
                return {
                    "candidate_id": f"{target_id}-{name}",
                    "url": f"https://cdn.test/{target_id}-{name}.webp",
                    "source": f"https://source.test/{target_id}-{name}",
                    "verdict": "deterministic_match",
                    "credit_text": f"Crédito da imagem: Fonte {name}.",
                }

            targets = []
            for number, subject in ((1, "Metroid Prime 4: exploração"), (2, "Hades II: ação")):
                identity = f"root:number-{number}"
                targets.append({
                    "target_id": identity,
                    "role": "inline",
                    "subject": subject,
                    "query": subject,
                    "paragraph_index": number - 1,
                    "item_identity": identity,
                    "item_title": subject,
                    "item_group": "",
                    "item_number": number,
                    "section_heading": subject,
                    "section_slot": number - 1,
                    "html_position": number * 10,
                    "replacement_reserve": 1,
                    "reuse": [],
                    "candidates": [candidate(identity, "primary"), candidate(identity, "reserve")],
                })
            targets.append({
                "target_id": "featured",
                "role": "featured",
                "subject": "Jogos",
                "query": "Jogos key art",
                "replacement_reserve": 1,
                "reuse": [],
                "candidates": [candidate("featured", "primary"), candidate("featured", "reserve")],
            })
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "batch_id": batch_id,
                "posts": [{
                    "post_id": 55,
                    "subject": "Jogos",
                    "query": "Jogos",
                    "needed": 3,
                    "featured_required": True,
                    "required_inline_images": 2,
                    "inline_deficit": 2,
                    "targets": targets,
                }],
            }), encoding="utf-8")
            batch = {
                "schema_version": 1,
                "batch_id": batch_id,
                "items": [{
                    "post_id": 55,
                    "status": "ok",
                    "editorial": {"cleaned_html": html, "media_plan": []},
                }],
            }

            merged, report = merge_media_manifest_into_editorial(batch, manifest)

            item = merged["items"][0]
            plan = item["editorial"]["media_plan"]
            self.assertEqual(len(plan), 3)
            inline = [row for row in plan if not row["is_featured"]]
            self.assertEqual([row["item_identity"] for row in inline], [
                "root:number-1", "root:number-2",
            ])
            self.assertEqual([row["target_id"] for row in inline], [
                "root:number-1", "root:number-2",
            ])
            self.assertEqual(len(item["media_fallback_candidates"]), 3)
            self.assertEqual(report["fallback_candidates"], 3)

    def test_batch_defaults_restore_original_html_when_model_omits_rewrite(self):
        post = _post(1, "Videogame sem reescrita")
        post["content"] = {"raw": "<article><p>Videogame sem reescrita preserva o conteúdo original.</p></article>"}
        client = BatchClient({1: post})
        batch = {
            "schema_version": 1,
            "batch_id": "batch-defaults",
            "items": [{
                "post_id": 1,
                "status": "ok",
                "editorial": {
                    "cleaned_html": None,
                    "seo": None,
                    "needs_trailer": None,
                    "trailer_url": None,
                },
            }],
        }

        resolved = resolve_batch_editorial_defaults(client, batch)
        editorial = resolved["items"][0]["editorial"]
        self.assertTrue(editorial["cleaned_html"])
        self.assertIn("Videogame sem reescrita", editorial["cleaned_html"])
        self.assertIsInstance(editorial["seo"], dict)
        self.assertFalse(editorial["needs_trailer"])

    def test_media_manifest_does_not_promote_ambiguous_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = root / "media.manifest.json"
            manifest.write_text(json.dumps({
                "schema_version": 1,
                "batch_id": "batch-ambiguous",
                "posts": [{
                    "post_id": 1,
                    "subject": "Silksong",
                    "query": "Silksong",
                    "needed": 1,
                    "candidates": [{"url": "https://img.test/a.webp", "source": "https://source.test/a", "verdict": "ambiguous"}],
                }],
            }), encoding="utf-8")
            merged, report = merge_media_manifest_into_editorial({
                "schema_version": 1,
                "batch_id": "batch-ambiguous",
                "items": [{"post_id": 1, "status": "ok", "editorial": {"media_plan": []}}],
            }, manifest)
            self.assertEqual(merged["items"][0]["editorial"]["media_plan"], [])
            self.assertEqual(report["missing_candidates"], [1])
            self.assertEqual(report["rejected_candidates"], 1)


if __name__ == "__main__":
    unittest.main()
