#!/usr/bin/env python3
"""
Aviso no Telegram (só para o dono) a cada OS externa fechada, com técnico e tempo.

O SGP só guarda a DATA de finalização, então o "tempo" é uma estimativa: o intervalo entre esta OS e a anterior
do mesmo técnico no mesmo dia (inclui deslocamento). A precisão depende de quantas vezes o script roda
(a cada 10 minutos, por exemplo). A 1ª OS do dia de cada técnico não tem intervalo.

Na primeira execução ele só "memoriza" as OS que já estavam fechadas (para não mandar um monte de avisos de uma vez).
Cliente: mostra SÓ o nome do cliente (autorizado pelo dono em 13/11/2026), apenas na mensagem privada do Telegram ao dono.
O nome nunca é gravado em arquivo de estado nem impresso no log do cron (o estado guarda só o número da OS).
Nunca telefone, CPF ou endereço.

Uso: python3 aviso_os_fechada.py [--sem-envio]   (--sem-envio: mostra o que mandaria e NÃO memoriza nada)
Variáveis: SGP_*, TELEGRAM_TOKEN, TELEGRAM_USER_ID, TIPO_EXTERNA
"""
import sys
from datetime import datetime, timedelta

import comum as c

ARQ = "aviso_os_estado.json"
MAX_AVISOS = 8


def duracao(td):
    m = int(td.total_seconds() // 60)
    return f"{m // 60}h{m % 60:02d}" if m >= 60 else f"{m} min"


def main():
    sem_envio = "--sem-envio" in sys.argv
    agora, hoje = c.agora(), c.hoje()
    fin = [o for o in c.lista_os(data_finalizacao_inicio=(hoje - timedelta(days=1)).isoformat(),
                                 data_finalizacao_fim=hoje.isoformat()) if c.eh_externa(o)]
    estado = c.carregar_estado(ARQ)
    primeira = not estado
    vistos = {k: v for k, v in (estado.get("vistos") or {}).items()
              if (hoje - (c.dia(v) or hoje)).days <= 3}
    ultima = estado.get("ultima") or {}
    novas = [o for o in fin if str(o.get("id")) not in vistos]

    if primeira and not sem_envio:
        for o in fin:
            vistos[str(o.get("id"))] = hoje.isoformat()
        c.salvar_estado(ARQ, {"vistos": vistos, "ultima": {}})
        print(f"Primeira execução: {len(fin)} OS já fechadas foram memorizadas. Os próximos fechamentos serão avisados.")
        return
    if not novas:
        print("Nenhuma OS nova fechada.")
        return

    msgs = []
    contagem_hoje = {}
    for o in fin:
        if c.dia(o.get("data_finalizacao")) == hoje:
            contagem_hoje[c.tecnico(o)] = contagem_hoje.get(c.tecnico(o), 0) + 1
    for o in novas[:MAX_AVISOS]:
        tec = c.tecnico(o) or "Sem técnico"
        proto = f" (prot. {o['ocorrencia']})" if o.get("ocorrencia") else ""
        cad = c.dia(o.get("data_cadastro"))
        idade = f" | aberta há {c.plural((hoje - cad).days, 'dia', 'dias')}" if cad else ""
        linha2 = ""
        ult = ultima.get(tec)
        if ult and ult[:10] == agora.date().isoformat():
            intervalo = agora - datetime.fromisoformat(ult)
            linha2 = (f"\nIntervalo desde a OS anterior dele: {duracao(intervalo)} (inclui deslocamento)"
                      if intervalo.total_seconds() >= 60 else "\nFechada junto com a anterior (mesma checagem)")
        else:
            linha2 = "\nPrimeira OS dele hoje (sem intervalo para comparar)"
        aux = o.get("tecnicos_auxiliares") or []
        msgs.append(f"*OS fechada* às {agora:%H:%M} - {tec}" + (f" (+ {', '.join(map(str, aux))})" if aux else "")
                    + f"\nOS {o.get('id')}{proto} | {(o.get('motivo') or 'sem motivo')[:50]}{idade}"
                    + f"\nCliente: {(o.get('cliente') or 'não informado').strip()}{linha2}"
                    + (f"\nFechadas por ele hoje: {contagem_hoje.get(c.tecnico(o), 0)}" if c.tecnico(o) else ""))
        ultima[tec] = agora.isoformat(timespec="seconds")
    if len(novas) > MAX_AVISOS:
        msgs.append(f"... e mais {len(novas) - MAX_AVISOS} OS fechadas nesta checagem.")
    texto = "\n\n".join(msgs)

    if sem_envio:
        print(texto)
        return
    if not c.telegram_enviar(texto):
        sys.exit("Não consegui enviar o aviso no Telegram (nada foi memorizado; tento de novo na próxima).")
    for o in novas:
        vistos[str(o.get("id"))] = hoje.isoformat()
    c.salvar_estado(ARQ, {"vistos": vistos, "ultima": ultima})
    print(f"{len(novas)} aviso(s) enviado(s).")


if __name__ == "__main__":
    main()
