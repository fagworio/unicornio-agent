# Supervisor do ciclo editorial V2

O cron Editorial existente continua como agendador. Ele chama
`start-v2-cycle.sh`, que retorna imediatamente e não executa Writer ou `v2-cycle`
dentro da sessão Hermes. O serviço singleton systemd roda o ciclo normal com o
checkpoint, os locks e o `.env` do projeto.

## Política operacional

- Unit: `unicornio-editorial-v2.service`; `Type=oneshot`, sem `[Install]` e sem
  habilitação no boot.
- `TimeoutStartSec=0`: não cortar um estágio legítimo no limite de 420 segundos
  da ferramenta Hermes. O processo continua sob systemd até o encerramento do
  coordenador V2.
- `TimeoutStopSec=0` e `Restart=no`: não forçar a morte nem reiniciar a unit
  automaticamente. Um stop só deve ser considerado após checar o processo e
  estabelecer janela de manutenção; falha fica visível em `ActiveState=failed`.
- O trigger não inicia segunda instância quando `active`/`activating`, e bloqueia
  quando a unit está `failed` ou não instalada. Não usar `restart` nem
  `reset-failed` como recuperação automática.
- O runner usa somente `v2-cycle --root <projeto>` normal; não usa `--fresh`, IDs
  forçados, Publisher ou fluxos legados.

## Instalação (somente após SHA aprovado e backup operacional verificado)

```bash
install -o root -g root -m 0644 systemd/unicornio-editorial-v2.service \
  /etc/systemd/system/unicornio-editorial-v2.service
systemd-analyze verify /etc/systemd/system/unicornio-editorial-v2.service
systemctl daemon-reload
systemctl show unicornio-editorial-v2.service \
  --property=LoadState,ActiveState,User,WorkingDirectory,ExecStart
```

Não habilite a unit no boot. Preserve o schedule e o ID do cron Hermes. Atualize
somente o prompt do job `66126ac17de0` para invocar o trigger após o reset e o
smoke test; nunca rode `cron-install.sh` como atalho para alterar um job existente.

## Verificação de ciclo de vida antes da ativação

A validação de shell/testes e `systemd-analyze verify` não exercita a execução
real. Antes de reativar o cron, teste instalação, start, stop e recuperação com
uma unit descartável cujo `ExecStart` aponta para um worker inofensivo em scratch;
nunca use o serviço de produção para esse teste enquanto houver estado editorial
incerto. Confirme que o teste de stop termina sem força-kill, que uma falha deixa
a unit em `failed` sem reinício e que uma nova execução só ocorre após
reconciliação explícita e `reset-failed` manual. Apague apenas os artefatos de
unit descartável que você mesmo criou e confirme que a unit de produção/Gateway
não foi reiniciada.

Depois da validação do reset e do supervisor, atualize o job existente com
`start-v2-cycle.sh`; mantenha o monitor e o schedule atuais. Leia o estado da unit
(`ActiveState`, `Result`, `ExecMainStatus`), checkpoint, journal, Registry e
readback WordPress para verificar uma execução. `systemctl start --no-block`
aceito não prova que o ciclo avançou nem que algum post chegou a READY.
