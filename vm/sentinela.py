#!/usr/bin/env python3
"""
Sentinela - confere se a VM e os relatórios estão bem e avisa no Telegram (só o dono) quando algo falha.

Confere: bot do Telegram ativo, relatórios das 7h rodaram hoje e sem erro no log, espaço em disco, memória, SGP no ar.
Só manda mensagem quando há PROBLEMA novo (repete no máximo a cada 6 horas) e avisa quando voltar ao normal.

Uso:
  python3 sentinela.py                 confere tudo e avisa só se houver problema (para o cron das 8h)
  python3 sentinela.py --rapido        só serviço, disco e memória (para o cron a cada 30 min)
  python3 sentinela.py --sem-envio     mostra o resultado completo e não manda nada (o Telegram usa este)
Variáveis opcionais: LIMITE_DISCO_PCT (85), LIMITE_MEMORIA_PCT (10 = alerta se sobrar menos que isso), LOG_DIR (/var/log)
"""
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import comum as c

ARQ = "sentinela_estado.json"
REPETIR_HORAS = 6
# (arquivo de log, nome, hora em que o cron roda)
RELATORIOS = (("contas-pagar.log", "Caixa (contas a pagar)", (7, 0)), ("auditoria-chatmix.log", "Auditor (Chatmix)", (7, 5)),
              ("painel-operacao.log", "Operação", (7, 10)), ("resumo-gestao.log", "Gestão", (7, 20)))
PADRAO_ERRO = re.compile(r"Traceback|Erro \d|NÃO aceitou|NÃO entregou|recusou|Falta definir|Não consegui", re.I)


def checar_servico(nome="bot-telegram"):
    try:
        r = subprocess.run(["systemctl", "is-active", nome], capture_output=True, text=True, timeout=15)
        return [] if r.stdout.strip() == "active" else [(f"servico_{nome}", f"O serviço {nome} NÃO está ativo ({r.stdout.strip() or 'sem resposta'}).")]
    except (OSError, subprocess.TimeoutExpired):
        return []  # sem systemctl (teste): não acusa


def checar_disco():
    p = []
    limite = int(os.environ.get("LIMITE_DISCO_PCT", "85"))
    for pasta in ("/", os.environ.get("RELATORIO_DIR", "/var/lib/relatorio-sgp")):
        try:
            u = shutil.disk_usage(pasta)
        except OSError:
            continue
        usado = 100 * u.used / u.total
        if usado >= limite:
            p.append((f"disco_{pasta}", f"Disco {pasta} com {usado:.0f}% usado ({u.free // 2**20} MB livres)."))
    return p


def checar_memoria():
    try:
        m = dict(l.split(":", 1) for l in open("/proc/meminfo").read().splitlines())
        tot, disp = int(m["MemTotal"].split()[0]), int(m["MemAvailable"].split()[0])
    except (OSError, KeyError, ValueError):
        return []
    livre = 100 * disp / tot
    return [("memoria", f"Memória quase no fim: só {livre:.0f}% disponível ({disp // 1024} MB).")] \
        if livre < int(os.environ.get("LIMITE_MEMORIA_PCT", "10")) else []


def checar_relatorios(agora):
    pasta, p = os.environ.get("LOG_DIR", "/var/log"), []
    for arq, nome, (h, m) in RELATORIOS:
        previsto = agora.replace(hour=h, minute=m, second=0, microsecond=0)
        if agora < previsto + timedelta(minutes=40):
            continue  # ainda não deu o tempo de rodar hoje
        caminho = os.path.join(pasta, arq)
        try:
            mod = datetime.fromtimestamp(os.path.getmtime(caminho), agora.tzinfo)
        except OSError:
            p.append((f"log_{arq}", f"{nome}: não achei o log {arq}."))
            continue
        if mod < previsto - timedelta(minutes=5):
            p.append((f"rodou_{arq}", f"{nome}: o relatório de hoje não rodou (último registro em {mod:%d/%m %H:%M})."))
            continue
        try:
            with open(caminho, encoding="utf-8", errors="ignore") as f:
                final = f.read()[-3000:]
            # "Destinatario duplicado" é recusa temporária do Chatmix: o relatório tenta de novo sozinho.
            # Se mesmo assim não entregar, o script escreve "NÃO entregou" e aí sim vira alerta.
            final = "\n".join(l for l in final.splitlines() if "duplicado" not in l.lower())
            if PADRAO_ERRO.search(final):
                p.append((f"erro_{arq}", f"{nome}: o log de hoje tem erro. Peça ao Claude da VM para ver {arq}."))
        except OSError:
            pass
    return p


def checar_sgp():
    base = os.environ.get("SGP_URL")
    if not base:
        return []
    try:
        urllib.request.urlopen(urllib.request.Request(base.rstrip("/") + "/", headers={"User-Agent": c.UA}), timeout=20)
    except urllib.error.HTTPError:
        return []  # respondeu (mesmo com erro de página): está no ar
    except Exception:
        return [("sgp", "O SGP não respondeu (fora do ar ou sem internet na VM).")]
    return []


def main():
    rapido, sem_envio = "--rapido" in sys.argv, "--sem-envio" in sys.argv
    agora = c.agora()
    problemas = checar_servico() + checar_disco() + checar_memoria()
    if not rapido:
        problemas += checar_relatorios(agora) + checar_sgp()

    if sem_envio or not (os.environ.get("TELEGRAM_TOKEN") and os.environ.get("TELEGRAM_USER_ID")):
        if problemas:
            print("*Sentinela: há problemas*\n" + "\n".join(f"- {t}" for _, t in problemas))
        else:
            print("*Sentinela: tudo certo*\n- bot ativo, disco e memória normais"
                  + ("" if rapido else ", relatórios das 7h rodando sem erro, SGP no ar"))
        return

    estado = c.carregar_estado(ARQ)
    ativos = estado.get("ativos", {})
    novos, agora_iso = [], agora.isoformat(timespec="seconds")
    for chave, texto in problemas:
        ult = ativos.get(chave)
        if not ult or agora - datetime.fromisoformat(ult) >= timedelta(hours=REPETIR_HORAS):
            novos.append(texto)
            ativos[chave] = agora_iso
    resolvidos = [k for k in list(ativos) if k not in {k2 for k2, _ in problemas}
                  and (rapido is False or k.split("_")[0] in ("servico", "disco", "memoria"))]
    for k in resolvidos:
        ativos.pop(k)
    msg = []
    if novos:
        msg += ["*Sentinela: atenção*"] + [f"- {t}" for t in novos]
    if resolvidos:
        msg += ["*Sentinela:* voltou ao normal: " + ", ".join(sorted(resolvidos))]
    if msg and c.telegram_enviar("\n".join(msg)):
        c.salvar_estado(ARQ, {"ativos": ativos})
    elif not msg:
        c.salvar_estado(ARQ, {"ativos": ativos})
    print("\n".join(msg) if msg else ("Sem novidades." if problemas else "Tudo certo."))


if __name__ == "__main__":
    main()
