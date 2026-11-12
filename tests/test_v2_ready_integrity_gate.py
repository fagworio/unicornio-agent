import hashlib
import json

import pytest

from unicornio_editor.pipeline_v2.model import LifecycleState, Outcome, Phase, WorkState
from unicornio_editor.pipeline_v2.production import editorial_fingerprint
from unicornio_editor.pipeline_v2.runtime import WordPressWriterV2


class NoWriteClient:
    def __init__(self, post_id):
        self.post = {
            "id": post_id,
            "status": "pending",
            "title": {"raw": "Título antigo"},
            "content": {"raw": "conteúdo antigo"},
            "featured_media": 0,
            "meta": {},
        }
        self.updates = []

    def get_post(self, _post_id):
        return self.post

    def update_post(self, _post_id, payload):
        self.updates.append(payload)
        return self.post


def _valid_payloads():
    editorial = {
        "decision": "process",
        "site_relevance": {"decision": "process", "confidence": 0.99},
        "cleaned_html": "<p>Texto editorial validado para o teste.</p>",
        "title": "Título editorial aprovado",
        "seo": {
            "title": "Título SEO aprovado",
            "meta_description": "Descrição SEO longa para atender ao requisito de tamanho e validar a integridade do payload antes de qualquer tentativa de gravação no WordPress.",
            "focus_keyword": "integridade editorial",
        },
    }
    content = "<p>Texto final validado para o artigo de teste.</p>"
    editorial_hash = editorial_fingerprint(editorial)
    candidate = {
        "_v2_run_id": "candidate-run",
        "content": content,
        "editorial": editorial,
        "editorial_hash": editorial_hash,
        "seo": dict(editorial["seo"]),
        "title": editorial["title"],
        "featured_media": None,
    }
    validation = {
        "_v2_run_id": "writer-run",
        "candidate_run_id": "candidate-run",
        "candidate_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "editorial_hash": editorial_hash,
        "passed": True,
        "failures": [],
    }
    return candidate, validation


@pytest.mark.parametrize(
    "invalid_case",
    [
        "missing_candidate",
        "empty_content",
        "missing_validation",
        "validation_not_passed",
        "failure_present",
        "failure_list_missing",
        "candidate_hash_mismatch",
        "editorial_hash_mismatch",
        "candidate_editorial_hash_mismatch",
        "missing_editorial",
        "missing_title",
        "mismatched_title",
        "mismatched_seo",
        "mismatched_candidate_run_id",
    ],
)
def test_ready_integrity_failures_reject_before_wordpress_or_journal_writes(tmp_path, invalid_case):
    post_id = 901
    backup = tmp_path / "backups" / str(post_id)
    backup.mkdir(parents=True)
    candidate, validation = _valid_payloads()

    if invalid_case == "empty_content":
        candidate["content"] = "  \n"
        validation["candidate_hash"] = hashlib.sha256(candidate["content"].encode()).hexdigest()
    elif invalid_case == "missing_validation":
        validation = None
    elif invalid_case == "validation_not_passed":
        validation["passed"] = False
    elif invalid_case == "failure_present":
        validation["failures"] = [{"gate": "seo"}]
    elif invalid_case == "failure_list_missing":
        validation.pop("failures")
    elif invalid_case == "candidate_hash_mismatch":
        validation["candidate_hash"] = "0" * 64
    elif invalid_case == "editorial_hash_mismatch":
        validation["editorial_hash"] = "0" * 64
    elif invalid_case == "candidate_editorial_hash_mismatch":
        candidate["editorial_hash"] = "0" * 64
    elif invalid_case == "missing_editorial":
        candidate.pop("editorial")
    elif invalid_case == "missing_title":
        candidate.pop("title")
    elif invalid_case == "mismatched_title":
        candidate["title"] = "Título sem correspondência"
    elif invalid_case == "mismatched_seo":
        candidate["seo"]["focus_keyword"] = "diferente"
    elif invalid_case == "mismatched_candidate_run_id":
        validation["candidate_run_id"] = "outro-run"

    if invalid_case != "missing_candidate":
        (backup / "editorial.candidate.json").write_text(
            json.dumps(candidate), encoding="utf-8"
        )
    if validation is not None:
        (backup / "editorial.validation.json").write_text(
            json.dumps(validation), encoding="utf-8"
        )

    client = NoWriteClient(post_id)
    state = WorkState(
        state=LifecycleState.READY,
        phase=Phase.VALIDATE,
        relevance_approved=True,
    )

    with pytest.raises(RuntimeError):
        WordPressWriterV2(client, tmp_path).commit(
            post_id,
            {"post_id": post_id, "v2_run_id": "writer-run", "post": client.post},
            state,
            Outcome.ready(),
        )

    assert client.updates == []
    assert not (tmp_path / "work" / "v2-journal").exists()
