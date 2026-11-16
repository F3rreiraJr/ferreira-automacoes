#!/usr/bin/env python3
"""
Lembrete das 18h (Telegram, só para o dono): contas a pagar que vencem HOJE e ainda estão pendentes (não pagas)
e as que vencem AMANHÃ. Se não houver nenhuma das duas, não manda nada (a menos que LEMBRETE_SEMPRE=1).

Uso: python3 lembrete_vencimentos.py [--sem-envio]
Variáveis: SGP_*, TELEGRAM_TOKEN, TELEGRAM_USER_ID
"""
import os
import sys
from datetime import timedelta

import comum as c
import contas_a_pagar_dia as cp


def bloco(titulo, dia, lista):
    L = [f"*{titulo}* - {c.DIAS[dia.weekday()]}, {dia.strftime('%d/%m')}"]
    if not lista:
        return L + ["Nenhuma conta pendente."]
    L.append(f"Total: {c.brl(sum(cp.valor(x) for x in lista))} ({c.plural(len(lista), 'conta', 'contas')})")
    L += [cp.linha(x) for x in sorted(lista, key=lambda x: -cp.valor(x))[:15]]
    if len(lista) > 15:
        L.append(f"... e mais {len(lista) - 15} contas (veja no SGP)")
    return L


def main():
    hoje = c.hoje()
    amanha = hoje + timedelta(days=1)
    base, app, token = (os.environ[k] for k in ("SGP_URL", "SGP_APP", "SGP_TOKEN"))
    base = base.rstrip("/")

    def do_dia(d):
        return cp.contas(base, app, token, status=cp.STATUS_PENDENTE,
                         data_vencimento_inicio=d.isoformat(), data_vencimento_fim=d.isoformat())

    lista_hoje, lista_amanha = do_dia(hoje), do_dia(amanha)
    if not (lista_hoje or lista_amanha) and os.environ.get("LEMBRETE_SEMPRE") != "1":
        print("Nada pendente para hoje nem para amanhã; nenhum lembrete enviado.")
        return
    L = ["*Lembrete das 18h: contas a pagar*", ""]
    L += bloco("HOJE (ainda pendentes)", hoje, lista_hoje) + [""] + bloco("AMANHÃ", amanha, lista_amanha)
    texto = "\n".join(L)
    if "--sem-envio" in sys.argv:
        print(texto)
    elif not c.telegram_enviar(texto):
        sys.exit("Não consegui enviar o lembrete no Telegram.")
    else:
        print("Lembrete enviado.")


if __name__ == "__main__":
    main()
