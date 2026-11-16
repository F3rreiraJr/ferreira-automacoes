#!/usr/bin/env python3
"""
Despacho - fila de OS externas em aberto, por prioridade, com sugestão de técnico (pela menor carga do dia).

SÓ SUGERE: não altera nada no SGP (quem designa o técnico continua sendo você, lá).
Mostra, para cada OS, o cliente, o endereço do contrato e o texto da OS (autorizado pelo dono em 13/11/2026, só nesta
mensagem privada do Telegram: nada disso é gravado em arquivo ou log, e não há telefone nem CPF).
(Ainda não considera a localização: isso entra quando tivermos a posição dos veículos do Prime Track.)

Variáveis: SGP_URL, SGP_APP, SGP_TOKEN
Opcionais:
    TIPO_EXTERNA        tipo de OS de campo (padrão Externa)
    DIAS_BACKLOG        quantos dias para trás procurar OS abertas (padrão 120)
    PRIORIDADE_ALTA     palavras do motivo que pedem urgência (separadas por vírgula)
    PRIORIDADE_BAIXA    palavras do motivo que podem esperar
    TECNICOS            lista fixa de técnicos (vírgula). Se vazio, usa quem fechou OS externa nos últimos 30 dias
    TECNICOS_AFASTADOS  técnicos que não devem receber sugestão (férias, folga)
    MAX_FILA            quantas OS listar com detalhes (padrão 10); aceita também um número: python3 despacho.py 20
Uso: python3 despacho.py [N] [--sem-envio]   (o Telegram chama com --sem-envio; nada é enviado por este script)
"""
import os
import sys
from collections import Counter
from datetime import timedelta

import comum as c

ALTA = "sem conex,sem acesso,sem internet,sem sinal,rompimento,rompido,queda,link,los,total,parou,offline,fora do ar"
BAIXA = "instal,ativa,mudanc,mudanç,retirada,recolh,visita comercial,troca de plano,cancel"
PESO_STATUS = {3: 4, 2: 0, 0: 2}  # pendente espera há mais tempo; em execução já está andando


def lista(nome, padrao):
    return [x.strip().lower() for x in os.environ.get(nome, padrao).split(",") if x.strip()]


def pontuar(o, hoje, alta, baixa):
    cad = c.dia(o.get("data_cadastro"))
    idade = max((hoje - cad).days, 0) if cad else 0
    texto = f"{o.get('motivo') or ''} {o.get('conteudo') or ''}".lower()
    pts = min(idade, 30) * 2 + PESO_STATUS.get(o.get("status_id"), 0)
    if any(k in texto for k in alta):
        pts += 40
    if any(k in texto for k in baixa):
        pts -= 10
    return pts, idade


def main():
    hoje = c.hoje()
    dias = int(os.environ.get("DIAS_BACKLOG", "120"))
    backlog = []
    for st in (0, 2, 3):
        backlog += c.lista_os(status=st, data_cadastro_inicio=(hoje - timedelta(days=dias)).isoformat(),
                              data_cadastro_fim=hoje.isoformat())
    fila = [o for o in backlog if c.eh_externa(o)]
    agenda = [o for o in c.lista_os(data_agendamento_inicio=hoje.isoformat(), data_agendamento_fim=hoje.isoformat())
              if c.eh_externa(o)]
    fin30 = [o for o in c.lista_os(data_finalizacao_inicio=(hoje - timedelta(days=30)).isoformat(),
                                   data_finalizacao_fim=hoje.isoformat()) if c.eh_externa(o)]

    afastados = set(lista("TECNICOS_AFASTADOS", ""))
    fixos = lista("TECNICOS", "")
    nomes = {}  # minúsculo -> nome como vem do SGP
    for o in fin30 + fila:
        t = c.tecnico(o)
        if t:
            nomes.setdefault(t.lower(), t)
    for t in os.environ.get("TECNICOS", "").split(","):
        if t.strip():
            nomes[t.strip().lower()] = t.strip()
    chaves = fixos or sorted({c.tecnico(o).lower() for o in fin30 if c.tecnico(o)})
    ativos = [nomes[k] for k in chaves if k in nomes and k not in afastados]

    carga = Counter()
    for o in agenda:
        if c.tecnico(o):
            carga[c.tecnico(o)] += 1
    for o in fila:
        if o.get("status_id") == 2 and c.tecnico(o):
            carga[c.tecnico(o)] += 1

    alta, baixa = lista("PRIORIDADE_ALTA", ALTA), lista("PRIORIDADE_BAIXA", BAIXA)
    pontuadas = sorted(((pontuar(o, hoje, alta, baixa), o) for o in fila), key=lambda x: -x[0][0])
    num = [a for a in sys.argv[1:] if a.isdigit()]
    max_fila = min(int(num[0]) if num else int(os.environ.get("MAX_FILA", "10")), 25)

    sem_tec = [o for o in fila if not c.tecnico(o)]
    L = ["*Despacho - OS externas em aberto*",
         f"{c.DIAS[hoje.weekday()].capitalize()}, {hoje.strftime('%d/%m/%Y')}", "",
         f"{len(fila)} OS externas em aberto: {len(sem_tec)} sem técnico, {c.plural(len(agenda), 'agendada', 'agendadas')} para hoje.", ""]
    if not fila:
        L.append("Nenhuma OS externa em aberto.")
    else:
        L.append("*Fila por prioridade* (idade + urgência do motivo; sugestão = técnico com menos serviço hoje)")
        sug = Counter()
        for n, ((pts, idade), o) in enumerate(pontuadas[:max_fila], 1):
            motivo = (o.get("motivo") or "sem motivo").strip()[:80]
            estado = c.STATUS_OS.get(o.get("status_id"), o.get("status") or "?")
            proto = f" prot. {o['ocorrencia']}" if o.get("ocorrencia") else ""
            if c.tecnico(o):
                quem = f"já com {c.tecnico(o)}"
            elif ativos:
                t = min(ativos, key=lambda x: (carga[x] + sug[x], x))
                sug[t] += 1
                quem = f"sugestão: {t} (carga hoje {carga[t] + sug[t] - 1})"
            else:
                quem = "sem técnico ativo para sugerir"
            L += ["", f"{n}. *OS {o.get('id')}*{proto} | {c.plural(idade, 'dia', 'dias')} | {estado}",
                  f"   Cliente: {(o.get('cliente') or 'não informado').strip()}",
                  f"   Endereço: {c.endereco_contrato(o.get('contrato')) or 'não encontrado no contrato'}",
                  f"   Motivo: {motivo}",
                  f"   O que diz a OS: {c.limpar_texto(o.get('conteudo')) or '(sem texto)'}",
                  f"   {quem}"]
        if len(pontuadas) > max_fila:
            L += ["", f"... e mais {len(pontuadas) - max_fila} OS. Para ver mais, mande: despacho {min(max_fila + 10, 25)}"]
    if ativos:
        L += ["", "*Carga de hoje por técnico* (agendadas + em execução)"]
        for t in sorted(ativos, key=lambda x: (-carga[x], x)):
            L.append(f"- {t}: {carga[t]}")
    L += ["", "Obs.: é só uma sugestão. Quem designa o técnico no SGP é você."]
    print("\n".join(L))


if __name__ == "__main__":
    main()
