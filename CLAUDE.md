# Ferreira Telecom - servidor de automações (CT115)

Você roda dentro do contêiner LXC 115 (Debian) do Proxmox da Ferreira Telecom, um provedor de internet com 10 funcionários.
Quem conversa com você é o dono da empresa, que NÃO é programador: explique em português simples, curto, e faça o trabalho
em vez de mandar ele digitar comandos. Seu usuário é `agente` (sem root, sem sudo).

## Regras de segurança (valem sempre)
- Nunca mostre, copie, imprima nem envie valores de tokens, chaves ou senhas. `/etc/relatorio-sgp.env` é só para os scripts
  carregarem; ao conferir, mostre apenas os NOMES das variáveis.
- Nunca envie WhatsApp ou mensagem para ninguém além do dono (variável `WHATSAPP_DESTINO`). Não crie envios para clientes.
- Os scripts só LEEM dados do SGP e do Chatmix. Não crie, altere nem apague registros no SGP/Chatmix sem o dono pedir de forma
  explícita e confirmar o que será feito.
- Não apague arquivos de dados nem altere o cron do root. Antes de qualquer ação irreversível, explique e peça confirmação.
- Dados de clientes (nome, CPF, telefone, endereço) não devem ir para arquivos, logs ou mensagens. Use só contagens e protocolos.
  EXCEÇÃO autorizada pelo dono em 13/11/2026: o agente Despacho (despacho.py), e só ele, mostra nome do cliente, endereço do contrato e o
  texto da OS, apenas em mensagem privada do Telegram ao dono, sob demanda. Nunca telefone/CPF, nunca em arquivos, logs, cron ou WhatsApp.
  SEGUNDA EXCEÇÃO, também em 13/11/2026: aviso_os_fechada.py mostra SÓ o nome do cliente no aviso de OS fechada (Telegram privado ao dono,
  automático via cron). O nome não vai para arquivo de estado nem para o log (o script só imprime contagens). Sem endereço, telefone ou CPF.
- Antes de agendar uma tarefa nova, teste com `--sem-envio` e mostre o resultado ao dono.

## Onde está tudo
- Scripts: `/opt/relatorio-sgp/` (você tem permissão de escrita). Saídas: `/var/lib/relatorio-sgp/`. Logs: `/var/log/*.log`.
- Configuração: `/etc/relatorio-sgp.env` (você pode ler). Variáveis: SGP_URL, SGP_APP, SGP_TOKEN, CHATMIX_KEY, CHATMIX_TOKEN,
  WHATSAPP_DESTINO, CHATMIX_DOMINIO, CHATMIX_PUBLIC, RELATORIO_DIR.
- Para rodar um script com as variáveis:
  `bash -c 'set -a; . /etc/relatorio-sgp.env; set +a; python3 /opt/relatorio-sgp/<script>.py --sem-envio'`
- Python 3 só com a biblioteca padrão (sem internet para instalar pacotes pip: não use bibliotecas externas).
- Fuso: America/Fortaleza. O cron de tarefas novas deve ser criado no crontab do SEU usuário (`crontab -e`), carregando o env
  com `set -a; . /etc/relatorio-sgp.env; set +a;`. O crontab do root (agendamentos atuais) só o root altera: se precisar mudar,
  peça ao dono para rodar o comando que você preparar.

## O que já roda todo dia (cron do root, resultado por WhatsApp)
| Hora | Script | Conteúdo |
|---|---|---|
| 7h00 | contas_a_pagar_dia.py | contas a pagar do dia + atrasadas |
| 7h05 | auditoria_chatmix.py | atendimentos do Chatmix encerrados ontem |
| 7h10 | painel_operacao.py | OS externas, técnicos, agenda, pendências, contratos |
| 7h20 | resumo_gestao.py | resumo de gestão diário em 2 mensagens |
Logs: /var/log/contas-pagar.log, auditoria-chatmix.log, painel-operacao.log, resumo-gestao.log.
Instalador dos agendamentos: `/opt/relatorio-sgp/instalar_agendamentos.sh` (precisa ser root).

## APIs
- SGP: POST form-encoded com `app` + `token`. Endpoints usados: /api/ura/titulos/, /api/ura/ocorrencia/list/,
  /api/ura/ordemservico/list/ (chave `ordens_servicos`), /api/ura/listacontrato/, /api/ura/contas/pagar/ (chave `contas`).
  A API de ESTOQUE (/api/estoque/...) pede usuário e senha (Basic) e recusou todas as credenciais testadas: ficou para depois.
- Chatmix envio: POST https://envios.bulkv2.chatmix.com.br/api JSON {key, token, numero, mensagem}. Use User-Agent de navegador
  (o Cloudflare barra o padrão do Python, erro 1010).
- Chatmix leitura: `{CHATMIX_DOMINIO}/api-v2/public-api`, cabeçalho `X-Auth`; limite de 2 chamadas por minuto em
  /attendances/closed e /attendances/{id}/messages (a resposta de /messages é uma lista simples; texto em content.content).

## Regras do negócio
- Faturas dos clientes vencem nos dias 5, 10, 15, 20, 25 e 30.
- OS tipo "Externa" = serviço de campo (5 técnicos); "Interna" = escritório. O SGP só guarda a DATA de finalização da OS.
- OS status: 0 aberta, 1 encerrada, 2 em execução, 3 pendente. Contas a pagar: 1 pendente, 2 quitado, 3 cancelado, 4 atrasada.

## Em andamento agora (NÃO interfira)
- Coleta dos atendimentos do Chatmix (jan/2026 até hoje) para auditoria e manual de atendimento: script
  `/tmp/coletar_atendimentos_chatmix.py`, dados em `/tmp/coleta_chatmix/` (pertencem ao root). A fase "amostra" baixa 400
  conversas anonimizadas (~3h). Não rode outra coleta ao mesmo tempo (limite de 2 chamadas/min). `PRIVADO_nomes.jsonl`
  nunca sai do servidor.

## Ideias combinadas (fazer só quando o dono pedir)
- Aviso no WhatsApp a cada OS externa fechada, com técnico e tempo gasto (precisa checar os horários que o SGP guarda:
  `explorar_os_tempos.py`).
- Fila de OS priorizada e sugestão de técnico por proximidade; depois integrar a posição dos veículos (Prime Track: precisa
  de API) com o check-in do SGP.
- Levantamento de recorrência de compras do estoque (`recorrencia_compras.py`, depende do acesso à API de estoque).
- Telegram (bot com comandos só do dono), alerta de estoque mínimo, "vencem amanhã", inadimplência por ciclo.
- Servidor TR-069 (GenieACS) para as ONTs: aguardando marcas e modelos de ONT/OLT.

## Bot do Telegram e agentes (adicionado)
- Serviço systemd `bot-telegram` (usuário agente) roda `/opt/relatorio-sgp/bot_telegram.py`; só responde ao dono (TELEGRAM_USER_ID).
  Aceita texto e áudio (faster-whisper em /opt/whisper, modelo em /var/lib/relatorio-sgp/whisper). Saídas do bot: `/var/lib/relatorio-sgp/bot`.
- Agentes: caixa (contas_a_pagar_dia.py; aceita data: dia 20, amanhã, 15/12, sexta, semana), auditor, operacao, gestao,
  despacho (despacho.py, só SUGERE técnico), cobranca (cobranca.py, só totais), sentinela (sentinela.py), consulta (consultas.py).
- Módulo comum: `comum.py` (SGP, Telegram, estado). Tarefas agendadas no crontab do agente (ver agendar_novas_tarefas.sh):
  aviso_os_fechada.py (10 min), lembrete_vencimentos.py (18h), sentinela.py (8h e a cada 30 min). Antes de agendar: testar com --sem-envio.
- Contas atrasadas = status Atrasada ou Pendente vencida, SEM data de pagamento e não Quitado/Cancelado, janela JANELA_ATRASO_DIAS (180).
- Painel web (painel_web.py, só leitura, só contagens/valores/OS/fornecedores/técnicos, nunca dados de cliente): `--gerar` atualiza
  `/var/lib/relatorio-sgp/painel/index.html` (cron em /etc/cron.d/painel-web, a cada 10 min); `--servir` (serviço systemd `painel-web`)
  serve com usuário/senha (PAINEL_SENHA no env, nunca mostrar) e bloqueio por tentativas. Sem HTTPS só escuta no IP interno (PAINEL_BIND).
  Instalador: `instalar_painel.sh` (root; opções --senha, --https CERT KEY, --status, --remover).
- A VM tem 12 GB de RAM e 12 núcleos (antes 512 MB).
