"""Comandos de linha para as rotinas que precisam rodar sozinhas.

POR QUE CLI E NÃO SCHEDULER EM PROCESSO
---------------------------------------
O serviço do Render está no plano free, que **hiberna por inatividade**. Um
APScheduler dentro do processo web só dispararia quando alguém já tivesse
acordado o serviço — ou seja, exatamente quando não se pode confiar nele. Pior:
se um dia o serviço escalar para 2 instâncias, o job roda duas vezes.

Comando de linha resolve os dois: o agendador é externo (Render Cron Job ou
GitHub Actions schedule), roda uma vez só, e o resultado fica no log do
agendador em vez de se perder no log da web.

    flask conciliar               # exit 1 se o ledger divergir
    flask conciliar --json        # saída para alertas/monitoramento
    flask expirar-pontos --dry-run
"""
from __future__ import annotations

import json
import sys

import click
from flask import Flask
from flask.cli import with_appcontext


@click.command("conciliar")
@click.option("--json", "como_json", is_flag=True, help="saída em JSON")
@click.option("--limite", default=50, show_default=True,
              help="quantos achados listar (0 = todos)")
@with_appcontext
def cmd_conciliar(como_json: bool, limite: int) -> None:
    """Confere se o saldo das carteiras bate com o ledger. Só lê, não corrige.

    Sai com código 1 quando encontra divergência — assim o agendador marca a
    execução como falha e o alerta dispara sozinho, sem ninguém precisar ler log.
    """
    from .services.reconciliation import conciliar

    rel = conciliar()
    achados = rel.achados if limite == 0 else rel.achados[:limite]

    if como_json:
        click.echo(json.dumps({
            "gerado_em": rel.gerado_em.isoformat(),
            "ok": rel.ok,
            "carteiras_verificadas": rel.carteiras_verificadas,
            "transacoes_verificadas": rel.transacoes_verificadas,
            "total_achados": len(rel.achados),
            "exposicao_pts": rel.exposicao_pts,
            "achados": [
                {"tipo": a.tipo, "chave": a.chave, "detalhe": a.detalhe,
                 "delta_pts": a.delta_pts}
                for a in achados
            ],
        }, ensure_ascii=False, indent=2))
    else:
        click.echo(rel.resumo())
        for a in achados:
            click.echo(f"  {a}")
        se_omitidos = len(rel.achados) - len(achados)
        if se_omitidos > 0:
            click.echo(f"  … e mais {se_omitidos} achado(s); use --limite 0 para ver todos")

    if not rel.ok:
        sys.exit(1)


# Teto de segurança da expiração. Um mês normal expira uma fração do saldo em
# circulação; um número muito acima disso é sinal de erro de data, não de
# clientes esquecidos. Melhor abortar e um humano olhar do que zerar carteiras
# e descobrir depois — expiração é débito, e débito não tem "desfazer" barato.
TETO_PADRAO_PONTOS = 500_000


@click.command("expirar-pontos")
@click.option("--dry-run", is_flag=True, help="calcula sem gravar")
@click.option("--max-pontos", type=int, default=TETO_PADRAO_PONTOS, show_default=True,
              help="aborta se a varredura for expirar mais que isto")
@click.option("--forcar", is_flag=True,
              help="ignora o teto (use só depois de conferir a prévia)")
@with_appcontext
def cmd_expirar_pontos(dry_run: bool, max_pontos: int, forcar: bool) -> None:
    """Expira pontos além da janela de validade.

    Existia só como POST /admin/expire-points, disparado à mão. Saldo que expira
    "quando alguém lembra" é passivo contábil errado — e, se o regulamento
    promete a expiração, é descumprimento do próprio regulamento.

    Roda em DUAS passadas quando vai gravar: a primeira em dry-run, só para
    medir; se o total passar do teto, aborta sem tocar em nada. O custo da
    passada extra é irrelevante num job mensal, e é o que separa "expirou o mês"
    de "zerou a base".
    """
    from .services.expiration import expire_old_points_all

    previa = expire_old_points_all(dry_run=True)
    total = int(previa.get("points_expired_total") or 0)

    if not dry_run and total > max_pontos and not forcar:
        click.echo(json.dumps(previa, ensure_ascii=False, indent=2, default=str))
        click.echo(
            f"\nABORTADO: a varredura expiraria {total:,} pts, acima do teto de "
            f"{max_pontos:,}.".replace(",", "."),
            err=True)
        click.echo(
            "Confira a prévia acima. Se estiver correto, repita com --forcar "
            "ou eleve --max-pontos.", err=True)
        sys.exit(2)

    resultado = previa if dry_run else expire_old_points_all(dry_run=False)
    click.echo(json.dumps(resultado, ensure_ascii=False, indent=2, default=str))
    if resultado.get("errors"):
        sys.exit(1)


def register_cli(app: Flask) -> None:
    app.cli.add_command(cmd_conciliar)
    app.cli.add_command(cmd_expirar_pontos)
