#!/usr/bin/env python3
"""
Contas a pagar do dia - Ferreira Telecom (dados do SGP, envio por WhatsApp via Chatmix).

Todo dia às 7h: lista as contas a pagar PENDENTES que vencem naquele dia.
Usa só a biblioteca padrão do Python 3.9+.

Variáveis de ambiente (as mesmas que você já tem na VM):
    SGP_URL, SGP_APP, SGP_TOKEN
    TELEGRAM_TOKEN, TELEGRAM_USER_ID   (se faltar alguma, só exibe e salva) (envio só pelo Telegram desde 17/11/2026)

Opcionais:
    RELATORIO_DIR       pasta de saída (padrão: ./relatorios)
    INCLUIR_ATRASADAS   0 = não lista as contas atrasadas (padrão: 1, lista as mais antigas primeiro)

Uso:
    python3 contas_a_pagar_dia.py                 # contas de HOJE, e envia
    python3 contas_a_pagar_dia.py --sem-envio     # só exibe e salva (para testar)
    python3 contas_a_pagar_dia.py 2026-11-15      # contas de outro dia (AAAA-MM-DD)
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Fortaleza")
UA = "Mozilla/5.0 (compatible; FerreiraTelecom-Relatorio/1.0)"
DIAS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
STATUS_PENDENTE, STATUS_ATRASADA = 1, 4  # códigos de status das contas no SGP
MAX_LINHAS = 40      # limite de contas do dia listadas na mensagem (o resto vira "... e mais N")
MAX_ATRASADAS = 30   # limite de contas atrasadas listadas (as mais antigas primeiro)


# ------------------------------------------------------------------------- SGP
def sgp_post(base, app, token, path, **params):
    dados = {"app": app, "token": token, **{k: v for k, v in params.items() if v not in (None, "")}}
    req = urllib.request.Request(base + path, data=urllib.parse.urlencode(dados).encode(), method="POST",
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Erro {e.code} em {path}: {e.read().decode('utf-8', 'ignore')[:300]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Não consegui conectar no SGP: {e.reason}")


def contas(base, app, token, **filtros):
    itens, offset = [], 0
    for _ in range(200):  # trava de segurança
        r = sgp_post(base, app, token, "/api/ura/contas/pagar/", offset=offset, limit=500, **filtros)
        lote = r.get("contas", []) if isinstance(r, dict) else []
        itens += lote
        total = (r.get("paginacao") or {}).get("total") if isinstance(r, dict) else None
        offset += 500
        if len(lote) < 500 or (total is not None and offset >= total):
            break
    return itens


def em_aberto(c):
    """True só se a conta está de fato em aberto: o texto do status não é Quitado/Cancelado e não há data de pagamento.
    (Protege caso o SGP devolva contas pagas junto com o filtro de status.)"""
    st = str(c.get("status") or "").lower()
    if "quit" in st or "cancel" in st or "pago" in st:
        return False
    return not str(c.get("data_pagamento") or "").strip()


def atrasadas_ate(base, app, token, dia):
    """Atrasadas = 'Atrasada' (status 4) + 'Pendente' (status 1) vencidas, só as que seguem em aberto, dos últimos
    JANELA_ATRASO_DIAS dias (padrão 180; contas mais antigas que isso quase sempre são sobras de cadastro antigo)."""
    janela = int(os.environ.get("JANELA_ATRASO_DIAS", "180"))
    ini = (dia - timedelta(days=janela)).isoformat()
    fim = (dia - timedelta(days=1)).isoformat()
    todas = (contas(base, app, token, status=STATUS_ATRASADA, data_vencimento_inicio=ini, data_vencimento_fim=fim)
             + contas(base, app, token, status=STATUS_PENDENTE, data_vencimento_inicio=ini, data_vencimento_fim=fim))
    return [c for c in todas if em_aberto(c) and str(c.get("data_vencimento") or "")[:10] <= fim]


# ----------------------------------------------------------------------- texto
def brl(v):
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def valor(c):
    return float(c.get("valor") or 0)


def linha(c):
    forn, desc = (c.get("fornecedor_nome") or "").strip(), (c.get("conta") or "").strip()
    nome = forn if forn and (not desc or forn == desc) else f"{forn} | {desc}" if forn else desc or "Sem descrição"
    pt = c.get("parcela_total") or 1
    parc = f" ({c.get('parcela')}/{pt})" if pt > 1 else ""
    return f"- {nome[:60]}{parc}: {brl(valor(c)) if valor(c) else 'valor a definir'}"


def atraso(c, dia):
    """Sufixo ' | venc. 05/11 (6 dias)' para contas atrasadas; vazio se a data não vier."""
    try:
        v = date.fromisoformat(str(c.get("data_vencimento"))[:10])
    except ValueError:
        return ""
    n = (dia - v).days
    return f" | venc. {v.strftime('%d/%m')} ({n} {'dia' if n == 1 else 'dias'})"


def montar(dia, hoje_lista, atrasadas, hoje=None):
    hoje = hoje or dia
    rotulo = "de hoje" if dia == hoje else f"de {dia.strftime('%d/%m')}"
    L = [f"*Ferreira Telecom - Contas a pagar {rotulo}*",
         f"{DIAS[dia.weekday()].capitalize()}, {dia.strftime('%d/%m/%Y')}", ""]
    if not hoje_lista:
        L.append("Nenhuma conta pendente vence hoje." if dia == hoje else "Nenhuma conta pendente vence neste dia.")
    else:
        total = sum(valor(c) for c in hoje_lista)
        L.append(f"*Total do dia: {brl(total)}* ({len(hoje_lista)} {'conta' if len(hoje_lista) == 1 else 'contas'})")
        por_empresa = defaultdict(list)
        for c in hoje_lista:
            por_empresa[(c.get("empresa_nome") or "").strip()].append(c)
        varias = len(por_empresa) > 1
        restante = MAX_LINHAS
        for emp, lista in sorted(por_empresa.items(), key=lambda x: -sum(valor(c) for c in x[1])):
            lista = sorted(lista, key=lambda c: -valor(c))
            L.append("")
            if varias:
                L.append(f"*{emp or 'Sem empresa'}* - {brl(sum(valor(c) for c in lista))}")
            for c in lista[:max(restante, 0)]:
                L.append(linha(c))
            restante -= len(lista)
            if restante < 0:
                L.append(f"... e mais {-restante} contas (veja no SGP)")
                restante = 0
    if atrasadas is not None:
        L.append("")
        if not atrasadas:
            L.append("Nenhuma conta atrasada.")
        else:
            L.append(f"*Atrasadas: {brl(sum(valor(c) for c in atrasadas))}* "
                     f"({len(atrasadas)} {'conta' if len(atrasadas) == 1 else 'contas'})")
            # as mais antigas primeiro
            for c in sorted(atrasadas, key=lambda c: c.get("data_vencimento") or "")[:MAX_ATRASADAS]:
                L.append(linha(c) + atraso(c, dia))
            if len(atrasadas) > MAX_ATRASADAS:
                L.append(f"... e mais {len(atrasadas) - MAX_ATRASADAS} contas atrasadas (veja no SGP)")
    return "\n".join(L)


def montar_semana(ini, lista):
    """Resumo dos próximos 7 dias: total e quantidade por dia de vencimento."""
    por_dia = defaultdict(list)
    for c in lista:
        por_dia[str(c.get("data_vencimento") or "")[:10]].append(c)
    L = [f"*Ferreira Telecom - Contas a pagar dos próximos 7 dias*",
         f"De {ini.strftime('%d/%m')} a {(ini + timedelta(days=6)).strftime('%d/%m/%Y')}", ""]
    for i in range(7):
        d = ini + timedelta(days=i)
        cs = por_dia.get(d.isoformat(), [])
        L.append(f"{d.strftime('%d/%m')} {DIAS[d.weekday()][:3]}: " +
                 (f"{brl(sum(valor(c) for c in cs))} ({len(cs)} {'conta' if len(cs) == 1 else 'contas'})" if cs else "nada"))
    L += ["", f"*Total: {brl(sum(valor(c) for c in lista))}* ({len(lista)} {'conta' if len(lista) == 1 else 'contas'})",
          "Para ver o detalhe de um dia: /caixa 2026-11-20 (ou diga 'caixa dia 20')."]
    return "\n".join(L)


# -------------------------------------------------------------------- Envio (Telegram)
_ERRO = [""]


def enviar_whatsapp(msg):
    """Desde 17/11/2026 o relatório vai SÓ pelo Telegram (privado do dono). O nome da função foi mantido."""
    import comum
    if comum.telegram_enviar(msg):
        print("Telegram: enviado ao dono.")
        return True
    print("Telegram: NÃO entregou a mensagem.", file=sys.stderr)
    return False


# ------------------------------------------------------------------------ main
def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sem_envio = "--sem-envio" in sys.argv
    try:
        base, app, token = (os.environ[k] for k in ("SGP_URL", "SGP_APP", "SGP_TOKEN"))
    except KeyError as e:
        raise SystemExit(f"Falta definir a variável de ambiente {e.args[0]}.")
    base = base.rstrip("/")
    hoje = datetime.now(TZ).date()
    dia = date.fromisoformat(args[0]) if args else hoje

    if "--semana" in sys.argv:
        lista = contas(base, app, token, status=STATUS_PENDENTE, data_vencimento_inicio=dia.isoformat(),
                       data_vencimento_fim=(dia + timedelta(days=6)).isoformat())
        print(montar_semana(dia, lista))
        return
    hoje_lista = contas(base, app, token, status=STATUS_PENDENTE,
                        data_vencimento_inicio=dia.isoformat(), data_vencimento_fim=dia.isoformat())
    # atrasadas só fazem sentido para hoje; ao consultar outro dia, mostra só as contas daquele dia
    atrasadas = (atrasadas_ate(base, app, token, hoje)
                 if dia == hoje and os.environ.get("INCLUIR_ATRASADAS", "1") != "0" else None)
    msg = montar(dia, hoje_lista, atrasadas, hoje)

    pasta = os.environ.get("RELATORIO_DIR", "relatorios")
    os.makedirs(pasta, exist_ok=True)
    arquivo = os.path.join(pasta, f"contas_pagar_{dia.isoformat()}.txt")
    with open(arquivo, "w", encoding="utf-8") as f:
        f.write(msg + "\n")
    print(msg, f"\n\nSalvo em {arquivo}")

    if sem_envio or not all(os.environ.get(k) for k in ("TELEGRAM_TOKEN", "TELEGRAM_USER_ID")):
        print("Envio por WhatsApp não realizado (modo --sem-envio ou variáveis ausentes).")
    elif not enviar_whatsapp(msg):
        sys.exit(1)  # sai com erro para ficar registrado no log


if __name__ == "__main__":
    main()
