# Validação P0 após `3f80456`

## Escopo

Esta alteração mantém o publisher pausado e não executa deploy, cron, publicação
externa ou escrita em posts. O objetivo é corrigir a detecção estrutural de
listicles e a aquisição incremental de mídia no V2.

## Implementado

- H2 agrupador com H3 descritivos sem numeração é reconhecido quando há intenção
  explícita de lista no título, categorias ou agrupador.
- CTA e headings auxiliares não são itens.
- Cada item carrega identidade, título, grupo, posição HTML, subject e slot.
- A aquisição individual não é abortada porque a cobertura total ainda está
  incompleta; cobertura completa continua sendo gate de COMPOSE/READY e do
  preflight do publisher.
- Featured não é contabilizada como quota inline.
- A inserção de listicles seleciona o heading canônico do item e não depende de
  numeração ou de dois-pontos.

## Regressão local

`tests/fixtures/post_115179_regression.html` representa o formato reportado para
o post 115179: dois H2 agrupadores, cinco H3 de itens sem numeração, títulos
curtos/escapados e um CTA H3. Ele é uma fixture local de regressão, não uma
cópia obtida do WordPress.

Foram cobertos localmente: zero, uma, quatro e cinco imagens; obra errada;
duplicação; featured sem quota inline; busca `EXHAUSTED` sem waiver; CTA fora
da contagem; inserção por seção sem fallback de heading; URLs trocadas entre
seções; e a contabilidade de identidade visual.

## Limitação operacional

O WP Agent não possui site WordPress conectado neste ambiente. Portanto, não
foi possível obter o HTML real do post 115179 nem identificar/auditar os dois
posts `READY` da produção. Essa validação permanece somente leitura pendente de
uma conexão autorizada; nenhum post foi alterado.

## Comandos de validação

```text
PYTHONPATH=src python3 -m pytest -q tests/test_list_quality.py tests/test_media_evidence.py tests/test_media_inserter.py tests/test_v2_media_accounting.py tests/test_checklist.py tests/test_v2_model.py tests/test_v2_production_stages.py
```

O publisher permanece pausado até a validação E2E supervisionada em produção.
