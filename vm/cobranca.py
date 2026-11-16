#!/usr/bin/env python3
"""
Cobrança - inadimplência por tempo de atraso e por ciclo de vencimento (dias 5, 10, 15, 20, 25, 30).
Só contagens e valores: NENHUM nome, CPF, telefone ou endereço de cliente. Não envia nada a clientes.

Variáveis: SGP_URL, SGP_APP, SGP_TOKEN
Opcional: JANELA_INADIMPLENCIA_DIAS (padrão 365)
Uso: python3 cobranca.py [--sem-envio]
"""
import os
from collections import defaultdict
from datetime import timedelta

import comum as c

CICLOS = (5, 10, 15, 20, 25, 30)
FAIXAS = ((1, 7, "1 a 7 dias"), (8, 30, "8 a 30 dias"), (31, 60, "31 a 60 dias"), (61, 10 ** 6, "mais de 60 dias"))


def main():
    hoje = c.hoje()
    janela = int(os.environ.get("JANELA_INADIMPLENCIA_DIAS", "365"))
    vencidos = c.titulos("abertos", data_vencimento_inicio=(hoje - timedelta(days=janela)).isoformat(),
                         data_vencimento_fim=(hoje - timedelta(days=1)).isoformat())
    a_vencer = c.titulos("abertos", data_vencimento_inicio=hoje.isoformat(),
                         data_vencimento_fim=(hoje + timedelta(days=1)).isoformat())

    clientes = {t.get("clienteContrato") or t.get("clienteCpfcnpj") for t in vencidos}
    L = ["*Cobrança - inadimplência*", f"{c.DIAS[hoje.weekday()].capitalize()}, {hoje.strftime('%d/%m/%Y')}", ""]
    if not vencidos:
        L.append("Nenhuma fatura vencida em aberto.")
    else:
        L.append(f"*Total vencido: {c.brl(sum(c.vtit(t) for t in vencidos))}* em "
                 f"{c.plural(len(vencidos), 'fatura', 'faturas')} / {c.plural(len(clientes), 'cliente', 'clientes')}")
        L += ["", "*Por tempo de atraso*"]
        for ini, fim, nome in FAIXAS:
            sel = [t for t in vencidos if c.dia(t.get("dataVencimento"))
                   and ini <= (hoje - c.dia(t.get("dataVencimento"))).days <= fim]
            if sel:
                L.append(f"- {nome}: {c.brl(sum(c.vtit(t) for t in sel))} ({len(sel)})")
        por_ciclo = defaultdict(list)
        for t in vencidos:
            d = c.dia(t.get("dataVencimento"))
            por_ciclo[d.day if d and d.day in CICLOS else 0].append(t)
        L += ["", "*Por ciclo de vencimento*"]
        for dd in sorted(por_ciclo, key=lambda k: -sum(c.vtit(t) for t in por_ciclo[k])):
            L.append(f"- {'dia ' + str(dd) if dd else 'outros dias'}: "
                     f"{c.brl(sum(c.vtit(t) for t in por_ciclo[dd]))} ({len(por_ciclo[dd])})")
    L.append("")
    for rotulo, d in (("hoje", hoje), ("amanhã", hoje + timedelta(days=1))):
        sel = [t for t in a_vencer if c.dia(t.get("dataVencimento")) == d]
        L.append(f"Vencem {rotulo}: {c.brl(sum(c.vtit(t) for t in sel))} ({len(sel)})")
    print("\n".join(L))


if __name__ == "__main__":
    main()
