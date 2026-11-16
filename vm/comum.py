"""
Funções compartilhadas dos agentes novos (despacho, cobrança, consultas, avisos, sentinela).
Só biblioteca padrão. Nada aqui grava ou imprime dados de clientes.
"""
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Fortaleza")
UA = "Mozilla/5.0 (compatible; FerreiraTelecom-Relatorio/1.0)"
DIAS = ["segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira", "sábado", "domingo"]
STATUS_OS = {0: "Aberta", 1: "Encerrada", 2: "Em execução", 3: "Pendente"}


def agora():
    return datetime.now(TZ)


def hoje():
    return agora().date()


# ------------------------------------------------------------------------- SGP
def sgp(path, **params):
    try:
        base, app, token = (os.environ[k] for k in ("SGP_URL", "SGP_APP", "SGP_TOKEN"))
    except KeyError as e:
        raise SystemExit(f"Falta definir a variável de ambiente {e.args[0]}.")
    dados = {"app": app, "token": token, **{k: v for k, v in params.items() if v not in (None, "")}}
    req = urllib.request.Request(base.rstrip("/") + path, data=urllib.parse.urlencode(dados).encode(),
                                 method="POST", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise SystemExit(f"Erro {e.code} em {path}: {e.read().decode('utf-8', 'ignore')[:200]}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Não consegui conectar no SGP: {e.reason}")


def paginar(path, chave, limite=500, **filtros):
    itens, offset = [], 0
    for _ in range(300):  # trava de segurança
        r = sgp(path, offset=offset, limit=limite, **filtros)
        lote = r.get(chave, []) if isinstance(r, dict) else []
        itens += lote
        total = (r.get("paginacao") or {}).get("total") if isinstance(r, dict) else None
        offset += limite
        if len(lote) < limite or (total is not None and offset >= total):
            break
    return itens


def lista_os(**filtros):
    return paginar("/api/ura/ordemservico/list/", "ordens_servicos", 1000, **filtros)


def titulos(status, **filtros):
    return paginar("/api/ura/titulos/", "titulos", 250, status=status, **filtros)


def eh_externa(o):
    return (o.get("tipo") or "").strip().lower() == os.environ.get("TIPO_EXTERNA", "Externa").strip().lower()


def tecnico(o):
    return (o.get("responsavel") or "").strip()


_cache_end = {}


def endereco_contrato(contrato):
    """Endereço do contrato (logradouro, número, bairro, cidade). Vazio se não achar. Só para mensagem privada ao dono."""
    if not contrato:
        return ""
    if contrato not in _cache_end:
        try:
            r = sgp("/api/ura/listacontrato/", contrato=contrato, exibir_endereco="true")
        except SystemExit:
            r = []
        lista = r.get("contratos", r) if isinstance(r, dict) else r
        e = (lista[0].get("endereco") or {}) if lista and isinstance(lista[0], dict) else {}
        rua = ", ".join(str(x) for x in (e.get("logradouro"), e.get("numero")) if x not in (None, ""))
        resto = " - ".join(str(x) for x in (e.get("bairro"), e.get("cidade")) if x)
        compl = f" ({e['complemento']})" if e.get("complemento") else ""
        _cache_end[contrato] = ", ".join(x for x in (rua, resto) if x) + compl
    return _cache_end[contrato]


def limpar_texto(t, limite=350):
    """Texto da OS sem tags HTML e sem quebras de linha, cortado no limite."""
    t = re.sub(r"<[^>]+>", " ", html.unescape(str(t or "")))
    t = re.sub(r"\s+", " ", t).strip()
    return t if len(t) <= limite else t[:limite].rstrip() + "..."


# ----------------------------------------------------------------------- texto
def brl(v):
    return "R$ " + f"{v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def dia(s):
    try:
        return date.fromisoformat(str(s)[:10])
    except ValueError:
        return None


def plural(n, um, varios):
    return f"{n} {um if n == 1 else varios}"


def vtit(t):
    return float(t.get("valorCorrigido") or t.get("valor") or 0)


# -------------------------------------------------------------------- Telegram
def negrito(texto):
    return re.sub(r"\*(\S(?:[^*\n]*?\S)?)\*", r"<b>\1</b>", html.escape(texto, quote=False))


def telegram_enviar(texto):
    """Manda a mensagem SOMENTE para o dono (TELEGRAM_USER_ID). Devolve True se o Telegram aceitou."""
    token, dono = os.environ.get("TELEGRAM_TOKEN", ""), os.environ.get("TELEGRAM_USER_ID", "").strip()
    api = os.environ.get("TELEGRAM_API_URL", "https://api.telegram.org").rstrip("/")
    if not (token and dono):
        print("Telegram não configurado (TELEGRAM_TOKEN / TELEGRAM_USER_ID).", file=sys.stderr)
        return False

    def chamar(corpo):
        req = urllib.request.Request(f"{api}/bot{token}/sendMessage", data=json.dumps(corpo).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8")).get("ok", False)

    ok = True
    pedaco = ""
    partes = []
    for linha in texto.strip().split("\n"):
        if len(pedaco) + len(linha) + 1 > 3800:
            partes.append(pedaco)
            pedaco = ""
        pedaco += linha[:3800] + "\n"
    if pedaco.strip():
        partes.append(pedaco)
    for p in partes:
        try:
            try:
                ok &= chamar({"chat_id": dono, "text": negrito(p.strip()), "parse_mode": "HTML"})
            except urllib.error.HTTPError as e:
                if e.code != 400:
                    raise
                ok &= chamar({"chat_id": dono, "text": p.strip()})
        except Exception as e:  # nunca imprime a URL (tem o token)
            print(f"Falha ao enviar no Telegram: {type(e).__name__}", file=sys.stderr)
            return False
    return ok


# ---------------------------------------------------------------------- estado
def pasta_estado():
    principal = os.environ.get("RELATORIO_DIR", "relatorios")
    pasta = os.environ.get("BOT_SAIDA_DIR", os.path.join(principal, "bot"))
    os.makedirs(pasta, exist_ok=True)
    return pasta


def carregar_estado(nome):
    try:
        with open(os.path.join(pasta_estado(), nome), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def salvar_estado(nome, dados):
    caminho = os.path.join(pasta_estado(), nome)
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False)
    os.replace(tmp, caminho)
