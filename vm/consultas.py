#!/usr/bin/env python3
"""
Consultas rápidas ("quanto recebemos esta semana?", "quantas OS abertas?"). Só contagens e valores, sem dados de clientes.

Uso: python3 consultas.py INTENCAO [AAAA-MM-DD AAAA-MM-DD] [--sem-envio]
INTENCAO: recebido | os_abertas | os_fechadas | contratos
"""
import sys
from collections import Counter
from datetime import timedelta

import comum as c


def periodo_txt(ini, fim):
    return ini.strftime("%d/%m") if ini == fim else f"{ini.strftime('%d/%m')} a {fim.strftime('%d/%m')}"


def recebido(ini, fim):
    pagos = c.titulos("pagos", data_pagamento_inicio=ini.isoformat(), data_pagamento_fim=fim.isoformat())
    pago = lambda t: float(t.get("valorPago") or 0)
    L = [f"*Recebido de clientes - {periodo_txt(ini, fim)}*",
         f"{c.brl(sum(pago(t) for t in pagos))} em {c.plural(len(pagos), 'fatura', 'faturas')}"]
    if ini != fim and (fim - ini).days <= 31:
        por_dia = {}
        for t in pagos:
            d = c.dia(t.get("dataPagamento"))
            if d:
                por_dia.setdefault(d, []).append(t)
        L.append("")
        d = ini
        while d <= fim:
            cs = por_dia.get(d, [])
            L.append(f"{d.strftime('%d/%m')} {c.DIAS[d.weekday()][:3]}: "
                     + (f"{c.brl(sum(pago(t) for t in cs))} ({len(cs)})" if cs else "nada"))
            d += timedelta(days=1)
    return "\n".join(L)


def os_abertas(_ini, _fim):
    hoje = c.hoje()
    todas = []
    for st in (0, 2, 3):
        todas += c.lista_os(status=st, data_cadastro_inicio=(hoje - timedelta(days=180)).isoformat(),
                            data_cadastro_fim=hoje.isoformat())
    L = ["*OS em aberto (últimos 180 dias)*", f"Total: {len(todas)}", ""]
    for nome, sel in (("Externas (campo)", [o for o in todas if c.eh_externa(o)]),
                      ("Internas (escritório)", [o for o in todas if not c.eh_externa(o)])):
        por_st = Counter(c.STATUS_OS.get(o.get("status_id"), "?") for o in sel)
        idades = [(hoje - c.dia(o.get("data_cadastro"))).days for o in sel if c.dia(o.get("data_cadastro"))]
        L.append(f"*{nome}:* {len(sel)}" + (f" ({', '.join(f'{v} {k.lower()}' for k, v in por_st.most_common())})"
                                          if sel else ""))
        if idades:
            L.append(f"  mais antiga: {c.plural(max(idades), 'dia', 'dias')} | até 2 dias: "
                     f"{sum(1 for i in idades if i <= 2)} | mais de 7 dias: {sum(1 for i in idades if i > 7)}")
    return "\n".join(L)


def os_fechadas(ini, fim):
    fin = c.lista_os(data_finalizacao_inicio=ini.isoformat(), data_finalizacao_fim=fim.isoformat())
    ext = [o for o in fin if c.eh_externa(o)]
    L = [f"*OS fechadas - {periodo_txt(ini, fim)}*",
         f"{len(fin)} no total: {len(ext)} externas e {len(fin) - len(ext)} internas"]
    por_t = Counter(c.tecnico(o) or "Sem técnico" for o in ext)
    if por_t:
        L += ["", "*Externas por técnico*"] + [f"- {t}: {n}" for t, n in por_t.most_common()]
    return "\n".join(L)


def contratos(_ini, _fim):
    r = c.sgp("/api/ura/listacontrato/")
    lista = r.get("contratos", r) if isinstance(r, dict) else r
    por = Counter((x.get("status") or "(sem status)") for x in lista)
    return "\n".join([f"*Contratos: {len(lista)}*", ""] + [f"- {k}: {v}" for k, v in por.most_common()])


INTENCOES = {"recebido": recebido, "os_abertas": os_abertas, "os_fechadas": os_fechadas, "contratos": contratos}


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args or args[0] not in INTENCOES:
        raise SystemExit("Uso: consultas.py " + "|".join(INTENCOES) + " [AAAA-MM-DD AAAA-MM-DD]")
    hoje = c.hoje()
    ini = c.dia(args[1]) if len(args) > 1 else hoje
    fim = c.dia(args[2]) if len(args) > 2 else ini
    print(INTENCOES[args[0]](ini or hoje, fim or ini or hoje))


if __name__ == "__main__":
    main()
