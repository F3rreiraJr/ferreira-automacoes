#!/usr/bin/env python3
"""
Auditoria diária dos atendimentos do Chatmix da Ferreira Telecom.

Usa só a biblioteca padrão do Python 3.9+. Lê os atendimentos ENCERRADOS do dia
anterior pela API Pública do Chatmix e monta um resumo para o WhatsApp.
A mensagem NÃO inclui nome, telefone ou e-mail de clientes (só o protocolo).

Variáveis de ambiente:
    CHATMIX_DOMINIO   ex.: https://srv2.chatmix.com.br  (sem barra no final)
    CHATMIX_PUBLIC    token da API Pública (Meu perfil -> Segurança -> API Pública)

  Envio por WhatsApp (as mesmas do relatório do SGP; se faltar alguma, só exibe e salva):
    CHATMIX_KEY, CHATMIX_TOKEN, WHATSAPP_DESTINO

  Opcionais:
    RELATORIO_DIR        pasta de saída (padrão: ./relatorios)
    NOTA_BAIXA           notas até este valor entram em "atenção" (padrão: 2, supondo escala 1 a 5)
    CHATMIX_PAUSA_SEG    pausa entre páginas de /closed (padrão: 31; o limite do Chatmix é 2/min)

Uso:
    python3 auditoria_chatmix.py                 # ontem, e envia
    python3 auditoria_chatmix.py --sem-envio     # só exibe e salva
    python3 auditoria_chatmix.py 2026-11-10      # outro dia
"""
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Fortaleza")
UA = "Mozilla/5.0 (compatible; FerreiraTelecom-Relatorio/1.0)"
SEM_ATENDENTE = "Sem atendente (automação)"


# --------------------------------------------------------------------- Chatmix
def chamar(base, path, token, params, tentativas=3):
    q = urllib.parse.urlencode(params)
    req = urllib.request.Request(f"{base}/api-v2/public-api{path}?{q}",
                                 headers={"X-Auth": token, "Accept": "application/json", "User-Agent": UA})
    for i in range(tentativas):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tentativas - 1:  # estourou o limite: espera e tenta de novo
                time.sleep(int(os.environ.get("CHATMIX_PAUSA_SEG", "31")) * 2)
                continue
            raise SystemExit(f"Erro {e.code} em {path}: {e.read().decode('utf-8', 'ignore')[:200]}")
        except urllib.error.URLError as e:
            raise SystemExit(f"Não consegui conectar no Chatmix: {e.reason}")


def buscar_encerrados(base, token, dia):
    pausa = int(os.environ.get("CHATMIX_PAUSA_SEG", "31"))
    itens, pagina = [], 1
    while True:
        r = chamar(base, "/attendances/closed", token,
                   {"date_start": f"{dia} 00:00:00", "date_end": f"{dia} 23:59:59",
                    "per_page": 50, "page": pagina})
        itens += r.get("data", [])
        ultima = (r.get("meta") or {}).get("last_page", 1)
        if pagina >= ultima or pagina >= 100:
            return itens
        pagina += 1
        time.sleep(pausa)  # /closed aceita só 2 chamadas por minuto


def fila_agora(base, token):
    return chamar(base, "/attendances/count", token, {}).get("attendances", {})


# --------------------------------------------------------------------- análise
def ts(s):
    return datetime.strptime(str(s)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")


def minutos(a):
    try:
        return max((ts(a["closed_at"]) - ts(a["created_at"])).total_seconds() / 60, 0)
    except (KeyError, ValueError, TypeError):
        return None


def nome_atendente(a):
    u = a.get("user") or {}
    nome = f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip()
    return nome or SEM_ATENDENTE


def dur(m):
    m = round(m)
    return f"{m} min" if m < 60 else f"{m // 60}h{m % 60:02d}"


def analisar(atend, nota_baixa):
    por_atendente = defaultdict(list)
    por_canal, por_hora, duracoes = Counter(), Counter(), []
    notas, por_pergunta, baixas, class_ = [], defaultdict(list), [], Counter()

    for a in atend:
        quem, m = nome_atendente(a), minutos(a)
        por_atendente[quem].append(a)
        por_canal[(a.get("channel") or {}).get("name") or "Sem canal"] += 1
        try:
            por_hora[ts(a["created_at"]).hour] += 1
        except (KeyError, ValueError, TypeError):
            pass
        if m is not None:
            duracoes.append(m)
        for c in a.get("classifications") or []:
            class_[c.get("name") or "Sem nome"] += 1
        for s in a.get("satisfaction_surveys") or []:
            n = s.get("satisfaction")
            if isinstance(n, (int, float)):
                notas.append(n)
                por_pergunta[s.get("question_text") or "Pergunta"].append(n)
                if n <= nota_baixa:
                    baixas.append((a.get("protocol"), quem, n, (s.get("comments") or "").strip()))

    linhas_at = []
    for quem, lista in por_atendente.items():
        ds = [d for d in (minutos(x) for x in lista) if d is not None]
        ns = [s["satisfaction"] for x in lista for s in (x.get("satisfaction_surveys") or [])
              if isinstance(s.get("satisfaction"), (int, float))]
        linhas_at.append((quem, len(lista), statistics.mean(ds) if ds else None,
                          statistics.mean(ns) if ns else None, len(ns)))
    linhas_at.sort(key=lambda x: -x[1])

    mais_longos = sorted(((minutos(a), a.get("protocol"), nome_atendente(a)) for a in atend
                          if minutos(a) is not None), reverse=True)[:3]
    return dict(total=len(atend), por_canal=por_canal.most_common(), por_hora=por_hora,
                duracoes=duracoes, linhas_at=linhas_at, notas=notas, por_pergunta=por_pergunta,
                baixas=baixas, mais_longos=mais_longos, classificacoes=class_.most_common(5))


# ----------------------------------------------------------------------- texto
def texto(r, dia, fila, nota_baixa):
    d = datetime.strptime(dia, "%Y-%m-%d").strftime("%d/%m/%Y")
    L = ["*Ferreira Telecom - Auditoria de atendimentos*",
         f"Referência: {d} | Gerado em {datetime.now(TZ).strftime('%d/%m/%Y %H:%M')}", "",
         "*Volume*", f"- Atendimentos encerrados: {r['total']}"]
    if r["duracoes"]:
        L.append(f"- Duração média: {dur(statistics.mean(r['duracoes']))} | mediana: "
                 f"{dur(statistics.median(r['duracoes']))} | 90% terminam em até "
                 f"{dur(sorted(r['duracoes'])[int(0.9 * (len(r['duracoes']) - 1))])}")
    if r["por_canal"]:
        L.append("- Por canal: " + ", ".join(f"{n} ({q})" for n, q in r["por_canal"]))
    if r["por_hora"]:
        pico = sorted(r["por_hora"].items(), key=lambda x: -x[1])[:3]
        L.append("- Horários de maior entrada: " + ", ".join(f"{h:02d}h ({q})" for h, q in pico))
    L += ["", "*Por atendente*"]
    for quem, q, dm, nm, nn in r["linhas_at"]:
        L.append(f"- {quem}: {q} atend." + (f", média {dur(dm)}" if dm is not None else "")
                 + (f", nota {nm:.1f} ({nn} resp.)" if nm is not None else ""))
    L += ["", "*Satisfação*"]
    if r["notas"]:
        L.append(f"- Nota média: {statistics.mean(r['notas']):.2f} "
                 f"({len(r['notas'])} respostas; escala vista: {min(r['notas']):g} a {max(r['notas']):g})")
        if len(r["por_pergunta"]) > 1:
            for p, ns in r["por_pergunta"].items():
                L.append(f"    - {p[:50]}: {statistics.mean(ns):.2f} ({len(ns)})")
        if r["baixas"]:
            L.append(f"- Atenção, notas até {nota_baixa}: {len(r['baixas'])}")
            for prot, quem, n, com in r["baixas"][:5]:
                L.append(f"    - Protocolo {prot} | {quem} | nota {n:g}" + (f" | \"{com[:80]}\"" if com else ""))
        else:
            L.append(f"- Nenhuma nota até {nota_baixa}.")
    else:
        L.append("- Nenhuma pesquisa respondida.")
    if r["classificacoes"]:
        L += ["", "*Assuntos (classificações)*"] + [f"- {n}: {q}" for n, q in r["classificacoes"]]
    if r["mais_longos"]:
        L += ["", "*Atendimentos mais longos*"] + \
             [f"- Protocolo {p} | {q} | {dur(m)}" for m, p, q in r["mais_longos"]]
    L += ["", "*Fila neste momento*",
          f"- Aguardando: {fila.get('waiting', '?')} | Em automação: {fila.get('automation', '?')} | "
          f"Em andamento: {fila.get('progress', '?')}"]
    return "\n".join(L)


# -------------------------------------------------------------------- WhatsApp
_ERRO = [""]


def enviar_whatsapp(msg):
    """Reenvia (até 3 vezes, esperando 30s, 90s e 3min) se o Chatmix recusar com 'Destinatario duplicado'."""
    for espera in (0, 30, 90, 180):
        if espera:
            time.sleep(espera)
        _ERRO[0] = ""
        if _enviar_uma(msg):
            return True
        if "duplicado" not in _ERRO[0].lower():
            return False
    print("Chatmix: NÃO entregou a mensagem depois de várias tentativas.", file=sys.stderr)
    return False


def _enviar_uma(msg):
    corpo = json.dumps({"key": os.environ["CHATMIX_KEY"], "token": os.environ["CHATMIX_TOKEN"],
                        "numero": os.environ["WHATSAPP_DESTINO"], "mensagem": msg}).encode("utf-8")
    req = urllib.request.Request(os.environ.get("CHATMIX_URL", "https://envios.bulkv2.chatmix.com.br/api"),
                                 data=corpo, method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            r = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        _det = e.read().decode("utf-8", "ignore")[:300]
        _ERRO[0] = _det
        print(f"Chatmix recusou o envio (erro {e.code}): {_det}", file=sys.stderr)
        return False
    except urllib.error.URLError as e:
        print(f"Não consegui conectar no Chatmix: {e.reason}", file=sys.stderr)
        return False
    ok = bool(r.get("success"))
    print(f"Chatmix: {'aceitou' if ok else 'NÃO aceitou'} - {r.get('message', '')}")
    return ok


# ------------------------------------------------------------------------ main
def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    sem_envio = "--sem-envio" in sys.argv
    try:
        base, token = os.environ["CHATMIX_DOMINIO"].rstrip("/"), os.environ["CHATMIX_PUBLIC"]
    except KeyError as e:
        raise SystemExit(f"Falta definir a variável de ambiente {e.args[0]}.")
    dia = args[0] if args else (datetime.now(TZ) - timedelta(days=1)).strftime("%Y-%m-%d")
    nota_baixa = float(os.environ.get("NOTA_BAIXA", "2"))

    atend = buscar_encerrados(base, token, dia)
    rel = texto(analisar(atend, nota_baixa), dia, fila_agora(base, token), nota_baixa)

    pasta = os.environ.get("RELATORIO_DIR", "relatorios")
    os.makedirs(pasta, exist_ok=True)
    arquivo = os.path.join(pasta, f"auditoria_{dia}.txt")
    with open(arquivo, "w", encoding="utf-8") as f:
        f.write(rel + "\n")
    print(rel, f"\n\nSalvo em {arquivo}")

    if sem_envio or not all(os.environ.get(k) for k in ("CHATMIX_KEY", "CHATMIX_TOKEN", "WHATSAPP_DESTINO")):
        print("Envio por WhatsApp não realizado (modo --sem-envio ou variáveis ausentes).")
    elif not enviar_whatsapp(rel):
        sys.exit(1)


if __name__ == "__main__":
    main()
