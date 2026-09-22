"""Testes de regressão da quinta rodada (correções curtas antes de congelar).

Itens:

1. `cost_guard` NÃO pode somar o main-loop duas vezes (o main-loop também
   aparece em `session_model_usage` com `task=''`);
2. `prompt_tokens` do guard passa a ser o GRAND TOTAL (main + auxiliar);
3. `media_decisions.json` legado não pode virar a "última decisão" quando o JSONL
   já tem registro do post;
4. `decision_id` ligado causalmente: media-validate por ITEM e apply com os ids
   do plano (sem atribuir o plano misto a uma decisão só);
5. memo do resolver guardado com o resultado COMPLETO também na chave original.
"""

import io
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from unicornio_editor.config import Config
from unicornio_editor.observability import (
    append_telemetry,
    read_media_decision,
    read_telemetry_summary,
    record_media_decision,
)

CRON_ENV = {"UNICORNIO_RUN_SOURCE": "cron",
            "HERMES_SESSION_ID": "cron_9e39343dc6f5_20260922_090524"}


def _config(**overrides):
    values = {
        "content_source": "wordpress",
        "wordpress_url": "http://wp.test",
        "wordpress_api_base": "/wp-json/wp/v2",
        "dry_run": False,
    }
    values.update(overrides)
    return Config(**values)


def _guard():
    import importlib.util

    caminho = Path(__file__).resolve().parents[1] / "hermes" / "cost_guard.py"
    spec = importlib.util.spec_from_file_location("cost_guard_r5", caminho)
    modulo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(modulo)
    return modulo


class GuardDoubleCountTests(unittest.TestCase):
    """O custo do main-loop aparece em `sessions` E em session_model_usage."""

    def _banco(self, root: Path) -> Path:
        banco = root / "state.db"
        db = sqlite3.connect(banco)
        db.execute(
            "CREATE TABLE sessions (id TEXT, source TEXT, started_at INTEGER, "
            "api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
            "cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
            "reasoning_tokens INTEGER, estimated_cost_usd REAL)"
        )
        db.execute(
            "CREATE TABLE session_model_usage (session_id TEXT, model TEXT, task TEXT, "
            "api_call_count INTEGER, input_tokens INTEGER, output_tokens INTEGER, "
            "cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
            "reasoning_tokens INTEGER, estimated_cost_usd REAL)"
        )
        agora = int(time.time())
        db.execute(
            "INSERT INTO sessions VALUES ('cron_9e39343dc6f5_20260922_090524','cron',?,"
            "40,1000,200,9000,0,0,0.20)",
            (agora,),
        )
        # main-loop ESPELHADO em session_model_usage (task=''): NÃO pode somar.
        db.execute(
            "INSERT INTO session_model_usage VALUES "
            "('cron_9e39343dc6f5_20260922_090524','deepseek','',40,1000,200,9000,0,0,0.20)"
        )
        # auxiliar de verdade: 1000+500=1500 prompt tokens, US$ 0,05.
        db.execute(
            "INSERT INTO session_model_usage VALUES "
            "('cron_9e39343dc6f5_20260922_090524','vision','vision',3,1000,300,500,0,0,0.05)"
        )
        db.commit()
        db.close()
        return banco

    def test_custo_nao_conta_o_main_loop_duas_vezes(self):
        modulo = _guard()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            banco = self._banco(root)
            medicao = modulo.usage_measurement_in_last_24h(banco, "9e39343dc6f5", str(root))
        self.assertEqual(medicao["cost_main_usd"], 0.20)
        self.assertEqual(medicao["cost_aux_usd"], 0.05)
        # 0,20 + 0,05 = 0,25 (e NÃO 0,45, que é o que saía ao somar task='').
        self.assertEqual(medicao["cost_usd"], 0.25)

    def test_prompt_tokens_usa_o_grand_total(self):
        modulo = _guard()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            banco = self._banco(root)
            medicao = modulo.usage_measurement_in_last_24h(banco, "9e39343dc6f5", str(root))
        self.assertEqual(medicao["main_prompt_tokens"], 10_000)
        self.assertEqual(medicao["aux_prompt_tokens"], 1_500)
        self.assertEqual(medicao["grand_total_prompt_tokens"], 11_500)
        self.assertEqual(medicao["prompt_tokens"], 11_500)
        # O teto compara o GRAND TOTAL: 11.500 passa de 11.000.
        self.assertEqual(
            modulo._decision({"prompt_tokens": 11_000.0}, medicao), ("block", "prompt_tokens")
        )
        self.assertEqual(
            modulo._decision({"prompt_tokens": 12_000.0}, medicao), ("allow", "within_budget")
        )


class LedgerPriorityTests(unittest.TestCase):
    """JSONL é autoritativo; o arquivo legado não pode virar a última decisão."""

    def test_jsonl_vence_o_arquivo_legado(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "work").mkdir(parents=True, exist_ok=True)
            # Legado (rodada 3) diz "none"; o JSONL (rodada 4) diz "auto".
            (root / "work" / "media_decisions.json").write_text(
                json.dumps({"555": {"decision": "none", "score_gap": None}}),
                encoding="utf-8",
            )
            novo = record_media_decision(root, 555, decision="auto", score_gap=3)
            self.assertEqual(read_media_decision(root, 555)["decision"], "auto")
            self.assertEqual(read_media_decision(root, 555)["decision_id"], novo)

    def test_post_so_no_legado_continua_legivel(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "work").mkdir(parents=True, exist_ok=True)
            (root / "work" / "media_decisions.json").write_text(
                json.dumps({
                    "555": {"decision": "none"},
                    "666": {"decision": "reuse", "score_gap": None},
                }),
                encoding="utf-8",
            )
            record_media_decision(root, 555, decision="auto", score_gap=3)
            self.assertEqual(read_media_decision(root, 555)["decision"], "auto")
            # Post que só existe no legado segue visível (e não some do histórico).
            self.assertEqual(read_media_decision(root, 666)["decision"], "reuse")


class MediaValidatePerItemTests(unittest.TestCase):
    """media-validate emite resultado POR ITEM (decisão ligada à imagem)."""

    def _payload(self, *, com_ids: bool = True) -> dict:
        itens = [
            {
                "paragraph_index": 0, "source_page_url": "https://pagina/1",
                "direct_image_url": "https://cdn/1.jpg", "author": "a", "license": "l",
                "license_url": "https://lic/1", "captured_at": "2026-01-01",
                "credit_text": "c", "alt_text": "alt", "is_featured": False,
                "decision_id": "aaa", "decision": "auto",
            },
            {
                "paragraph_index": 1, "source_page_url": "https://pagina/2",
                "direct_image_url": "https://cdn/2.jpg", "author": "a", "license": "l",
                "license_url": "https://lic/2", "captured_at": "2026-01-01",
                "credit_text": "c", "alt_text": "alt", "is_featured": False,
                "decision_id": "bbb", "decision": "choose",
            },
        ]
        if not com_ids:
            for item in itens:
                item.pop("decision_id", None)
                item.pop("decision", None)
        return {
            "site_relevance": {"verdict": "relevant", "reason": "ok"},
            "seo": {"title": "Titulo", "meta_description": "d", "slug": "s", "focus_keyword": "k"},
            "media_plan": itens,
            "cleaned_html": "<p>" + ("palavra " * 400) + "</p>",
        }

    def _run(self, root: Path, *, com_ids: bool = True) -> None:
        from unicornio_editor import cli

        arquivo = root / "editorial.json"
        arquivo.write_text(json.dumps(self._payload(com_ids=com_ids)), encoding="utf-8")
        client = mock.Mock()
        client.get_post.return_value = {"id": 5, "title": {"raw": "Titulo"}, "featured_media": 0}
        resultado_plano = {
            "valid": False,
            "rejected": [{"index": 1, "reason": "imagem nao consta na pagina"}],
            "featured_vision": [{"status": "passed"}],
        }
        with mock.patch.object(cli, "load_config", return_value=_config(dry_run=True)), \
                mock.patch.object(cli, "WordPressClient", return_value=client), \
                mock.patch.object(
                    cli, "validate_media_plan", return_value=resultado_plano
                ), \
                mock.patch.dict(os.environ, CRON_ENV, clear=False):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                cli.main(["media-validate", str(arquivo), "--post-id", "5", "--root", str(root)])

    def _eventos(self, root: Path) -> list[dict]:
        linhas = (root / "work" / "telemetry.jsonl").read_text(encoding="utf-8").splitlines()
        return [
            json.loads(linha) for linha in linhas
            if json.loads(linha).get("event") == "media_validate_result"
        ]

    def test_resultado_por_item_carrega_decision_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # O ledger é a fonte autoritativa: os ids do plano têm de existir.
            record_media_decision(root, 5, decision="auto", score_gap=4,
                                  decision_id="aaa")
            record_media_decision(root, 5, decision="choose", score_gap=0,
                                  decision_id="bbb")
            self._run(root)
            eventos = self._eventos(root)
        por_item = [e for e in eventos if e.get("decision_id")]
        self.assertEqual(len(por_item), 2, eventos)
        self.assertEqual([e["decision_id"] for e in por_item], ["aaa", "bbb"])
        # `decision`/`score_gap` vêm do LEDGER (pelo id), não do texto do plano.
        self.assertEqual([e["decision"] for e in por_item], ["auto", "choose"])
        self.assertEqual([e["score_gap"] for e in por_item], [4, 0])
        self.assertEqual([e["attribution"] for e in por_item], ["resolved", "resolved"])
        self.assertEqual([e["item_index"] for e in por_item], [0, 1])
        # O item 1 (índice 1) foi o rejeitado: a decisão `choose` é a culpada.
        self.assertEqual(por_item[0]["valid"], True)
        self.assertEqual(por_item[0]["rejected_items"], 0)
        self.assertEqual(por_item[1]["valid"], False)
        self.assertEqual(por_item[1]["rejected_items"], 1)
        # Agregado do post entra SEM rótulo; dois ids distintos = plano MISTO.
        agregados = [e for e in eventos if not e.get("decision_id")]
        self.assertEqual(len(agregados), 1)
        self.assertEqual(agregados[0]["decision"], "")
        self.assertEqual(agregados[0]["attribution"], "mixed")
        self.assertEqual(agregados[0]["rejected_items"], 1)
        self.assertEqual(agregados[0]["featured_status"], "passed")

    def test_texto_do_plano_nao_mente_para_a_telemetria(self):
        """O plano diz "auto", o ledger diz "choose": vale o ledger."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            record_media_decision(root, 5, decision="choose", score_gap=1,
                                  decision_id="aaa")
            record_media_decision(root, 5, decision="choose", score_gap=1,
                                  decision_id="bbb")
            self._run(root)
            eventos = self._eventos(root)
        por_item = [e for e in eventos if e.get("decision_id")]
        # O plano (fixture) copiou "auto"/"choose" do agente; o ledger manda.
        self.assertEqual([e["decision"] for e in por_item], ["choose", "choose"])
        self.assertEqual([e["score_gap"] for e in por_item], [1, 1])

    def test_id_inexistente_no_ledger_e_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Só um dos dois ids existe no ledger.
            record_media_decision(root, 5, decision="auto", score_gap=4,
                                  decision_id="aaa")
            self._run(root)
            eventos = self._eventos(root)
        por_item = [e for e in eventos if e.get("decision_id")]
        self.assertEqual([e["attribution"] for e in por_item], ["resolved", "invalid"])
        # Id inválido NÃO ganha rótulo: sem invenção.
        self.assertEqual(por_item[1]["decision"], "")
        self.assertIsNone(por_item[1]["score_gap"])

    def test_plano_sem_id_conta_como_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self._run(root, com_ids=False)
            eventos = self._eventos(root)
            resumo = read_telemetry_summary(root)
        por_item = [e for e in eventos if "item_index" in e]
        self.assertEqual([e["attribution"] for e in por_item], ["missing", "missing"])
        self.assertEqual(resumo["decision_attribution"]["missing"], 3)  # 2 itens + agregado
        self.assertEqual(resumo["decision_attribution"]["decision_attribution_rate"], 0.0)


class ResolverMemoAliasTests(unittest.TestCase):
    """O memo da chave ORIGINAL precisa do resultado COMPLETO."""

    @staticmethod
    def _candidato(url: str) -> dict:
        return {
            "direct_image_url": url, "source_page_url": "", "usable": False,
            "discovery_only": True, "rejected_reason": "missing_source_page",
            "engine": "yandex",
        }

    def test_candidato_bruto_repetido_nao_vira_rejeitado(self):
        from unicornio_editor import cli

        memo: dict = {}
        fortes = ["metroid-prime-4-keyart"]
        contador = {"n": 0}

        def busca_falsa(_query):
            contador["n"] += 1
            return [{"source_page_url": f"https://site/pagina{contador['n']}"}]

        def evidence_dirigida(subject, *, filename="", **kwargs):
            forte = any(marcador in filename for marcador in fortes)
            return {
                "subject": subject, "matched": ["filename"] if forte else [],
                "evidence": {}, "local_score": 9 if forte else 0,
                "score": 9 if forte else 0, "gate": "relevance", "penalties": [],
                "needs_vision": False,
                "verdict": "deterministic_match" if forte else "reject",
            }

        def rodada(candidatos):
            with mock.patch(
                "unicornio_editor.media.source_resolver._buscador_padrao", side_effect=busca_falsa
            ), mock.patch(
                "unicornio_editor.media.source_verify.validate_discovered_candidate",
                side_effect=lambda cand, **kw: {
                    "valid": True, "reason": "ok", "images_in_page": 1
                },
            ), mock.patch(
                "unicornio_editor.media.visual_hash.image_hashes",
                side_effect=lambda urls: {u: f"hash-{u}" for u in urls if u},
            ), mock.patch(
                "unicornio_editor.media.evidence.evidence_score",
                side_effect=evidence_dirigida,
            ), mock.patch(
                "unicornio_editor.media.evidence.dedupe_by_phash",
                side_effect=lambda aprovados, rejeitados, **kw: (aprovados, rejeitados),
            ):
                return cli._enriquecer_candidatos(
                    candidatos, subject="Metroid Prime 4", termo="metroid prime 4",
                    capacity=1, enriched_cache=memo,
                )

        primeiro = [self._candidato("https://cdn/metroid-prime-4-keyart.jpg")]
        aprovados1, _r1, _d1 = rodada(primeiro)
        self.assertEqual(len(aprovados1), 1)
        self.assertEqual((primeiro[0].get("evidence") or {}).get("verdict"), "deterministic_match")

        # O MESMO candidato bruto reaparece (memo da chave original).
        segundo = [self._candidato("https://cdn/metroid-prime-4-keyart.jpg")]
        aprovados2, rejeitados2, _d2 = rodada(segundo)
        veredito = (segundo[0].get("evidence") or {}).get("verdict")
        self.assertEqual(veredito, "deterministic_match", "memo incompleto seria rejeitado")
        self.assertEqual(len(aprovados2), 1)
        self.assertEqual(rejeitados2, [])


if __name__ == "__main__":
    unittest.main()
