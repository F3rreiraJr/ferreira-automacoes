#!/usr/bin/env python3
"""
Resumo de gestão - Ferreira Telecom (SGP + Chatmix, enviado por WhatsApp).

Por padrão é DIÁRIO: olha o dia ANTERIOR e compara com o MESMO DIA DA SEMANA PASSADA.
Com --semanal, olha os 7 dias anteriores e compara com a semana anterior a essa.
Vai em DUAS mensagens:
  1) Financeiro e clientes
  2) Operação e atendimento

A mensagem NÃO inclui dados de clientes (nome, CPF, telefone, endereço). Só nomes de técnicos e atendentes.
Usa só a biblioteca padrão do Python 3.9+.

Variáveis de ambiente (as mesmas que você já tem na VM):
    SGP_URL, SGP_APP, SGP_TOKEN
    TELEGRAM_TOKEN, TELEGRAM_USER_ID   (se faltar alguma, só exibe e salva) (envio só pelo Telegram desde 17/11/2026)
    CHATMIX_DOMINIO, CHATMIX_PUBLIC                (se faltar, a parte do Chatmix é pulada)

Opcionais:
    TIPO_EXTERNA        nome do tipo de OS de campo (padrão: Externa)
    NOTA_BAIXA          notas até este valor entram em "atenção" (padrão: 2, supondo escala 1 a 5)
    CHATMIX_PAUSA_SEG   pausa entre páginas do Chatmix (padrão: 31; o limite deles é 2/min)
    RELATORIO_DIR       pasta de saída e de histórico (padrão: ./relatorios)

As comparações "há 7 dias" de inadimplência, base de contratos e nota do Chatmix usam o histórico
guardado em RELATORIO_DIR/historico_resumo.json. O histórico só é gravado quando o relatório é ENVIADO
(não no modo --sem-envio), então testar não atrapalha. Nos primeiros 7 dias não há comparação.

Uso:
    python3 resumo_gestao.py                 # diário: dia anterior, e envia
    python3 resumo_gestao.py --semanal       # últimos 7 dias, e envia
    python3 resumo_gestao.py --sem-envio     # só exibe e salva (para testar)
    python3 resumo_gestao.py 2026-11-16      # simula a execução em outro dia (AAAA-MM-DD)
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
SEM_TECNICO = "Sem técnico"
STATUS_ABERTA, STATUS_EM_EXECUCAO, STATUS_PENDENTE = 0, 2, 3


# ------------------------------------------------------------------------- SGP
def sgp(base, app, token, path, **params):
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


def paginar(base, app, token, path, chave, limite, **filtros):
    itens, offset = [], 0
    for _ in range(300):  # trava de segurança
        r = sgp(base, app, token, path, offset=offset, limit=limite, **filtros)
        lote = r.get(chave, []) if isinstance(r, dict) else []
        itens += lote
        total = (r.get("paginacao") or {}).get("total") if isinstance(r, dict) else None
        offset += limite
        if len(lote) < limite or (total is not None and offset >= total):
            break
    return itens


def coletar_sgp(b, a, t, s_ini, s_fim, p_ini, p_fim, hoje):
    d = lambda x: x.isoformat()
    tit = lambda status, **f: paginar(b, a, t, "/api/ura/titulos/", "titulos", 250, status=status, **f)
    oc = lambda **f: paginar(b, a, t, "/api/ura/ocorrencia/list/", "ocorrencias", 1000, **f)
    contratos = sgp(b, a, t, "/api/ura/listacontrato/")
    return dict(
        pagos_sem=tit("pagos", data_pagamento_inicio=d(s_ini), data_pagamento_fim=d(s_fim)),
        pagos_ant=tit("pagos", data_pagamento_inicio=d(p_ini), data_pagamento_fim=d(p_fim)),
        inad=tit("abertos", data_vencimento_inicio=d(hoje - timedelta(days=365)),
                 data_vencimento_fim=d(hoje - timedelta(days=1))),
        a_vencer=tit("abertos", data_vencimento_inicio=d(hoje), data_vencimento_fim=d(hoje + timedelta(days=6))),
        contas_prox=paginar(b, a, t, "/api/ura/contas/pagar/", "contas", 500, status=1,
                            data_vencimento_inicio=d(hoje), data_vencimento_fim=d(hoje + timedelta(days=6))),
        # atrasadas = "Atrasada" (4) + "Pendente" (1) vencidas, só as em aberto e dos últimos JANELA_ATRASO_DIAS dias
        contas_atr=[c for st in (4, 1) for c in paginar(b, a, t, "/api/ura/contas/pagar/", "contas", 500, status=st,
                    data_vencimento_inicio=d(hoje - timedelta(days=int(os.environ.get("JANELA_ATRASO_DIAS", "180")))),
                    data_vencimento_fim=d(hoje - timedelta(days=1)))
                    if not any(w in str(c.get("status") or "").lower() for w in ("quit", "cancel", "pago"))
                    and not str(c.get("data_pagamento") or "").strip()],
        oc_novas=oc(data_cadastro_inicio=d(s_ini), data_cadastro_fim=d(s_fim)),
        oc_enc=oc(data_finalizacao_inicio=d(s_ini), data_finalizacao_fim=d(s_fim)),
        oc_pend=sum(len(oc(status=st)) for st in (STATUS_ABERTA, STATUS_EM_EXECUCAO, STATUS_PENDENTE)),
        os14=paginar(b, a, t, "/api/ura/ordemservico/list/", "ordens_servicos", 1000,
                     data_finalizacao_inicio=d(p_ini), data_finalizacao_fim=d(s_fim)),
        contratos=contratos.get("contratos", contratos) if isinstance(contratos, dict) else contratos)


# ----------------------------------------------------------------------- Chatmix
def chatmix_get(base, token, path, params, tentativas=3):
    req = urllib.request.Request(f"{base}/api-v2/public-api{path}?{urllib.parse.urlencode(params)}",
                                 headers={"X-Auth": token, "Accept": "application/json", "User-Agent": UA})
    for i in range(tentativas):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and i < tentativas - 1:
                time.sleep(int(os.environ.get("CHATMIX_PAUSA_SEG", "31")) * 2)
                continue
            raise SystemExit(f"Erro {e.code} no Chatmix ({path}): {e.read().decode('utf-8', 'ignore')[:200]}")
        except urllib.error.URLError as e:
            raise SystemExit(f"Não consegui conectar no Chatmix: {e.reason}")


def chatmix_semana(base, token, s_ini, s_fim):
    pausa, itens, pag = int(os.environ.get("CHATMIX_PAUSA_SEG", "31")), [], 1
    while True:
        r = chatmix_get(base, token, "/attendances/closed",
                        {"date_start": f"{s_ini} 00:00:00", "date_end": f"{s_fim} 23:59:59", "per_page": 50, "page": pag})
        itens += r.get("data", [])
        if pag >= (r.get("meta") or {}).get("last_page", 1) or pag >= 200:
            return itens
        pag += 1
        time.sleep(pausa)


# ----------------------------------------------------------------------- análise
def dia(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def ts(s):
    return datetime.strptime(str(s)[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S")


def vtit(t):
    return float(t.get("valorCorrigido") or t.get("valor") or 0)


def tecnico(o):
    return (o.get("responsavel") or "").strip() or SEM_TECNICO


def analisar(c, s_ini, s_fim, p_ini, p_fim, hoje, tipo_externa):
    m = {}
    # --- financeiro
    pago = lambda ts_: sum(float(t.get("valorPago") or 0) for t in ts_)
    m["rec_valor"], m["rec_qtd"] = pago(c["pagos_sem"]), len(c["pagos_sem"])
    m["rec_valor_ant"], m["rec_qtd_ant"] = pago(c["pagos_ant"]), len(c["pagos_ant"])
    m["inad_valor"], m["inad_qtd"] = sum(vtit(t) for t in c["inad"]), len(c["inad"])
    m["inad_clientes"] = len({t.get("clienteCpfcnpj") or t.get("clienteContrato") for t in c["inad"]})
    m["inad_60_valor"] = sum(vtit(t) for t in c["inad"]
                             if dia(t.get("dataVencimento")) and (hoje - dia(t["dataVencimento"])).days > 60)
    m["vencer_valor"], m["vencer_qtd"] = sum(vtit(t) for t in c["a_vencer"]), len(c["a_vencer"])
    cv = lambda x: float(x.get("valor") or 0)
    m["cp_valor"], m["cp_qtd"] = sum(cv(x) for x in c["contas_prox"]), len(c["contas_prox"])
    m["cp_atr_valor"], m["cp_atr_qtd"] = sum(cv(x) for x in c["contas_atr"]), len(c["contas_atr"])

    # --- contratos (eventos = contratos cuja ÚLTIMA mudança de situação caiu na semana)
    base = Counter(x.get("status") or "(sem status)" for x in c["contratos"])
    m["base"] = dict(base)
    na_semana = lambda x: dia(x.get("data_status")) and s_ini <= dia(x["data_status"]) <= s_fim
    ev = Counter(x.get("status") for x in c["contratos"] if na_semana(x))
    m["novos"] = sum(1 for x in c["contratos"] if dia(x.get("data_cadastro")) and s_ini <= dia(x["data_cadastro"]) <= s_fim)
    m["cancelados"], m["suspensos"], m["ativados"] = ev.get("Cancelado", 0), ev.get("Suspenso", 0), ev.get("Ativo", 0)
    m["motivos_cancel"] = Counter((x.get("motivo_status") or "(sem motivo)") for x in c["contratos"]
                                  if na_semana(x) and x.get("status") == "Cancelado").most_common(4)

    # --- chamados do SGP
    m["oc_novas"], m["oc_enc"], m["oc_pend"] = len(c["oc_novas"]), len(c["oc_enc"]), c["oc_pend"]

    # --- ordens de serviço
    ext = lambda o: (o.get("tipo") or "").strip().lower() == tipo_externa.lower()
    na = lambda o, a, b: dia(o.get("data_finalizacao")) and a <= dia(o["data_finalizacao"]) <= b
    ext_s = [o for o in c["os14"] if ext(o) and na(o, s_ini, s_fim)]
    ext_a = [o for o in c["os14"] if ext(o) and na(o, p_ini, p_fim)]
    m["os_ext"], m["os_ext_ant"] = len(ext_s), len(ext_a)
    m["os_int"] = sum(1 for o in c["os14"] if not ext(o) and na(o, s_ini, s_fim))
    dias_ativos = {dia(o["data_finalizacao"]) for o in ext_s}
    m["os_media_dia"] = len(ext_s) / len(dias_ativos) if dias_ativos else None
    prazos = [max((dia(o["data_finalizacao"]) - dia(o["data_cadastro"])).days, 0) for o in ext_s
              if dia(o.get("data_cadastro")) and dia(o.get("data_finalizacao"))]
    m["os_prazo"] = statistics.mean(prazos) if prazos else None
    tec, aux, tec_dias, tec_ant = Counter(), Counter(), defaultdict(set), Counter(tecnico(o) for o in ext_a)
    for o in ext_s:
        tec[tecnico(o)] += 1
        tec_dias[tecnico(o)].add(dia(o["data_finalizacao"]))
        for x in o.get("tecnicos_auxiliares") or []:
            aux[str(x).strip()] += 1
    m["tecnicos"] = [(t, tec[t], aux.get(t, 0), tec[t] / len(tec_dias[t]) if tec_dias[t] else None, tec_ant.get(t, 0))
                     for t in sorted(set(tec) | set(aux), key=lambda t: (-tec[t], -aux.get(t, 0)))]
    m["os_motivos"] = Counter(o.get("motivo") or "(sem motivo)" for o in ext_s).most_common(3)
    return m


def analisar_chatmix(atend, nota_baixa):
    r = {"total": len(atend)}
    dur = []
    for a in atend:
        try:
            dur.append(max((ts(a["closed_at"]) - ts(a["created_at"])).total_seconds() / 60, 0))
        except (KeyError, ValueError, TypeError):
            pass
    r["dur_media"], r["dur_mediana"] = (statistics.mean(dur), statistics.median(dur)) if dur else (None, None)
    notas = [s["satisfaction"] for a in atend for s in (a.get("satisfaction_surveys") or [])
             if isinstance(s.get("satisfaction"), (int, float))]
    r["nota_media"], r["notas_qtd"] = (statistics.mean(notas) if notas else None), len(notas)
    r["notas_baixas"] = sum(1 for n in notas if n <= nota_baixa)
    por = defaultdict(list)
    for a in atend:
        u = a.get("user") or {}
        por[(f"{u.get('first_name') or ''} {u.get('last_name') or ''}".strip()) or "Sem atendente (automação)"].append(a)
    linhas = []
    for nome, lista in por.items():
        ns = [s["satisfaction"] for a in lista for s in (a.get("satisfaction_surveys") or [])
              if isinstance(s.get("satisfaction"), (int, float))]
        linhas.append((nome, len(lista), statistics.mean(ns) if ns else None, len(ns)))
    r["atendentes"] = sorted(linhas, key=lambda x: -x[1])
    return r


# ----------------------------------------------------------------------- histórico
def carregar_hist(pasta):
    try:
        with open(os.path.join(pasta, "historico_resumo.json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def snapshot_anterior(hist, hoje):
    """Registro mais próximo de 7 dias atrás (entre 4 e 10 dias)."""
    alvo, melhor = hoje - timedelta(days=7), None
    for k, v in hist.items():
        dk = dia(k)
        if dk and 4 <= (hoje - dk).days <= 10 and (melhor is None or abs((dk - alvo).days) < abs((dia(melhor) - alvo).days)):
            melhor = k
    return hist.get(melhor) if melhor else None


def salvar_hist(pasta, hist, hoje, m, ch):
    hist[hoje.isoformat()] = {"inad_valor": m["inad_valor"], "inad_qtd": m["inad_qtd"], "base": m["base"],
                              "novos": m["novos"], "cancelados": m["cancelados"], "suspensos": m["suspensos"],
                              "ativados": m["ativados"], "chatmix_total": ch["total"] if ch else None,
                              "chatmix_nota": ch["nota_media"] if ch else None}
    for k in sorted(hist)[:-120]:  # guarda ~4 meses de histórico diário
        del hist[k]
    os.makedirs(pasta, exist_ok=True)
    with open(os.path.join(pasta, "historico_resumo.json"), "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=1)


# ------------------------------------------------------------------------- texto
def brl(v):
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def n1(v):
    return f"{v:.1f}".replace(".", ",")


def vs(atual, antes, rotulo):
    if not antes:
        return ""
    p = round(100 * (atual - antes) / antes)
    return f" ({'+' if p >= 0 else ''}{p}% {rotulo})"


def dif(atual, antes):
    if antes is None:
        return ""
    return f" ({atual - antes:+.0f})"


def n2(v):
    return f"{v:.2f}".replace(".", ",")


def periodo(modo, s_ini, s_fim):
    if modo["dias"] == 1:
        return f"Referência: {modo['nome_dia'][s_fim.weekday()]}, {s_fim:%d/%m/%Y}"
    return f"Semana de {s_ini:%d/%m} a {s_fim:%d/%m/%Y}"


def dur(m):
    m = round(m)
    return f"{m} min" if m < 60 else f"{m // 60}h{m % 60:02d}"


def parte1(m, ant, s_ini, s_fim, hoje, modo):
    L = [f"*Ferreira Telecom - {modo['titulo']} (1/2)*", periodo(modo, s_ini, s_fim),
         "", "*Financeiro*",
         f"- Recebido: {brl(m['rec_valor'])} ({m['rec_qtd']} faturas){vs(m['rec_valor'], m['rec_valor_ant'], modo['vs'])}",
         f"- Inadimplência hoje: {brl(m['inad_valor'])} em {m['inad_qtd']} faturas / {m['inad_clientes']} clientes"
         + (f" | há 7 dias: {brl(ant['inad_valor'])}" if ant else ""),
         f"    - com mais de 60 dias: {brl(m['inad_60_valor'])}",
         f"- A receber nos próximos 7 dias: {brl(m['vencer_valor'])} ({m['vencer_qtd']} faturas)",
         f"- Contas a pagar nos próximos 7 dias: {brl(m['cp_valor'])} ({m['cp_qtd']} contas)",
         f"- Contas atrasadas: {brl(m['cp_atr_valor'])} ({m['cp_atr_qtd']} contas)", "", "*Clientes (contratos)*"]
    b0 = ant["base"] if ant else {}
    L.append("- Base: " + " | ".join(f"{s} {q}{dif(q, b0.get(s)) if ant else ''}"
                                     for s, q in sorted(m["base"].items(), key=lambda x: -x[1])))
    L.append(f"- {modo['no']}: novos cadastros {m['novos']} | passaram a Ativo {m['ativados']} | "
             f"suspensos {m['suspensos']} | cancelados {m['cancelados']}")
    L.append(f"- Saldo (novos - cancelados): {m['novos'] - m['cancelados']:+d}")
    if m["motivos_cancel"]:
        L.append("- Motivos dos cancelamentos: " + "; ".join(f"{x[:30]} ({q})" for x, q in m["motivos_cancel"]))
    if not ant:
        L.append("(Ainda sem histórico: as comparações \"há 7 dias\" aparecem a partir da próxima semana.)")
    return "\n".join(L)


def parte2(m, ch, ant, s_ini, s_fim, modo):
    L = [f"*Ferreira Telecom - {modo['titulo']} (2/2)*", periodo(modo, s_ini, s_fim), "",
         "*OS externas (campo)*",
         f"- Finalizadas: {m['os_ext']}{vs(m['os_ext'], m['os_ext_ant'], modo['vs'])}"
         + (f" | média {n1(m['os_media_dia'])}/dia com atividade" if modo["dias"] > 1 and m["os_media_dia"] else ""),
         f"- OS internas finalizadas: {m['os_int']}"]
    if m["os_prazo"] is not None:
        L.append(f"- Prazo médio de conclusão: {n1(m['os_prazo'])} dias")
    if m["tecnicos"]:
        L.append(f"- Por técnico ({modo['col']}):")
        for t, q, aux, med, q_ant in m["tecnicos"]:
            L.append(f"    - {t}: {q}" + (f" (+{aux} como auxiliar)" if aux else "")
                     + (f", média {n1(med)}/dia" if modo["dias"] > 1 and med else "") + f" | {q_ant}")
    if m["os_motivos"]:
        L.append("- Principais motivos: " + "; ".join(f"{x[:35]} ({q})" for x, q in m["os_motivos"]))
    L += ["", "*Chamados (SGP)*",
          f"- Abertos na semana: {m['oc_novas']} | encerrados: {m['oc_enc']} | ainda pendentes hoje: {m['oc_pend']}", "",
          "*Atendimento (Chatmix)*"]
    if ch is None:
        L.append("- Não consultado (faltam CHATMIX_DOMINIO e CHATMIX_PUBLIC).")
    else:
        L.append(f"- Encerrados: {ch['total']}{vs(ch['total'], ant.get('chatmix_total'), 'vs há 7 dias') if ant and modo['dias'] == 1 else ''}"
                 + (f" | média {n1(ch['total'] / modo['dias'])}/dia" if modo["dias"] > 1 else ""))
        if ch["dur_media"] is not None:
            L.append(f"- Duração média: {dur(ch['dur_media'])} | mediana: {dur(ch['dur_mediana'])}")
        if ch["nota_media"] is not None:
            L.append(f"- Nota média: {n2(ch['nota_media'])} ({ch['notas_qtd']} respostas)"
                     + (f" | há 7 dias: {n2(ant['chatmix_nota'])}" if ant and ant.get("chatmix_nota") else "")
                     + f" | notas baixas: {ch['notas_baixas']}")
        for nome, q, nm, nn in ch["atendentes"][:8]:
            L.append(f"    - {nome}: {q} atend." + (f", nota {nm:.1f} ({nn})" if nm is not None else ""))
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
        b, a, t = (os.environ[k] for k in ("SGP_URL", "SGP_APP", "SGP_TOKEN"))
    except KeyError as e:
        raise SystemExit(f"Falta definir a variável de ambiente {e.args[0]}.")
    b = b.rstrip("/")
    hoje = date.fromisoformat(args[0]) if args else datetime.now(TZ).date()
    semanal = "--semanal" in sys.argv
    dias = 7 if semanal else 1
    s_ini, s_fim = hoje - timedelta(days=dias), hoje - timedelta(days=1)
    p_ini, p_fim = s_ini - timedelta(days=7), s_fim - timedelta(days=7)  # mesmo dia da semana passada (ou semana anterior)
    modo = {"dias": dias, "nome_dia": ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"],
            "titulo": "Resumo semanal" if semanal else "Resumo diário",
            "vs": "vs semana anterior" if semanal else "vs mesmo dia da semana passada",
            "col": "semana | semana anterior" if semanal else "ontem | mesmo dia da semana passada",
            "no": "Na semana" if semanal else "Ontem"}
    pasta = os.environ.get("RELATORIO_DIR", "relatorios")

    m = analisar(coletar_sgp(b, a, t, s_ini, s_fim, p_ini, p_fim, hoje), s_ini, s_fim, p_ini, p_fim, hoje,
                 os.environ.get("TIPO_EXTERNA", "Externa"))
    ch = None
    if os.environ.get("CHATMIX_DOMINIO") and os.environ.get("CHATMIX_PUBLIC"):
        ch = analisar_chatmix(chatmix_semana(os.environ["CHATMIX_DOMINIO"].rstrip("/"), os.environ["CHATMIX_PUBLIC"],
                                             s_ini, s_fim), float(os.environ.get("NOTA_BAIXA", "2")))
    hist = carregar_hist(pasta)
    ant = snapshot_anterior(hist, hoje)
    msgs = [parte1(m, ant, s_ini, s_fim, hoje, modo), parte2(m, ch, ant, s_ini, s_fim, modo)]

    os.makedirs(pasta, exist_ok=True)
    arquivo = os.path.join(pasta, f"{'resumo_semanal' if semanal else 'resumo_diario'}_{hoje.isoformat()}.txt")
    with open(arquivo, "w", encoding="utf-8") as f:
        f.write("\n\n--------\n\n".join(msgs) + "\n")
    print("\n\n--------\n\n".join(msgs), f"\n\nSalvo em {arquivo}")

    if sem_envio or not all(os.environ.get(k) for k in ("TELEGRAM_TOKEN", "TELEGRAM_USER_ID")):
        print("Envio por WhatsApp não realizado (modo --sem-envio ou variáveis ausentes). Histórico não gravado.")
        return
    for i, msg in enumerate(msgs):
        if i:
            time.sleep(5)  # dá tempo da 1ª mensagem sair antes da 2ª
        if not enviar_whatsapp(msg):
            sys.exit(1)  # sai com erro para ficar registrado no log
    salvar_hist(pasta, hist, hoje, m, ch)


if __name__ == "__main__":
    main()
