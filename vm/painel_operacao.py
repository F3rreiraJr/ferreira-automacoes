#!/usr/bin/env python3
"""
Painel da operação - Ferreira Telecom (dados do SGP, envio por WhatsApp via Chatmix).

Resume o dia ANTERIOR e a agenda de HOJE:
  - OS externas: finalizadas, média, prazo, ranking de técnicos, motivos
  - Agenda de hoje (OS externas agendadas por técnico) e pendências em aberto
  - Contratos: base por situação, novos, ativações, suspensões e cancelamentos do dia

A mensagem NÃO inclui dados de clientes (nome, CPF, telefone, endereço). Só nomes de técnicos.
Usa só a biblioteca padrão do Python 3.9+.

Variáveis de ambiente (as mesmas que você já tem na VM):
    SGP_URL, SGP_APP, SGP_TOKEN
    CHATMIX_KEY, CHATMIX_TOKEN, WHATSAPP_DESTINO   (se faltar alguma, só exibe e salva)

Opcionais:
    TIPO_EXTERNA     nome do tipo de OS de campo no SGP (padrão: Externa)
    DIAS_BACKLOG     quantos dias para trás procurar OS externas ainda abertas (padrão: 180)
    RELATORIO_DIR    pasta de saída (padrão: ./relatorios)

Uso:
    python3 painel_operacao.py                 # dia anterior + agenda de hoje, e envia
    python3 painel_operacao.py --sem-envio     # só exibe e salva (para testar)
    python3 painel_operacao.py 2026-11-10      # outro dia de referência (AAAA-MM-DD); "hoje" = dia seguinte
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
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Fortaleza")
UA = "Mozilla/5.0 (compatible; FerreiraTelecom-Relatorio/1.0)"
DIAS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
STATUS_ABERTA, STATUS_EM_EXECUCAO, STATUS_PENDENTE = 0, 2, 3
SEM_TECNICO = "Sem técnico"


# ------------------------------------------------------------------------- SGP
def post(base, app, token, path, **params):
    dados = {"app": app, "token": token, **{k: v for k, v in params.items() if v not in (None, "")}}
    req = urllib.request.Request(base + path, data=urllib.parse.urlencode(dados).encode(), method="POST",
                                 headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Erro {e.code} em {path}: {e.read().decode('utf-8', 'ignore')[:300]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Não consegui conectar no SGP: {e.reason}")


def lista_os(base, app, token, **filtros):
    itens, offset = [], 0
    for _ in range(300):  # trava de segurança
        r = post(base, app, token, "/api/ura/ordemservico/list/", offset=offset, limit=1000, **filtros)
        lote = r.get("ordens_servicos", []) if isinstance(r, dict) else []
        itens += lote
        total = (r.get("paginacao") or {}).get("total") if isinstance(r, dict) else None
        offset += 1000
        if len(lote) < 1000 or (total is not None and offset >= total):
            break
    return itens


def coletar(base, app, token, ontem, hoje, dias_backlog):
    d = lambda x: x.isoformat()
    fin30 = lista_os(base, app, token, data_finalizacao_inicio=d(ontem - timedelta(days=30)),
                     data_finalizacao_fim=d(ontem))
    backlog = []
    for st in (STATUS_ABERTA, STATUS_EM_EXECUCAO, STATUS_PENDENTE):
        backlog += lista_os(base, app, token, status=st,
                            data_cadastro_inicio=d(hoje - timedelta(days=dias_backlog)),
                            data_cadastro_fim=d(hoje))
    agenda = lista_os(base, app, token, data_agendamento_inicio=d(hoje), data_agendamento_fim=d(hoje))
    contratos = post(base, app, token, "/api/ura/listacontrato/")
    contratos = contratos.get("contratos", contratos) if isinstance(contratos, dict) else contratos
    return fin30, backlog, agenda, contratos


# --------------------------------------------------------------------- análise
def dia(s):
    """'2026-11-10' ou '2026-11-10 08:00:00' -> date (ou None)."""
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def tecnico(o):
    return (o.get("responsavel") or "").strip() or SEM_TECNICO


def analisar(fin30, backlog, agenda, contratos, ontem, hoje, tipo_externa):
    externa = lambda o: (o.get("tipo") or "").strip().lower() == tipo_externa.lower()
    r = {}

    # --- OS finalizadas ontem (externas x internas)
    fin_ontem = [o for o in fin30 if dia(o.get("data_finalizacao")) == ontem]
    ext_ontem = [o for o in fin_ontem if externa(o)]
    r["fin_ontem_total"], r["ext_ontem"], r["int_ontem"] = len(fin_ontem), len(ext_ontem), len(fin_ontem) - len(ext_ontem)

    # --- média das externas nos 30 dias anteriores (dias COM atividade; exclui ontem)
    por_dia = Counter(dia(o.get("data_finalizacao")) for o in fin30 if externa(o) and dia(o.get("data_finalizacao")) != ontem)
    por_dia.pop(None, None)
    r["media30"] = statistics.mean(por_dia.values()) if por_dia else None

    # --- prazo: dias entre cadastro e finalização (só há data, sem hora)
    prazos = []
    for o in ext_ontem:
        c, f = dia(o.get("data_cadastro")), dia(o.get("data_finalizacao"))
        if c and f:
            prazos.append(max((f - c).days, 0))
    r["prazo_medio"] = statistics.mean(prazos) if prazos else None
    r["mesmo_dia"] = (100 * sum(1 for p in prazos if p == 0) // len(prazos)) if prazos else None

    # --- ranking de técnicos
    ontem_t, aux_t = Counter(tecnico(o) for o in ext_ontem), Counter()
    for o in ext_ontem:
        for a in o.get("tecnicos_auxiliares") or []:
            aux_t[str(a).strip()] += 1
    tot30, dias30 = Counter(), defaultdict(set)
    for o in fin30:
        if externa(o) and dia(o.get("data_finalizacao")) != ontem:
            tot30[tecnico(o)] += 1
            dias30[tecnico(o)].add(dia(o.get("data_finalizacao")))
    r["tecnicos"] = [(t, ontem_t[t], aux_t.get(t, 0), tot30[t], (tot30[t] / len(dias30[t])) if dias30[t] else None)
                     for t in sorted(set(ontem_t) | set(tot30) | set(aux_t),
                                     key=lambda t: (-ontem_t[t], -aux_t.get(t, 0), -tot30[t]))
                     if t != SEM_TECNICO or ontem_t[t]]
    r["motivos"] = Counter(o.get("motivo") or "(sem motivo)" for o in ext_ontem).most_common(5)

    # --- agenda de hoje
    ag = [o for o in agenda if externa(o)]
    r["agenda_total"] = len(ag)
    r["agenda_por_tecnico"] = Counter(tecnico(o) for o in ag).most_common()

    # --- pendências externas em aberto (idade = dias desde o cadastro)
    pend = [o for o in backlog if externa(o)]
    idades = [(hoje - dia(o.get("data_cadastro"))).days for o in pend if dia(o.get("data_cadastro"))]
    r["pend_total"] = len(pend)
    r["pend_faixas"] = (sum(1 for i in idades if i <= 2), sum(1 for i in idades if 3 <= i <= 7), sum(1 for i in idades if i > 7))
    r["pend_antiga"] = max(idades) if idades else None

    # --- contratos
    r["base"] = Counter(c.get("status") or "(sem status)" for c in contratos).most_common()
    r["novos"] = sum(1 for c in contratos if dia(c.get("data_cadastro")) == ontem)
    mudou = [c for c in contratos if dia(c.get("data_status")) == ontem]
    r["status_ontem"] = Counter(c.get("status") or "(sem status)" for c in mudou)
    r["motivos_ontem"] = {st: Counter(c.get("motivo_status") or "(sem motivo)" for c in mudou if c.get("status") == st).most_common(3)
                          for st in ("Cancelado", "Suspenso")}
    return r


# ----------------------------------------------------------------------- texto
def n1(v):
    return f"{v:.1f}".replace(".", ",")


def texto(r, ontem, hoje):
    L = ["*Ferreira Telecom - Painel da operação*",
         f"Referência: {DIAS[ontem.weekday()]}, {ontem:%d/%m/%Y} | Gerado em {datetime.now(TZ):%d/%m/%Y %H:%M}", "",
         "*OS externas (campo)*"]
    ref = f" (média dos 30 dias: {n1(r['media30'])}/dia com atividade)" if r["media30"] is not None else ""
    L.append(f"- Finalizadas ontem: {r['ext_ontem']}{ref}")
    if r["prazo_medio"] is not None:
        L.append(f"- Prazo médio de conclusão: {n1(r['prazo_medio'])} dias ({r['mesmo_dia']}% no mesmo dia do cadastro)")
    if r["tecnicos"]:
        L.append("- Por técnico (ontem | 30 dias):")
        for t, o, aux, t30, med in r["tecnicos"]:
            L.append(f"    - {t}: {o}" + (f" (+{aux} como auxiliar)" if aux else "")
                     + f" | {t30}" + (f", média {n1(med)}/dia" if med else ""))
    if r["motivos"]:
        L.append("- Principais motivos ontem: " + "; ".join(f"{m[:35]} ({q})" for m, q in r["motivos"]))

    L += ["", f"*Agenda de hoje ({DIAS[hoje.weekday()]})*", f"- OS externas agendadas: {r['agenda_total']}"]
    if r["agenda_por_tecnico"]:
        L.append("    " + ", ".join(f"{t} ({q})" for t, q in r["agenda_por_tecnico"]))
    a, b, c = r["pend_faixas"]
    L += [f"- Externas em aberto: {r['pend_total']}" + (f" (até 2 dias: {a} | 3 a 7: {b} | mais de 7: {c}; "
                                                         f"a mais antiga tem {r['pend_antiga']} dias)" if r["pend_total"] else "")]

    L += ["", "*OS internas*", f"- Finalizadas ontem: {r['int_ontem']}", "", "*Contratos*",
          "- Base atual: " + " | ".join(f"{s} {q}" for s, q in r["base"]),
          f"- Ontem: novos cadastros {r['novos']} | passaram a Ativo {r['status_ontem'].get('Ativo', 0)} | "
          f"suspensos {r['status_ontem'].get('Suspenso', 0)} | cancelados {r['status_ontem'].get('Cancelado', 0)}"]
    for st, rotulo in (("Cancelado", "Motivos dos cancelamentos"), ("Suspenso", "Motivos das suspensões")):
        if r["motivos_ontem"][st]:
            L.append(f"    - {rotulo}: " + "; ".join(f"{m[:30]} ({q})" for m, q in r["motivos_ontem"][st]))
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
        base, app, token = (os.environ[k] for k in ("SGP_URL", "SGP_APP", "SGP_TOKEN"))
    except KeyError as e:
        raise SystemExit(f"Falta definir a variável de ambiente {e.args[0]}.")
    base = base.rstrip("/")
    agora = datetime.now(TZ).date()
    ontem = date.fromisoformat(args[0]) if args else agora - timedelta(days=1)
    hoje = ontem + timedelta(days=1)

    dados = coletar(base, app, token, ontem, hoje, int(os.environ.get("DIAS_BACKLOG", "180")))
    msg = texto(analisar(*dados, ontem, hoje, os.environ.get("TIPO_EXTERNA", "Externa")), ontem, hoje)

    pasta = os.environ.get("RELATORIO_DIR", "relatorios")
    os.makedirs(pasta, exist_ok=True)
    arquivo = os.path.join(pasta, f"painel_operacao_{hoje.isoformat()}.txt")
    with open(arquivo, "w", encoding="utf-8") as f:
        f.write(msg + "\n")
    print(msg, f"\n\nSalvo em {arquivo}")

    if sem_envio or not all(os.environ.get(k) for k in ("CHATMIX_KEY", "CHATMIX_TOKEN", "WHATSAPP_DESTINO")):
        print("Envio por WhatsApp não realizado (modo --sem-envio ou variáveis ausentes).")
    elif not enviar_whatsapp(msg):
        sys.exit(1)  # sai com erro para ficar registrado no log


if __name__ == "__main__":
    main()
