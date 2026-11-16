#!/usr/bin/env python3
"""
Bot do Telegram da Ferreira Telecom: você manda um comando (ou o nome do agente) e ele responde com o relatório.
Usa só a biblioteca padrão do Python 3.9+. Responde SOMENTE ao seu usuário do Telegram.

Variáveis de ambiente (ficam em /etc/relatorio-sgp.env, junto com as outras):
    TELEGRAM_TOKEN     token que o @BotFather entregou ao criar o bot
    TELEGRAM_USER_ID   número do seu usuário no Telegram (se estiver vazio, o bot só responde qual é o seu número)
    (mais as variáveis do SGP e do Chatmix, que os relatórios usam)
Opcionais: DIR_APP (padrão /opt/relatorio-sgp), BOT_SAIDA_DIR (padrão RELATORIO_DIR/bot), TELEGRAM_API_URL (testes)

Comandos (com ou sem a barra):  caixa | auditor | operacao | gestao | despacho | cobranca | sentinela | ajuda
    perguntas livres: "quanto recebemos esta semana?", "quantas OS abertas?", "quantos contratos?"
    caixa dia 20 | caixa amanhã | caixa 15/12 | caixa sexta | caixa 2026-11-20   contas a pagar de outro dia
    caixa semana          resumo dos próximos 7 dias
    gestao semanal        resumo dos últimos 7 dias
Áudio: mande um áudio curto dizendo o agente; a transcrição roda na própria VM (faster-whisper).
Os relatórios são gerados no modo "sem envio": o texto volta aqui no Telegram e nada vai para o WhatsApp.
"""
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import unicodedata
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Fortaleza")
API = os.environ.get("TELEGRAM_API_URL", "https://api.telegram.org").rstrip("/")
TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
DONO = os.environ.get("TELEGRAM_USER_ID", "").strip()
DIR_APP = os.environ.get("DIR_APP", "/opt/relatorio-sgp")
PY = sys.executable or "/usr/bin/python3"
LIMITE_TELEGRAM = 3800  # o Telegram aceita 4096 caracteres por mensagem

# nome -> (script, descrição, tempo máximo em segundos, aceita data AAAA-MM-DD, opção extra aceita)
AGENTES = {
    "caixa":    ("contas_a_pagar_dia.py", "contas a pagar de hoje e atrasadas", 180, True, "semana"),
    "auditor":  ("auditoria_chatmix.py", "atendimentos do Chatmix de ontem", 900, True, None),
    "operacao": ("painel_operacao.py", "OS, técnicos, agenda e contratos", 300, True, None),
    "gestao":   ("resumo_gestao.py", "resumo de gestão do dia", 900, False, "semanal"),
    "despacho": ("despacho.py", "fila de OS por prioridade e sugestão de técnico", 300, False, None),
    "cobranca": ("cobranca.py", "inadimplência por atraso e por ciclo (só totais)", 300, False, None),
    "sentinela": ("sentinela.py", "saúde da VM, do bot e dos relatórios", 120, False, None),
    "consulta": ("consultas.py", "perguntas rápidas (recebido, OS abertas/fechadas, contratos)", 300, False, None),
}
APELIDOS = {
    "caixa": "caixa", "contas": "caixa", "pagar": "caixa", "pagamentos": "caixa",
    "auditor": "auditor", "auditoria": "auditor", "atendimento": "auditor", "atendimentos": "auditor",
    "operacao": "operacao", "operação": "operacao", "painel": "operacao", "os": "operacao",
    "gestao": "gestao", "gestão": "gestao", "resumo": "gestao",
    "despacho": "despacho", "prioridade": "despacho", "prioridades": "despacho", "fila": "despacho",
    "cobranca": "cobranca", "cobrança": "cobranca", "inadimplencia": "cobranca", "inadimplência": "cobranca",
    "sentinela": "sentinela", "saude": "sentinela", "saúde": "sentinela", "sistema": "sentinela",
}
AJUDA_PALAVRAS = {"ajuda", "help", "start", "agentes", "menu"}
ocupado = threading.Lock()  # um relatório por vez (o Chatmix só aceita 2 consultas por minuto)


# ------------------------------------------------------------------------ datas
MESES = {m: i for i, m in enumerate("janeiro fevereiro marco abril maio junho julho agosto setembro outubro "
                                    "novembro dezembro".split(), 1)}
DIAS_SEMANA = {"segunda": 0, "terca": 1, "quarta": 2, "quinta": 3, "sexta": 4, "sabado": 5, "domingo": 6}
_UN = "um dois tres quatro cinco seis sete oito nove".split()
_DE = "dez onze doze treze quatorze quinze dezesseis dezessete dezoito dezenove".split()
NUMEROS_EXTENSO = {**{w: i for i, w in enumerate(_UN, 1)}, "primeiro": 1, **{w: 10 + i for i, w in enumerate(_DE)},
                   "vinte": 20, "trinta": 30}
for _d, _n in (("vinte", 20), ("trinta", 30)):
    for _i, _w in enumerate(_UN, 1):
        NUMEROS_EXTENSO[f"{_d} e {_w}"] = _n + _i
NUMEROS_EXTENSO["trinta e um"] = 31


def sem_acento(t):
    return "".join(c for c in unicodedata.normalize("NFD", t.lower()) if unicodedata.category(c) != "Mn")


def _proxima_ocorrencia(dia, hoje, mes=None):
    """Próxima data (a partir de hoje) com esse dia do mês; com mês dado, o primeiro ano em que a data existe."""
    a, m = hoje.year, hoje.month
    for _ in range(30):
        if mes is None or m == mes:
            try:
                d = date(a, m, dia)
                if d >= hoje:
                    return d
            except ValueError:
                pass
        m += 1
        if m > 12:
            a, m = a + 1, 1
    return None


def extrair_data(texto, hoje=None):
    """Acha uma data no texto: 'hoje', 'amanhã', 'depois de amanhã', 'ontem', 'sexta', 'dia 20', 'dia 20 de dezembro',
    '20/11', '20/11/2026', '2026-11-20'. Devolve date ou None."""
    hoje = hoje or datetime.now(TZ).date()
    t = sem_acento(texto)
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None
    m = re.search(r"\b(\d{1,2})[/.-](\d{1,2})(?:[/.-](\d{2,4}))?\b", t)
    if m:
        d, mes = int(m[1]), int(m[2])
        if m[3]:
            ano = int(m[3]) + (2000 if int(m[3]) < 100 else 0)
            try:
                return date(ano, mes, d)
            except ValueError:
                return None
        return _proxima_ocorrencia(d, hoje, mes) if 1 <= mes <= 12 else None
    if "depois de amanha" in t:
        return hoje + timedelta(days=2)
    if re.search(r"\bamanha\b", t):
        return hoje + timedelta(days=1)
    if re.search(r"\bontem\b", t):
        return hoje - timedelta(days=1)
    if re.search(r"\bhoje\b", t):
        return hoje
    m = re.search(rf"\b(\d{{1,2}})\s+de\s+({'|'.join(MESES)})\b", t)
    if m and 1 <= int(m[1]) <= 31:
        return _proxima_ocorrencia(int(m[1]), hoje, MESES[m[2]])
    ext = "|".join(sorted(NUMEROS_EXTENSO, key=len, reverse=True))
    m = re.search(rf"\bdia\s+(\d{{1,2}}|{ext})\b(?:\s+de\s+({'|'.join(MESES)}))?", t)
    if m:
        d = int(m[1]) if m[1].isdigit() else NUMEROS_EXTENSO[m[1]]
        return _proxima_ocorrencia(d, hoje, MESES[m[2]] if m[2] else None) if 1 <= d <= 31 else None
    for nome, wd in DIAS_SEMANA.items():
        if re.search(rf"\b{nome}\b", t):
            return hoje + timedelta(days=(wd - hoje.weekday()) % 7 or 7)
    return None


def pede_semana(texto):
    t = sem_acento(texto)
    return bool(re.search(r"\b(semana|proximos dias|proximos 7 dias|proximos sete dias)\b", t))


# ------------------------------------------------------------------ perguntas
def extrair_periodo(texto, hoje=None):
    """'hoje', 'ontem', 'esta semana', 'semana passada', 'este mês', 'mês passado', 'últimos 15 dias', datas."""
    hoje = hoje or datetime.now(TZ).date()
    t = sem_acento(texto)
    ini_sem = hoje - timedelta(days=hoje.weekday())
    if re.search(r"semana passada", t):
        return ini_sem - timedelta(days=7), ini_sem - timedelta(days=1)
    if re.search(r"mes passado", t):
        fim = hoje.replace(day=1) - timedelta(days=1)
        return fim.replace(day=1), fim
    m = re.search(r"ultimos?\s+(\d{1,3})\s+dias", t)
    if m:
        return hoje - timedelta(days=int(m[1]) - 1), hoje
    if re.search(r"\bsemana\b", t):
        return ini_sem, hoje
    if re.search(r"\bmes\b", t):
        return hoje.replace(day=1), hoje
    d = extrair_data(texto, hoje)
    return (d, d) if d else (hoje, hoje)


INTENCOES = (  # (padrão no texto sem acento, agente, intenção da consulta)
    (r"\b(os|ordens?)\b.*\b(fechad\w*|finalizad\w*|encerrad\w*|feit\w*)|\b(fechou|fecharam|finalizaram)\b", "consulta", "os_fechadas"),
    (r"\b(os|ordens?)\b.*\b(abert\w*|pendente\w*|aguardando)|\b(abert\w*|pendente\w*)\b.*\b(os|ordens?)\b", "consulta", "os_abertas"),
    (r"\b(receb\w*|arrecad\w*|entrou|entrada)\b", "consulta", "recebido"),
    (r"inadimpl|devendo|calote|em atraso", "cobranca", None),
    (r"\bcontratos?\b|clientes? ativos?|quantos clientes|base de clientes", "consulta", "contratos"),
    (r"quem vai|qual os (primeiro|fazer)|tecnico para|despacho", "despacho", None),
    (r"tudo (certo|ok)|servidor|\bvm\b|esta funcionando", "sentinela", None),
)


def detectar_consulta(texto, hoje=None):
    """Perguntas em linguagem natural ('quanto recebemos esta semana?') -> (agente, argumentos) ou (None, [])."""
    t = sem_acento(texto)
    for padrao, agente, intencao in INTENCOES:
        if re.search(padrao, t):
            if agente != "consulta":
                return agente, []
            ini, fim = extrair_periodo(texto, hoje)
            return agente, [intencao, ini.isoformat(), fim.isoformat()]
    return None, []


# -------------------------------------------------------------------- Telegram
def tg(metodo, **dados):
    req = urllib.request.Request(f"{API}/bot{TOKEN}/{metodo}", data=json.dumps(dados).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=70) as r:
        return json.loads(r.read().decode("utf-8"))


def responder(chat, texto):
    """Envia em pedaços de até 3800 caracteres, quebrando nas linhas."""
    pedaco = ""
    for linha in texto.split("\n"):
        while len(linha) > LIMITE_TELEGRAM:  # linha gigante: corta em pedaços
            if pedaco.strip():
                enviar(chat, pedaco)
                pedaco = ""
            enviar(chat, linha[:LIMITE_TELEGRAM])
            linha = linha[LIMITE_TELEGRAM:]
        if len(pedaco) + len(linha) + 1 > LIMITE_TELEGRAM:
            enviar(chat, pedaco)
            pedaco = ""
        pedaco += linha + "\n"
    if pedaco.strip():
        enviar(chat, pedaco)


def negrito(texto):
    """*texto* (formato do WhatsApp) vira negrito do Telegram; o resto é escapado para não quebrar o HTML."""
    return re.sub(r"\*(\S(?:[^*\n]*?\S)?)\*", r"<b>\1</b>", html.escape(texto, quote=False))


def enviar(chat, texto):
    texto = texto.strip() or "(vazio)"
    try:
        try:
            tg("sendMessage", chat_id=chat, text=negrito(texto), parse_mode="HTML")
        except urllib.error.HTTPError as e:
            if e.code != 400:
                raise
            tg("sendMessage", chat_id=chat, text=texto)  # se o Telegram recusar a formatação, vai como texto puro
    except Exception as e:  # nunca imprime a URL (ela contém o token)
        print(f"Falha ao enviar mensagem: {type(e).__name__}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------- agentes
def texto_ajuda():
    L = ["Agentes da Ferreira Telecom. Mande o nome (texto ou áudio):", ""]
    for nome, (_, desc, _, _, extra) in AGENTES.items():
        if nome != "consulta":
            L.append(f"/{nome} - {desc}" + (f" (ou /{nome} {extra})" if extra else ""))
    L += ["", "O caixa aceita data: /caixa amanhã, /caixa dia 20, /caixa 15/12, /caixa sexta, /caixa semana",
          "", "Também respondo perguntas, por texto ou áudio:",
          "- quanto recebemos esta semana? / hoje / mês passado",
          "- quantas OS abertas? / quantas OS fechadas ontem?",
          "- quantos contratos temos?",
          "", "Os relatórios de atendimento e gestão levam 1 a 2 minutos."]
    return "\n".join(L)


def interpretar(texto):
    """Devolve (agente, argumentos) ou ('ajuda', []) ou (None, mensagem de erro)."""
    palavras = re.findall(r"[\wçãõáéíóúâêôà-]+", texto.lower())
    if not palavras:
        return "ajuda", []
    primeira = palavras[0].split("@")[0]
    if primeira in AJUDA_PALAVRAS:
        return "ajuda", []
    agente = APELIDOS.get(primeira)
    if not agente:
        agente, args = detectar_consulta(texto)
        if agente:
            return agente, args
        return None, "Não reconheci o pedido. Mande /ajuda para ver o que eu sei fazer."
    _, _, _, aceita_data, extra = AGENTES[agente]
    args = []
    resto = re.sub(r"^\s*/?\S+", "", texto, count=1).strip()
    if agente == "caixa" and pede_semana(resto):
        return agente, ["--semana"]
    if aceita_data:
        d = extrair_data(resto) or (extrair_data("dia " + resto) if re.fullmatch(r"\d{1,2}", resto.strip()) else None)
        if d:
            args.append(d.isoformat())
    if agente == "despacho":
        n = re.search(r"\b(\d{1,2})\b", resto)
        if n:
            args.append(n[1])
    if extra and agente != "caixa" and extra in palavras[1:]:
        args.append("--" + extra)
    return agente, args


def preparar_saida():
    """Os relatórios do bot são salvos numa pasta só do bot (os arquivos do cron das 7h pertencem ao root e este
    usuário não pode sobrescrevê-los). O histórico do resumo de gestão é copiado só para leitura (comparações)."""
    principal = os.environ.get("RELATORIO_DIR", "relatorios")
    pasta = os.environ.get("BOT_SAIDA_DIR", os.path.join(principal, "bot"))
    os.makedirs(pasta, exist_ok=True)
    hist = os.path.join(principal, "historico_resumo.json")
    try:
        if os.path.exists(hist):
            shutil.copyfile(hist, os.path.join(pasta, "historico_resumo.json"))
    except OSError:
        pass
    env = dict(os.environ)
    env["RELATORIO_DIR"] = pasta
    return env


def executar(chat, agente, args):
    script, desc, limite, _, _ = AGENTES[agente]
    if agente == "consulta" and len(args) == 3:
        i, f = date.fromisoformat(args[1]), date.fromisoformat(args[2])
        enviar(chat, "Período entendido: " + (i.strftime("%d/%m/%Y") if i == f else f"{i:%d/%m/%Y} a {f:%d/%m/%Y}"))
    for a in args if agente == "caixa" else []:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", a):
            d = date.fromisoformat(a)
            enviar(chat, f"Data entendida: {['segunda','terça','quarta','quinta','sexta','sábado','domingo'][d.weekday()]}, "
                         f"{d.strftime('%d/%m/%Y')}")
    if not ocupado.acquire(blocking=False):
        responder(chat, "Já estou gerando outro relatório. Aguarde ele terminar e peça de novo.")
        return
    try:
        if agente in ("auditor", "gestao"):
            responder(chat, f"Gerando: {desc}. Leva 1 a 2 minutos...")
        try:
            env = preparar_saida()
            r = subprocess.run([PY, os.path.join(DIR_APP, script), "--sem-envio", *args],
                               capture_output=True, text=True, timeout=limite, env=env)
        except subprocess.TimeoutExpired:
            responder(chat, f"O agente {agente} demorou mais de {limite // 60} minutos e foi interrompido.")
            return
        if r.returncode != 0:
            erro = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["sem detalhes"]
            responder(chat, f"O agente {agente} terminou com erro: {erro[0][:300]}")
            return
        corpo = r.stdout.split("\n\nSalvo em ")[0].strip()
        for parte in corpo.split("\n\n--------\n\n"):  # o resumo de gestão tem 2 mensagens
            responder(chat, parte)
            time.sleep(1)
    finally:
        ocupado.release()


# ------------------------------------------------------------------------ loop
# ------------------------------------------------------------------------ áudio
WHISPER_PY = os.environ.get("WHISPER_PY", "/opt/whisper/bin/python")
AMBIGUAS = {"os", "painel", "pagar", "fila", "prioridade", "prioridades", "sistema", "resumo"}  # palavras comuns na fala: só valem como PRIMEIRA palavra
MAX_SEGUNDOS_AUDIO = 60


def interpretar_voz(texto):
    """Na fala o agente pode vir em qualquer ponto da frase ('me mostra o caixa de hoje')."""
    palavras = re.findall(r"[\wçãõáéíóúâêôà-]+", texto.lower())
    if not palavras:
        return None, []
    if palavras[0] in APELIDOS or palavras[0] in AJUDA_PALAVRAS:
        return interpretar(texto)
    for p in palavras:
        if p in APELIDOS and p not in AMBIGUAS:
            agente = APELIDOS[p]
            extra = AGENTES[agente][4]
            if agente == "caixa":
                if pede_semana(texto):
                    return agente, ["--semana"]
                d = extrair_data(texto)
                return agente, [d.isoformat()] if d else []
            args = ["--" + extra] if extra and (extra in palavras or "semana" in palavras) else []
            return agente, args
        if p in AJUDA_PALAVRAS:
            return "ajuda", []
    return detectar_consulta(texto)


def baixar_audio(file_id):
    caminho = tg("getFile", file_id=file_id)["result"]["file_path"]
    with urllib.request.urlopen(f"{API}/file/bot{TOKEN}/{caminho}", timeout=60) as r:
        dados = r.read(10_000_000)
    fd, arquivo = tempfile.mkstemp(suffix=".oga")
    with os.fdopen(fd, "wb") as f:
        f.write(dados)
    return arquivo


def transcrever(chat, voz):
    """Devolve o texto falado, ou None (e avisa o motivo)."""
    script = os.path.join(DIR_APP, "transcrever.py")
    if not (os.path.exists(WHISPER_PY) and os.path.exists(script)):
        enviar(chat, "A transcrição de áudio ainda não está instalada na VM. Escreva o nome do agente por enquanto.")
        return None
    if (voz.get("duration") or 0) > MAX_SEGUNDOS_AUDIO:
        enviar(chat, f"Áudio longo demais (limite de {MAX_SEGUNDOS_AUDIO} segundos). Mande um áudio curto, só com o pedido.")
        return None
    arquivo = None
    try:
        arquivo = baixar_audio(voz["file_id"])
        r = subprocess.run([WHISPER_PY, script, arquivo], capture_output=True, text=True, timeout=240)
        texto = r.stdout.strip()
        if r.returncode != 0 or not texto:
            enviar(chat, "Não consegui entender o áudio. Tente falar mais perto do microfone ou escreva o pedido.")
            return None
        return texto
    except subprocess.TimeoutExpired:
        enviar(chat, "A transcrição demorou demais e foi interrompida. Tente um áudio mais curto.")
    except Exception as e:
        print(f"Falha na transcrição: {type(e).__name__}", file=sys.stderr, flush=True)
        enviar(chat, "Não consegui processar o áudio agora.")
    finally:
        if arquivo:
            try:
                os.remove(arquivo)
            except OSError:
                pass
    return None


def tratar_audio(chat, voz):
    texto = transcrever(chat, voz)
    if not texto:
        return
    agente, args = interpretar_voz(texto)
    if agente == "ajuda":
        enviar(chat, f"Entendi: \"{texto}\"\n\n" + texto_ajuda())
    elif agente is None:
        enviar(chat, f"Entendi: \"{texto}\"\nMas não reconheci um agente. Diga o nome dele (caixa, auditor, "
                     "operação ou gestão). /ajuda mostra a lista.")
    else:
        enviar(chat, f"Entendi: \"{texto}\" -> {agente}")
        executar(chat, agente, args)


def tratar(msg):
    chat = msg["chat"]["id"]
    quem = str((msg.get("from") or {}).get("id", ""))
    if not DONO:  # modo de descoberta: só diz o número de quem escreveu
        enviar(chat, f"Seu número no Telegram é {quem}. Coloque-o em TELEGRAM_USER_ID no arquivo de configuração "
                     "e reinicie o bot.")
        return
    if quem != DONO:
        print(f"Mensagem ignorada de usuário não autorizado ({quem}).", file=sys.stderr, flush=True)
        return
    voz = msg.get("voice") or msg.get("audio")
    if voz:
        threading.Thread(target=tratar_audio, args=(chat, voz), daemon=True).start()
        return
    agente, args = interpretar(msg.get("text") or "")
    if agente == "ajuda":
        enviar(chat, texto_ajuda())
    elif agente is None:
        enviar(chat, args)
    else:
        threading.Thread(target=executar, args=(chat, agente, args), daemon=True).start()


def main():
    if not TOKEN:
        raise SystemExit("Falta definir a variável de ambiente TELEGRAM_TOKEN.")
    print("Bot iniciado." + ("" if DONO else " TELEGRAM_USER_ID vazio: modo de descoberta."), flush=True)
    offset = None
    while True:
        try:
            r = tg("getUpdates", timeout=50, offset=offset, allowed_updates=["message"])
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                if u.get("message"):
                    tratar(u["message"])
        except urllib.error.HTTPError as e:
            print(f"Telegram respondeu erro {e.code}.", file=sys.stderr, flush=True)
            time.sleep(30 if e.code in (401, 404) else 5)
        except Exception as e:
            print(f"Falha de conexão ({type(e).__name__}); tentando de novo.", file=sys.stderr, flush=True)
            time.sleep(10)


if __name__ == "__main__":
    main()
