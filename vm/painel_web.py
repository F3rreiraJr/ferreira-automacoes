#!/usr/bin/env python3
"""
Painel web da Ferreira Telecom: uma página só para o dono acompanhar contas a pagar, OS e técnicos, inadimplência,
contratos, atendimento (Chatmix) e a saúde dos agentes.

SÓ LEITURA. Não altera nada no SGP/Chatmix e não envia mensagem a ninguém.
A página tem SÓ contagens, valores, números de OS/protocolo, motivos e nomes de fornecedores e técnicos.
NENHUM nome de cliente, CPF, telefone ou endereço (nem na página, nem no cache, nem no log).

Dois modos:
  python3 painel_web.py --gerar [--saida ARQUIVO]   coleta os dados e escreve a página (o cron roda a cada 10 min)
  python3 painel_web.py --servir                    serve a página com usuário e senha (serviço painel-web)

Variáveis: SGP_*, CHATMIX_DOMINIO, CHATMIX_PUBLIC (opcional), RELATORIO_DIR
  PAINEL_SENHA   senha de acesso (obrigatória para --servir, mínimo 10 caracteres)
  PAINEL_USUARIO usuário (padrão: dono)
  PAINEL_PORTA   porta (padrão 8443 com HTTPS, 8080 sem)
  PAINEL_BIND    endereço em que escuta (padrão 0.0.0.0; sem HTTPS o instalador usa só o IP interno da VM)
  PAINEL_CERT / PAINEL_KEY   certificado HTTPS (arquivos .pem). Sem eles, o painel só aceita acesso pela rede local
  PAINEL_CHATMIX=0           não consulta o Chatmix
"""
import base64
import hmac
import html
import json
import os
import ssl
import sys
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import comum as c

esc = html.escape
PASTA = os.path.join(os.environ.get("RELATORIO_DIR", "relatorios"), "painel")
ARQ_PAGINA = "index.html"
CACHE = "painel_cache.json"
FAIXAS = ((1, 7, "1 a 7 dias"), (8, 30, "8 a 30 dias"), (31, 60, "31 a 60 dias"), (61, 10 ** 6, "Mais de 60 dias"))


# ============================================================================ coleta
def com_cache(cache, chave, ttl_min, fn):
    item = cache.get(chave)
    agora = c.agora()
    if item:
        try:
            if agora - datetime.fromisoformat(item["em"]) < timedelta(minutes=ttl_min):
                return item["dados"]
        except (KeyError, ValueError):
            pass
    try:
        dados = fn()
    except (Exception, SystemExit):
        if item:
            return item["dados"]  # usa o dado antigo se a consulta nova falhar
        raise
    cache[chave] = {"em": agora.isoformat(timespec="seconds"), "dados": dados}
    return dados


def coletar_contas(hoje):
    import contas_a_pagar_dia as cp
    base, app, token = (os.environ[k] for k in ("SGP_URL", "SGP_APP", "SGP_TOKEN"))
    base = base.rstrip("/")
    ate = hoje + timedelta(days=6)
    pend = [x for x in cp.contas(base, app, token, status=cp.STATUS_PENDENTE, data_vencimento_inicio=hoje.isoformat(),
                                 data_vencimento_fim=ate.isoformat()) if cp.em_aberto(x)]
    por_dia = defaultdict(list)
    for x in pend:
        por_dia[str(x.get("data_vencimento") or "")[:10]].append(x)
    dias = []
    for i in range(7):
        d = hoje + timedelta(days=i)
        lst = por_dia.get(d.isoformat(), [])
        dias.append({"data": d.isoformat(), "rotulo": c.DIAS[d.weekday()][:3].capitalize() + d.strftime(" %d/%m"),
                     "qtd": len(lst), "total": sum(cp.valor(x) for x in lst)})
    lista_hoje = sorted(por_dia.get(hoje.isoformat(), []), key=lambda x: -cp.valor(x))[:8]
    atr = cp.atrasadas_ate(base, app, token, hoje)
    top_atr = sorted(atr, key=lambda x: -cp.valor(x))[:6]
    return {"dias": dias, "hoje": [cp.linha(x)[2:] for x in lista_hoje],
            "atrasadas_qtd": len(atr), "atrasadas_total": sum(cp.valor(x) for x in atr),
            "atrasadas_top": [cp.linha(x)[2:] + cp.atraso(x, hoje) for x in top_atr],
            "semana_total": sum(d["total"] for d in dias)}


def coletar_os(hoje):
    dias_b = int(os.environ.get("DIAS_BACKLOG", "120"))
    aberta = []
    for st in (0, 2, 3):
        aberta += c.lista_os(status=st, data_cadastro_inicio=(hoje - timedelta(days=dias_b)).isoformat(),
                             data_cadastro_fim=hoje.isoformat())
    ext = [o for o in aberta if c.eh_externa(o)]
    agenda = [o for o in c.lista_os(data_agendamento_inicio=hoje.isoformat(), data_agendamento_fim=hoje.isoformat())
              if c.eh_externa(o)]
    fin7 = [o for o in c.lista_os(data_finalizacao_inicio=(hoje - timedelta(days=6)).isoformat(),
                                  data_finalizacao_fim=hoje.isoformat()) if c.eh_externa(o)]

    def idade(o):
        cad = c.dia(o.get("data_cadastro"))
        return max((hoje - cad).days, 0) if cad else 0

    faixas = Counter()
    for o in ext:
        i = idade(o)
        faixas["0 a 2 dias" if i <= 2 else "3 a 7 dias" if i <= 7 else "8 a 30 dias" if i <= 30 else "Mais de 30 dias"] += 1
    antigas = sorted(ext, key=lambda o: -idade(o))[:8]
    fin_hoje = [o for o in fin7 if c.dia(o.get("data_finalizacao")) == hoje]
    tecs = defaultdict(lambda: {"agendadas": 0, "execucao": 0, "fechadas_hoje": 0, "fechadas_7d": 0})
    for o in agenda:
        if c.tecnico(o) and o.get("status_id") != 1:
            tecs[c.tecnico(o)]["agendadas"] += 1
    for o in ext:
        if o.get("status_id") == 2 and c.tecnico(o):
            tecs[c.tecnico(o)]["execucao"] += 1
    for o in fin7:
        if c.tecnico(o):
            tecs[c.tecnico(o)]["fechadas_7d"] += 1
            if c.dia(o.get("data_finalizacao")) == hoje:
                tecs[c.tecnico(o)]["fechadas_hoje"] += 1
    por_dia = Counter(c.dia(o.get("data_finalizacao")) for o in fin7)
    serie = []
    for i in range(6, -1, -1):
        d = hoje - timedelta(days=i)
        serie.append({"rotulo": c.DIAS[d.weekday()][:3].capitalize() + d.strftime(" %d/%m"), "qtd": por_dia.get(d, 0)})
    return {"total": len(ext), "sem_tecnico": sum(1 for o in ext if not c.tecnico(o)),
            "internas": sum(1 for o in aberta if not c.eh_externa(o)),
            "por_status": dict(Counter(c.STATUS_OS.get(o.get("status_id"), "?") for o in ext)),
            "faixas": [{"nome": n, "qtd": faixas.get(n, 0)} for n in ("0 a 2 dias", "3 a 7 dias", "8 a 30 dias", "Mais de 30 dias")],
            "antigas": [{"id": o.get("id"), "prot": o.get("ocorrencia") or "", "motivo": (o.get("motivo") or "sem motivo")[:60],
                         "idade": idade(o), "estado": c.STATUS_OS.get(o.get("status_id"), "?"),
                         "tecnico": c.tecnico(o) or "sem técnico"} for o in antigas],
            "agendadas_hoje": len(agenda), "fechadas_hoje": len(fin_hoje),
            "tecnicos": [dict(nome=k, **v) for k, v in sorted(tecs.items(), key=lambda kv: (-kv[1]["fechadas_7d"], kv[0]))],
            "serie": serie}


def coletar_inad(hoje):
    janela = int(os.environ.get("JANELA_INADIMPLENCIA_DIAS", "365"))
    venc = c.titulos("abertos", data_vencimento_inicio=(hoje - timedelta(days=janela)).isoformat(),
                     data_vencimento_fim=(hoje - timedelta(days=1)).isoformat())
    prox = c.titulos("abertos", data_vencimento_inicio=hoje.isoformat(), data_vencimento_fim=(hoje + timedelta(days=1)).isoformat())
    faixas = []
    for ini, fim, nome in FAIXAS:
        sel = [t for t in venc if c.dia(t.get("dataVencimento")) and ini <= (hoje - c.dia(t["dataVencimento"])).days <= fim]
        faixas.append({"nome": nome, "qtd": len(sel), "total": sum(c.vtit(t) for t in sel)})
    ciclos = defaultdict(list)
    for t in venc:
        d = c.dia(t.get("dataVencimento"))
        ciclos[d.day if d and d.day in (5, 10, 15, 20, 25, 30) else 0].append(t)
    ciclo_l = [{"nome": ("Dia %d" % k) if k else "Outros dias", "qtd": len(v), "total": sum(c.vtit(t) for t in v)}
               for k, v in sorted(ciclos.items(), key=lambda kv: -sum(c.vtit(t) for t in kv[1]))]
    vencem = {}
    for rot, d in (("hoje", hoje), ("amanha", hoje + timedelta(days=1))):
        sel = [t for t in prox if c.dia(t.get("dataVencimento")) == d]
        vencem[rot] = {"qtd": len(sel), "total": sum(c.vtit(t) for t in sel)}
    return {"total": sum(c.vtit(t) for t in venc), "qtd": len(venc),
            "clientes": len({t.get("clienteContrato") or t.get("clienteCpfcnpj") for t in venc}),
            "faixas": faixas, "ciclos": ciclo_l, "vencem": vencem}


def coletar_contratos(hoje):
    r = c.sgp("/api/ura/listacontrato/")
    lista = r.get("contratos", r) if isinstance(r, dict) else r
    lista = [x for x in lista if isinstance(x, dict)]
    base = Counter(x.get("status") or "(sem status)" for x in lista)
    ontem = hoje - timedelta(days=1)
    ini7 = hoje - timedelta(days=7)

    def conta(campo, status=None, de=ontem, ate=ontem):
        return sum(1 for x in lista if c.dia(x.get(campo)) and de <= c.dia(x[campo]) <= ate
                   and (status is None or x.get("status") == status))
    ev = lambda de, ate: {"novos": conta("data_cadastro", None, de, ate), "cancelados": conta("data_status", "Cancelado", de, ate),
                          "suspensos": conta("data_status", "Suspenso", de, ate), "ativados": conta("data_status", "Ativo", de, ate)}
    return {"base": [{"nome": k, "qtd": v} for k, v in base.most_common()], "total": len(lista),
            "ontem": ev(ontem, ontem), "sete_dias": ev(ini7, ontem)}


def coletar_chatmix_fila():
    import auditoria_chatmix as a
    fila = a.fila_agora(os.environ["CHATMIX_DOMINIO"].rstrip("/"), os.environ["CHATMIX_PUBLIC"])
    return {"aguardando": fila.get("waiting"), "automacao": fila.get("automation"), "andamento": fila.get("progress")}


def resumir_dia(atend):
    """Resumo de um dia de atendimentos encerrados: por atendente (quantidade, tempo, notas) e assuntos. Sem dados de cliente."""
    import auditoria_chatmix as a
    por, assuntos = {}, Counter()
    for x in atend:
        q, m = a.nome_atendente(x), a.minutos(x)
        p = por.setdefault(q, {"qtd": 0, "soma": 0.0, "n": 0, "nsum": 0.0, "nn": 0})
        p["qtd"] += 1
        if m is not None:
            p["soma"] += m
            p["n"] += 1
        for s in x.get("satisfaction_surveys") or []:
            v = s.get("satisfaction")
            if isinstance(v, (int, float)):
                p["nsum"] += v
                p["nn"] += 1
        for cl in x.get("classifications") or []:
            assuntos[cl.get("name") or "Sem nome"] += 1
    return {"total": len(atend), "por": por, "assuntos": assuntos.most_common(6)}


def atualizar_chatmix(cache, agora, hoje):
    """Guarda, dia a dia, os atendimentos encerrados dos últimos 7 dias. Por rodada busca no máximo o dia de hoje
    (a cada 20 min) e UM dia que falta, para respeitar o limite de 2 consultas por minuto do Chatmix."""
    import auditoria_chatmix as a
    base, token = os.environ["CHATMIX_DOMINIO"].rstrip("/"), os.environ["CHATMIX_PUBLIC"]
    dias = cache.setdefault("cm_dias", {})
    for k in [k for k in dias if (hoje - datetime.fromisoformat(k).date()).days > 9]:
        dias.pop(k)
    falha = cache.get("cm_falha")
    if agora.hour < 8 or (falha and agora - datetime.fromisoformat(falha) < timedelta(minutes=30)):
        return  # antes das 8h os relatórios das 7h usam o limite do Chatmix; depois de uma falha espera um pouco

    def buscar(dia):
        dias[dia.isoformat()] = dict(resumir_dia(a.buscar_encerrados(base, token, dia.isoformat())),
                                     em=agora.isoformat(timespec="seconds"))

    try:
        h = dias.get(hoje.isoformat())
        if not h or agora - datetime.fromisoformat(h["em"]) >= timedelta(minutes=20):
            buscar(hoje)
        for i in range(1, 7):  # ontem, anteontem... só 1 por rodada
            dia = hoje - timedelta(days=i)
            item = dias.get(dia.isoformat())
            if not item or item["em"][:10] == dia.isoformat():  # não tem, ou foi pego antes do fim do dia
                buscar(dia)
                break
        cache.pop("cm_falha", None)
    except (Exception, SystemExit) as e:
        cache["cm_falha"] = agora.isoformat(timespec="seconds")
        print(f"Chatmix (atendimentos por dia) falhou: {type(e).__name__}", file=sys.stderr)


def montar_chatmix(cache, hoje):
    dias = cache.get("cm_dias") or {}
    serie, tem = [], 0
    nomes = defaultdict(lambda: {"hoje": 0, "qtd7": 0, "soma": 0.0, "n": 0, "nsum": 0.0, "nn": 0})
    assuntos = Counter()
    for i in range(6, -1, -1):
        dia = hoje - timedelta(days=i)
        item = dias.get(dia.isoformat())
        serie.append({"rotulo": c.DIAS[dia.weekday()][:3].capitalize() + dia.strftime(" %d/%m"),
                      "qtd": item["total"] if item else 0, "tem": bool(item)})
        if not item:
            continue
        tem += 1
        for q, p in item["por"].items():
            x = nomes[q]
            x["qtd7"] += p["qtd"]
            x["soma"] += p["soma"]
            x["n"] += p["n"]
            x["nsum"] += p["nsum"]
            x["nn"] += p["nn"]
            if dia == hoje:
                x["hoje"] += p["qtd"]
        for nome, qtd in item.get("assuntos", []):
            assuntos[nome] += qtd
    if not tem:
        return None
    h = dias.get(hoje.isoformat())
    atend = [{"nome": q, "hoje": x["hoje"], "qtd7": x["qtd7"], "media": x["soma"] / x["n"] if x["n"] else None,
              "nota": x["nsum"] / x["nn"] if x["nn"] else None, "n_notas": x["nn"]} for q, x in nomes.items()]
    atend.sort(key=lambda x: (x["nome"].startswith("Sem atendente"), -x["qtd7"], x["nome"]))
    return {"serie": serie, "dias_com_dados": tem, "atendentes": atend, "assuntos": assuntos.most_common(6),
            "hoje_total": h["total"] if h else None, "hoje_em": h["em"][11:16] if h else None}


def coletar_saude(agora):
    import sentinela as s
    problemas = s.checar_servico() + s.checar_disco() + s.checar_memoria() + s.checar_sgp()
    pasta = os.environ.get("LOG_DIR", "/var/log")
    rel = []
    for arq, nome, (h, m) in s.RELATORIOS:
        previsto = agora.replace(hour=h, minute=m, second=0, microsecond=0)
        status, quando = "ok", None
        try:
            mod = datetime.fromtimestamp(os.path.getmtime(os.path.join(pasta, arq)), agora.tzinfo)
            quando = mod.strftime("%d/%m %H:%M")
            if agora >= previsto + timedelta(minutes=40) and mod < previsto - timedelta(minutes=5):
                status = "nao_rodou"
            else:
                with open(os.path.join(pasta, arq), encoding="utf-8", errors="ignore") as f:
                    final = "\n".join(l for l in f.read()[-3000:].splitlines() if "duplicado" not in l.lower())
                    if s.PADRAO_ERRO.search(final):  # recusa "duplicado" é temporária: o relatório tenta de novo
                        status = "erro"
        except OSError:
            status = "sem_log"
        rel.append({"nome": nome, "hora": "%02d:%02d" % (h, m), "ultimo": quando, "status": status})
    mem = None
    try:
        mi = dict(l.split(":", 1) for l in open("/proc/meminfo").read().splitlines())
        mem = round(100 * (1 - int(mi["MemAvailable"].split()[0]) / int(mi["MemTotal"].split()[0])))
    except (OSError, KeyError, ValueError):
        pass
    import shutil
    try:
        u = shutil.disk_usage("/")
        disco = round(100 * u.used / u.total)
    except OSError:
        disco = None
    return {"problemas": [t for _, t in problemas], "relatorios": rel, "memoria_pct": mem, "disco_pct": disco}


def coletar_tudo():
    agora, hoje = c.agora(), c.hoje()
    cache = c.carregar_estado(CACHE)
    d = {"gerado_em": agora.strftime("%d/%m/%Y %H:%M"), "hoje": hoje.isoformat(),
         "dia_semana": c.DIAS[hoje.weekday()], "secoes_com_erro": []}

    def secao(nome, fn):
        try:
            d[nome] = fn()
        except (Exception, SystemExit) as e:
            d[nome] = None
            d["secoes_com_erro"].append(nome)
            print(f"Seção {nome} falhou: {type(e).__name__}", file=sys.stderr)

    secao("contas", lambda: coletar_contas(hoje))
    secao("os", lambda: coletar_os(hoje))
    secao("inad", lambda: com_cache(cache, "inad", 60, lambda: coletar_inad(hoje)))
    secao("contratos", lambda: com_cache(cache, "contratos", 120, lambda: coletar_contratos(hoje)))
    if os.environ.get("PAINEL_CHATMIX") != "0" and os.environ.get("CHATMIX_DOMINIO") and os.environ.get("CHATMIX_PUBLIC"):
        secao("fila", lambda: com_cache(cache, "fila", 8, coletar_chatmix_fila))
        atualizar_chatmix(cache, agora, hoje)
        d["chatmix"] = montar_chatmix(cache, hoje)
    secao("saude", lambda: coletar_saude(agora))
    try:
        c.salvar_estado(CACHE, cache)
    except OSError as e:
        print(f"Não consegui salvar o cache: {type(e).__name__}", file=sys.stderr)
    return d


# ============================================================================ página
CSS = """
:root{--bg:#eef2f6;--card:#fff;--fg:#14212b;--muted:#5a6b79;--line:#dde4ea;--accent:#2e5f9e;--accent2:#e3edf8;
--good:#17794a;--warn:#a85f00;--crit:#b3261e;--goodbg:#e2f4ea;--warnbg:#fbeed6;--critbg:#fbe3e0;--infobg:#e3edf8;--bar:#2e5f9e;--barbg:#e6ecf2}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#0d1319;--card:#151f28;--fg:#e5edf2;--muted:#8fa2b0;
--line:#243442;--accent:#6aa3e6;--accent2:#172a40;--good:#4cc38a;--warn:#e3a840;--crit:#ff8a80;--goodbg:#10301f;--warnbg:#3a2a0b;--critbg:#3d1714;
--infobg:#172a40;--bar:#5b9be0;--barbg:#20303d;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#0d1319;--card:#151f28;--fg:#e5edf2;--muted:#8fa2b0;--line:#243442;--accent:#6aa3e6;--accent2:#172a40;
--good:#4cc38a;--warn:#e3a840;--crit:#ff8a80;--goodbg:#10301f;--warnbg:#3a2a0b;--critbg:#3d1714;--infobg:#172a40;--bar:#5b9be0;--barbg:#20303d;color-scheme:dark}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
padding-block:16px 48px;padding-left:max(16px,env(safe-area-inset-left,0px));padding-right:max(16px,env(safe-area-inset-right,0px))}
.wrap{max-width:1240px;margin:0 auto}
.top{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:12px 24px;margin-bottom:12px}
.brand{display:flex;align-items:center;gap:16px;min-width:0}
.logo{background:#fff;border-radius:10px;padding:6px 12px;display:flex;border:1px solid var(--line)}
.logo img{height:46px;width:auto;display:block}
h1{font-size:21px;margin:0;letter-spacing:-.01em;text-wrap:balance}
.sub{color:var(--muted);font-size:13px;margin-top:2px}
.pill{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:5px 12px;font-size:13px;font-weight:600;white-space:nowrap}
.pill i{width:8px;height:8px;border-radius:50%;background:currentColor;display:inline-block}
.ok{background:var(--goodbg);color:var(--good)}.bad{background:var(--critbg);color:var(--crit)}.att{background:var(--warnbg);color:var(--warn)}.inf{background:var(--infobg);color:var(--accent)}
nav{position:sticky;top:env(safe-area-inset-top,0px);z-index:5;background:var(--bg);display:flex;flex-wrap:wrap;gap:6px;padding:8px 0 10px;margin-bottom:6px;border-bottom:1px solid var(--line)}
nav a{color:var(--fg);text-decoration:none;font-size:13px;font-weight:600;padding:6px 12px;border-radius:999px;background:var(--card);border:1px solid var(--line)}
nav a:hover,nav a:focus-visible{border-color:var(--accent);color:var(--accent);outline:none}
.grupo{font-size:13px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);font-weight:700;margin:26px 0 10px;display:flex;align-items:center;gap:12px;scroll-margin-top:64px}
.grupo::after{content:"";flex:1;height:1px;background:var(--line)}
.atencao{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin:12px 0}
.atencao h2{margin:0 0 6px;font-size:14px}
.at{display:flex;gap:10px;align-items:baseline;padding:4px 0;font-size:14px;color:var(--fg);text-decoration:none}
.at i{width:9px;height:9px;border-radius:50%;flex:none;transform:translateY(0)}
.at-bad i{background:var(--crit)}.at-att i{background:var(--warn)}.at-inf i{background:var(--accent)}.at-ok i{background:var(--good)}
.at:hover span{text-decoration:underline}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;min-width:0;color:var(--fg);text-decoration:none;display:block}
.kpi:hover,.kpi:focus-visible{border-color:var(--accent);outline:none}
.kpi span{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;display:block}
.kpi b{display:block;font-size:24px;letter-spacing:-.02em;font-variant-numeric:tabular-nums;line-height:1.25;margin:2px 0}
.kpi small{font-size:12px;color:var(--muted);display:block}
.kpi.k-crit b{color:var(--crit)}.kpi.k-warn b{color:var(--warn)}.kpi.k-good b{color:var(--good)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;min-width:0}
.card.wide{grid-column:1/-1}
.card h2{font-size:15px;margin:0 0 2px}.card .dica{color:var(--muted);font-size:13px;margin:0 0 12px}
.duas{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,380px),1fr));gap:4px 28px}
h3{font-size:12px;margin:16px 0 6px;color:var(--muted);font-weight:700;text-transform:uppercase;letter-spacing:.06em}
.big{font-size:28px;font-weight:700;font-variant-numeric:tabular-nums;letter-spacing:-.02em;line-height:1.2}
.stats{display:flex;flex-wrap:wrap;gap:8px 28px;margin:6px 0 4px}
.stats div{min-width:0}.stats b{display:block;font-size:26px;font-variant-numeric:tabular-nums;letter-spacing:-.02em;line-height:1.2}
.stats span{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.row{display:flex;justify-content:space-between;gap:12px;padding:6px 0;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums;font-size:14px}
.row:last-child{border-bottom:0}.row span:first-child{min-width:0;overflow-wrap:anywhere}.row span:nth-child(2){white-space:nowrap;color:var(--muted)}
.bar{display:grid;grid-template-columns:minmax(96px,150px) 1fr auto;gap:10px;align-items:center;padding:3px 0;font-size:13px;font-variant-numeric:tabular-nums}
.bar .t{background:var(--barbg);border-radius:3px;height:12px;overflow:hidden}.bar .t i{display:block;height:100%;background:var(--bar);border-radius:3px}
.bar .v{color:var(--muted);white-space:nowrap}
.cols{display:flex;align-items:flex-end;gap:6px;height:112px;margin:6px 0 4px}
.cols div{flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;height:100%;min-width:0}
.cols i{display:block;width:100%;background:var(--bar);border-radius:3px 3px 0 0;min-height:2px}
.cols i.vazio{background:var(--barbg)}
.cols em{font-style:normal;font-size:11px;color:var(--muted);margin-bottom:3px;font-variant-numeric:tabular-nums}
.lab{display:flex;gap:6px}.lab span{flex:1;text-align:center;font-size:11px;color:var(--muted);min-width:0}
.tw{overflow-x:auto}table{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
th{font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--muted);text-align:left;font-weight:600;padding:4px 12px 6px 0}
td{padding:7px 12px 7px 0;border-top:1px solid var(--line);vertical-align:top}td.n,th.n{text-align:right}th:last-child,td:last-child{padding-right:0}
tr.mut td{color:var(--muted)}
.err{color:var(--crit);font-size:13px;margin:6px 0}.mut{color:var(--muted);font-size:13px}
footer{margin-top:28px;color:var(--muted);font-size:12px;text-align:center}
@media (max-width:480px){.bar{grid-template-columns:96px 1fr auto}.logo img{height:38px}}
"""

try:
    import painel_logo
    LOGO = ('<span class="logo"><img src="data:image/png;base64,%s" width="%d" height="%d" alt="Ferreira Telecom"></span>'
            % (painel_logo.LOGO_PNG_BASE64, painel_logo.LOGO_LARGURA, painel_logo.LOGO_ALTURA))
except Exception:  # sem o arquivo da logo o painel funciona do mesmo jeito
    LOGO = ""


def brl(v):
    return c.brl(v or 0)


def barras(itens, chave_val="qtd", fmt=None):
    mx = max([i.get(chave_val, 0) or 0 for i in itens] + [1])
    out = []
    for i in itens:
        v = i.get(chave_val, 0) or 0
        pct = max(2, round(100 * v / mx)) if v else 0
        out.append('<div class="bar"><span>%s</span><div class="t"><i style="width:%d%%"></i></div><span class="v">%s</span></div>'
                   % (esc(str(i["nome"])), pct, esc(fmt(i) if fmt else str(v))))
    return "".join(out)


def colunas(itens, chave, rotulo_val):
    mx = max([i[chave] for i in itens] + [1])
    cols = "".join('<div><em>%s</em><i class="%s" style="height:%d%%"></i></div>'
                   % (esc(rotulo_val(i)), "" if i.get("tem", True) else "vazio", round(100 * i[chave] / mx) if i[chave] else 0)
                   for i in itens)
    labs = "".join("<span>%s</span>" % esc(i["rotulo"].split(" ")[0]) for i in itens)
    return '<div class="cols">%s</div><div class="lab">%s</div>' % (cols, labs)


def linhas(lst):
    return "".join('<div class="row"><span>%s</span></div>' % esc(x) for x in lst) or '<p class="mut">Nada por aqui.</p>'


def cartao(titulo, dica, corpo, dado, larga=False, ident=""):
    if dado is None:
        corpo = '<p class="err">Não consegui carregar esta parte agora. Tento de novo na próxima atualização.</p>'
    return '<section class="card%s"%s><h2>%s</h2><p class="dica">%s</p>%s</section>' % (
        " wide" if larga else "", (' id="%s"' % ident) if ident else "", esc(titulo), esc(dica), corpo)


def minutos_txt(m):
    return "-" if m is None else ("%d min" % round(m) if m < 60 else "%dh%02d" % (m // 60, round(m) % 60))


def atencoes(d):
    L = []
    ct, osd, fila, sd, ctr = d.get("contas"), d.get("os"), d.get("fila"), d.get("saude"), d.get("contratos")
    if sd:
        for r in sd["relatorios"]:
            if r["status"] in ("erro", "nao_rodou"):
                L.append(("bad", "Relatório %s: %s" % (r["nome"], "com erro no log" if r["status"] == "erro" else "não rodou hoje"), "#sistema"))
        for p in sd["problemas"]:
            L.append(("bad", p, "#sistema"))
    if osd:
        if osd["sem_tecnico"]:
            L.append(("att", "%d OS externas abertas sem técnico" % osd["sem_tecnico"], "#operacao"))
        velhas = sum(f["qtd"] for f in osd["faixas"] if f["nome"].startswith("Mais de 30"))
        if velhas:
            L.append(("att", "%d OS externas abertas há mais de 30 dias" % velhas, "#operacao"))
    if ct:
        if ct["dias"][0]["qtd"]:
            L.append(("inf", "%d contas a pagar hoje, total %s" % (ct["dias"][0]["qtd"], brl(ct["dias"][0]["total"])), "#financeiro"))
        if ct["atrasadas_qtd"]:
            L.append(("att", "%d contas a pagar atrasadas, total %s" % (ct["atrasadas_qtd"], brl(ct["atrasadas_total"])), "#financeiro"))
    if fila and (fila.get("aguardando") or 0) >= 10:
        L.append(("att", "%s clientes aguardando atendente no Chatmix" % fila["aguardando"], "#atendimento"))
    if not L:
        L.append(("ok", "Nada precisa de atenção agora.", "#resumo"))
    return L


def render(d):
    ct, osd, inad, ctr, fila, cm, sd = (d.get(k) for k in ("contas", "os", "inad", "contratos", "fila", "chatmix", "saude"))
    if sd is None:
        pilula = '<span class="pill att"><i></i>Saúde dos agentes: sem dados</span>'
    elif sd["problemas"] or any(r["status"] in ("erro", "nao_rodou", "sem_log") for r in sd["relatorios"]):
        pilula = '<span class="pill bad"><i></i>Há problemas nos agentes</span>'
    else:
        pilula = '<span class="pill ok"><i></i>Tudo funcionando</span>'

    # ---------------------------------------------------------------- resumo
    aten = "".join('<a class="at at-%s" href="%s"><i></i><span>%s</span></a>' % (n, esc(l), esc(t)) for n, t, l in atencoes(d))
    kp = []

    def kpi(rot, val, dica, link, cls=""):
        kp.append('<a class="kpi %s" href="%s"><span>%s</span><b>%s</b><small>%s</small></a>' % (cls, link, esc(rot), esc(str(val)), esc(dica)))
    if ct:
        hj = ct["dias"][0]
        kpi("Contas a pagar hoje", brl(hj["total"]) if hj["qtd"] else "Nenhuma", "Próximos 7 dias: " + brl(ct["semana_total"]), "#financeiro")
        kpi("Contas atrasadas", brl(ct["atrasadas_total"]), "%d contas" % ct["atrasadas_qtd"], "#financeiro", "k-warn" if ct["atrasadas_qtd"] else "k-good")
    if inad:
        kpi("Faturas de clientes vencidas", brl(inad["total"]), "%d faturas · %d clientes" % (inad["qtd"], inad["clientes"]), "#financeiro", "k-crit")
    if osd:
        kpi("OS externas abertas", osd["total"], "%d sem técnico" % osd["sem_tecnico"], "#operacao", "k-warn" if osd["sem_tecnico"] else "")
        kpi("OS fechadas hoje", osd["fechadas_hoje"], "Agendadas para hoje: %d" % osd["agendadas_hoje"], "#operacao")
    if fila and fila.get("aguardando") is not None:
        kpi("Clientes na fila do Chatmix", fila["aguardando"], "Em andamento: %s" % fila.get("andamento", "?"), "#atendimento",
            "k-warn" if (fila["aguardando"] or 0) >= 10 else "")
    if ctr:
        ativos = next((b["qtd"] for b in ctr["base"] if b["nome"] == "Ativo"), 0)
        kpi("Contratos ativos", ativos, "Ontem: %d novos, %d cancelados" % (ctr["ontem"]["novos"], ctr["ontem"]["cancelados"]), "#operacao")

    # ---------------------------------------------------------------- financeiro
    corpo = ""
    if ct:
        corpo = ('<div class="big">%s</div><div class="mut">a pagar nos próximos 7 dias</div>' % esc(brl(ct["semana_total"]))
                 + colunas(ct["dias"], "total", lambda i: ("%.0f" % i["total"]) if i["total"] else "")
                 + "<h3>Vencem hoje</h3>" + linhas(ct["hoje"])
                 + "<h3>Atrasadas: %s em %d %s</h3>" % (esc(brl(ct["atrasadas_total"])), ct["atrasadas_qtd"], "conta" if ct["atrasadas_qtd"] == 1 else "contas")
                 + linhas(ct["atrasadas_top"]))
    c_contas = cartao("Contas a pagar", "O que a empresa precisa pagar (fornecedores e despesas).", corpo, ct)
    corpo = ""
    if inad:
        corpo = ('<div class="big">%s</div><div class="mut">vencido em %d faturas de %d clientes</div><h3>Por tempo de atraso</h3>%s'
                 '<h3>Por ciclo de vencimento</h3>%s<h3>Vencem em breve</h3>'
                 '<div class="row"><span>Hoje</span><span>%s (%d)</span></div><div class="row"><span>Amanhã</span><span>%s (%d)</span></div>'
                 % (esc(brl(inad["total"])), inad["qtd"], inad["clientes"],
                    barras(inad["faixas"], "total", lambda i: "%s (%d)" % (brl(i["total"]), i["qtd"])),
                    barras(inad["ciclos"], "total", lambda i: "%s (%d)" % (brl(i["total"]), i["qtd"])),
                    esc(brl(inad["vencem"]["hoje"]["total"])), inad["vencem"]["hoje"]["qtd"],
                    esc(brl(inad["vencem"]["amanha"]["total"])), inad["vencem"]["amanha"]["qtd"]))
    c_inad = cartao("Inadimplência", "Faturas de clientes que passaram do vencimento e seguem em aberto.", corpo, inad)

    # ---------------------------------------------------------------- operação
    corpo = ""
    if osd:
        status = " · ".join("%s %d" % (k2, v) for k2, v in sorted(osd["por_status"].items()))
        trs = "".join('<tr><td>%s</td><td class="n">%d</td><td class="n">%d</td><td class="n">%d</td><td class="n">%d</td></tr>'
                      % (esc(t["nome"]), t["agendadas"], t["execucao"], t["fechadas_hoje"], t["fechadas_7d"]) for t in osd["tecnicos"])
        ant = "".join('<tr><td>%s</td><td>%s</td><td>%s</td><td class="n">%d d</td></tr>'
                      % (esc(str(o["id"])), esc(o["motivo"]), esc(o["tecnico"]), o["idade"]) for o in osd["antigas"])
        esq = ('<div class="stats"><div><b>%d</b><span>Abertas</span></div><div><b>%d</b><span>Sem técnico</span></div>'
               '<div><b>%d</b><span>Agendadas hoje</span></div><div><b>%d</b><span>Fechadas hoje</span></div></div>'
               '<div class="mut">Situação: %s. Internas abertas (escritório): %d.</div>'
               % (osd["total"], osd["sem_tecnico"], osd["agendadas_hoje"], osd["fechadas_hoje"], esc(status or "nenhuma"), osd["internas"])
               + "<h3>Há quanto tempo estão abertas</h3>" + barras(osd["faixas"])
               + "<h3>Fechadas por dia (externas)</h3>" + colunas(osd["serie"], "qtd", lambda i: str(i["qtd"]) if i["qtd"] else ""))
        dir_ = ('<h3>Técnicos</h3><div class="tw"><table><tr><th>Técnico</th><th class="n">Agendadas hoje</th><th class="n">Em execução</th>'
                '<th class="n">Fechou hoje</th><th class="n">Fechou em 7 dias</th></tr>%s</table></div>'
                '<h3>As OS mais antigas em aberto</h3><div class="tw"><table><tr><th>OS</th><th>Motivo</th><th>Técnico</th><th class="n">Aberta há</th></tr>%s</table></div>'
                % (trs or '<tr><td colspan="5">Sem dados.</td></tr>', ant or '<tr><td colspan="4">Nenhuma.</td></tr>'))
        corpo = '<div class="duas"><div>%s</div><div>%s</div></div>' % (esq, dir_)
    c_os = cartao("Ordens de serviço e técnicos", "Serviços de campo (OS externas) em aberto e o trabalho de cada técnico.", corpo, osd, True)
    corpo = ""
    if ctr:
        def ev(titulo, e):
            return ('<div class="row"><span><b>%s:</b> novos %d · ativados %d · suspensos %d · cancelados %d</span></div>'
                    % (esc(titulo), e["novos"], e["ativados"], e["suspensos"], e["cancelados"]))
        corpo = ('<div class="duas"><div><div class="big">%d</div><div class="mut">contratos no total</div><h3>Base por situação</h3>%s</div>'
                 '<div><h3>Movimento da base</h3>%s%s</div></div>'
                 % (ctr["total"], barras(ctr["base"]), ev("Ontem", ctr["ontem"]), ev("Últimos 7 dias", ctr["sete_dias"])))
    c_ctr = cartao("Contratos", "Quantos clientes ativos e como a base se movimentou.", corpo, ctr, True)

    # ---------------------------------------------------------------- atendimento
    corpo, dado = "", (fila or cm)
    if fila:
        corpo += ('<div class="stats"><div><b>%s</b><span>Aguardando atendente</span></div><div><b>%s</b><span>Em andamento</span></div>'
                  '<div><b>%s</b><span>Em automação</span></div>%s</div>'
                  % (esc(str(fila.get("aguardando", "?"))), esc(str(fila.get("andamento", "?"))), esc(str(fila.get("automacao", "?"))),
                     ('<div><b>%d</b><span>Encerrados hoje (às %s)</span></div>' % (cm["hoje_total"], esc(cm["hoje_em"]))) if cm and cm["hoje_total"] is not None else ""))
    if cm:
        trs = "".join('<tr class="%s"><td>%s</td><td class="n">%d</td><td class="n">%d</td><td class="n">%s</td><td class="n">%s</td></tr>'
                      % ("mut" if a["nome"].startswith("Sem atendente") else "", esc(a["nome"]), a["hoje"], a["qtd7"], minutos_txt(a["media"]),
                         ("%.1f (%d)" % (a["nota"], a["n_notas"])) if a["nota"] is not None else "-") for a in cm["atendentes"])
        esq = ('<h3>Atendimentos encerrados por atendente</h3><div class="tw"><table><tr><th>Atendente</th><th class="n">Hoje</th><th class="n">7 dias</th>'
               '<th class="n">Tempo médio</th><th class="n">Nota (respostas)</th></tr>%s</table></div>' % trs)
        dir_ = "<h3>Encerrados por dia</h3>" + colunas(cm["serie"], "qtd", lambda i: str(i["qtd"]) if i["tem"] else "")
        if cm["assuntos"]:
            dir_ += "<h3>Assuntos mais comuns (7 dias)</h3>" + barras([{"nome": n, "qtd": q} for n, q in cm["assuntos"]])
        corpo += '<div class="duas"><div>%s</div><div>%s</div></div>' % (esq, dir_)
        if cm["dias_com_dados"] < 7:
            corpo += '<p class="mut">Histórico em formação: %d de 7 dias carregados (1 dia novo por atualização, por causa do limite do Chatmix).</p>' % cm["dias_com_dados"]
    elif fila:
        corpo += '<p class="mut">Os atendimentos por atendente aparecem aqui assim que o primeiro dia for carregado (a partir das 8h).</p>'
    c_at = cartao("Atendimento (Chatmix)", "Fila agora e o que cada atendente encerrou.", corpo, dado, True) if (fila or cm) else ""

    # ---------------------------------------------------------------- sistema
    corpo = ""
    if sd:
        rot = {"ok": ("ok", "Rodou"), "erro": ("bad", "Erro no log"), "nao_rodou": ("bad", "Não rodou hoje"), "sem_log": ("att", "Sem log")}
        trs = "".join('<tr><td>%s</td><td>%s</td><td>%s</td><td><span class="pill %s">%s</span></td></tr>'
                      % (esc(r["nome"]), r["hora"], esc(r["ultimo"] or "-"), rot[r["status"]][0], rot[r["status"]][1]) for r in sd["relatorios"])
        corpo = '<div class="tw"><table><tr><th>Relatório</th><th>Horário</th><th>Último registro</th><th>Situação</th></tr>%s</table></div>' % trs
        corpo += ('<h3>Servidor</h3><div class="row"><span>Memória em uso</span><span>%s</span></div><div class="row"><span>Disco em uso</span><span>%s</span></div>'
                  % (("%d%%" % sd["memoria_pct"]) if sd["memoria_pct"] is not None else "-", ("%d%%" % sd["disco_pct"]) if sd["disco_pct"] is not None else "-"))
        corpo += ("<h3>Alertas</h3>" + "".join('<p class="err">%s</p>' % esc(p) for p in sd["problemas"])) if sd["problemas"] \
            else '<p class="mut">Bot do Telegram ativo e SGP respondendo.</p>'
    c_saude = cartao("Saúde dos agentes", "Se os relatórios das 7h rodaram e se o servidor está bem.", corpo, sd)

    erros = d["secoes_com_erro"]
    aviso = ('<p class="err">Algumas partes não carregaram na última atualização: %s.</p>' % esc(", ".join(erros))) if erros else ""
    data_br = "%s/%s/%s" % (d["hoje"][8:10], d["hoje"][5:7], d["hoje"][:4])
    return ('<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="refresh" content="300"><meta name="robots" content="noindex,nofollow"><title>Painel Ferreira Telecom</title>'
            '<style>%s</style></head><body><div class="wrap">'
            '<header class="top"><div class="brand">%s<div><h1>Painel de operação</h1><div class="sub">%s, %s · atualizado às %s · a página se atualiza sozinha</div></div></div>%s</header>'
            '<nav><a href="#resumo">Resumo</a><a href="#financeiro">Financeiro</a><a href="#operacao">Operação</a><a href="#atendimento">Atendimento</a><a href="#sistema">Sistema</a></nav>'
            '<div id="resumo" class="grupo">Resumo</div>%s<div class="atencao"><h2>O que precisa de atenção</h2>%s</div><div class="kpis">%s</div>'
            '<div id="financeiro" class="grupo">Financeiro</div><div class="grid">%s%s</div>'
            '<div id="operacao" class="grupo">Operação</div><div class="grid">%s%s</div>'
            '<div id="atendimento" class="grupo">Atendimento</div><div class="grid">%s</div>'
            '<div id="sistema" class="grupo">Sistema</div><div class="grid">%s</div>'
            '<footer>Só números e totais: esta página não mostra nome, CPF, telefone ou endereço de cliente.</footer></div></body></html>'
            % (CSS, LOGO, esc(d["dia_semana"].capitalize()), esc(data_br), esc(d["gerado_em"][-5:]), pilula,
               aviso, aten, "".join(kp), c_contas, c_inad, c_os, c_ctr, c_at, c_saude))


def gerar(saida=None):
    import fcntl
    os.makedirs(PASTA, exist_ok=True)
    trava = open(os.path.join(PASTA, "gerar.lock"), "w")
    try:
        fcntl.flock(trava, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("Outra atualização do painel ainda está rodando; esta foi pulada.")
        return
    d = coletar_tudo()
    pagina = render(d)
    if saida is None:
        os.makedirs(PASTA, exist_ok=True)
        saida = os.path.join(PASTA, ARQ_PAGINA)
    tmp = saida + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(pagina)
    os.replace(tmp, saida)
    print(f"Painel atualizado ({len(pagina) // 1024} KB, {len(d['secoes_com_erro'])} parte(s) com erro).")


# ============================================================================ servidor
FALHAS, TRAVA = {}, threading.Lock()
MAX_FALHAS, BLOQUEIO_SEG = 5, 15 * 60


def servir():
    senha, usuario = os.environ.get("PAINEL_SENHA", ""), os.environ.get("PAINEL_USUARIO", "dono")
    if len(senha) < 10:
        sys.exit("Defina PAINEL_SENHA (mínimo 10 caracteres) em /etc/relatorio-sgp.env antes de iniciar o painel.")
    cert, chave = os.environ.get("PAINEL_CERT"), os.environ.get("PAINEL_KEY")
    https = bool(cert and chave)
    porta = int(os.environ.get("PAINEL_PORTA", "8443" if https else "8080"))
    esperado = (usuario + ":" + senha).encode("utf-8")

    class H(BaseHTTPRequestHandler):
        server_version = "painel"
        sys_version = ""
        timeout = 20  # conexão parada/lenta é derrubada em 20s (não trava o painel)

        def log_message(self, fmt, *args):  # nunca registra cabeçalhos nem senha
            print(self.client_address[0], str(args[0]) if args else "", file=sys.stderr)

        def _resp(self, codigo, corpo=b"", tipo="text/plain; charset=utf-8", extra=()):
            self.send_response(codigo)
            self.send_header("Content-Type", tipo)
            self.send_header("Content-Length", str(len(corpo)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'")
            if https:
                self.send_header("Strict-Transport-Security", "max-age=31536000")
            for k2, v in extra:
                self.send_header(k2, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(corpo)

        def _autorizado(self):
            ip, agora = self.client_address[0], time.time()
            with TRAVA:
                n, ate = FALHAS.get(ip, (0, 0))
                if ate > agora:
                    return None  # bloqueado
            cab = self.headers.get("Authorization", "")
            ok = False
            if cab.startswith("Basic "):
                try:
                    ok = hmac.compare_digest(base64.b64decode(cab[6:], validate=True), esperado)
                except (ValueError, TypeError):
                    ok = False
            with TRAVA:
                if ok:
                    FALHAS.pop(ip, None)
                elif cab:  # só conta quando tentou uma senha
                    n = n + 1
                    FALHAS[ip] = (0, agora + BLOQUEIO_SEG) if n >= MAX_FALHAS else (n, 0)
            return ok

        def do_GET(self):
            r = self._autorizado()
            if r is None:
                return self._resp(429, "Muitas tentativas. Tente mais tarde.".encode())
            if not r:
                return self._resp(401, "Acesso restrito.".encode(), extra=[("WWW-Authenticate", 'Basic realm="Painel Ferreira Telecom", charset="UTF-8"')])
            if self.path.split("?")[0] not in ("/", "/index.html"):
                return self._resp(404, b"Nao encontrado.")
            try:
                with open(os.path.join(PASTA, ARQ_PAGINA), "rb") as f:
                    corpo = f.read()
            except OSError:
                return self._resp(503, "O painel ainda não foi gerado. Aguarde alguns minutos.".encode())
            self._resp(200, corpo, "text/html; charset=utf-8")

        do_HEAD = do_GET

        def do_POST(self):
            self._resp(405, b"Metodo nao permitido.")

    class Servidor(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):  # varreduras/handshake falho: sem traceback no log
            pass

    srv = Servidor((os.environ.get("PAINEL_BIND", "0.0.0.0"), porta), H)
    if https:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(cert, chave)
        # handshake na thread da conexão (não no accept): um cliente parado não trava o servidor
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True, do_handshake_on_connect=False)
    print(f"Painel no ar na porta {porta} ({'HTTPS' if https else 'HTTP, sem cadeado'}).", file=sys.stderr)
    srv.serve_forever()


if __name__ == "__main__":
    if "--servir" in sys.argv:
        servir()
    elif "--gerar" in sys.argv:
        saida = sys.argv[sys.argv.index("--saida") + 1] if "--saida" in sys.argv else None
        gerar(saida)
    else:
        sys.exit("Use --gerar (atualiza a página) ou --servir (coloca no ar).")
