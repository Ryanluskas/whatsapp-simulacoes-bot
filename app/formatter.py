"""Textos que o bot envia no grupo.

Toda resposta carrega o nome do consultor e o ID da solicitacao. Combinado com
a citacao da mensagem original (ver ``whatsapp.py``), e' o que garante que um
consultor nunca leia o resultado de outro por engano quando varios pedidos
chegam juntos no mesmo grupo.
"""

from __future__ import annotations

from .models import ParsedRequest, SimulationResult
from .security import mask_cpf


def format_brl(value: float | None) -> str:
    if not value or value <= 0:
        return "R$ 0,00"
    return "R$ " + f"{value:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def format_missing(missing: list[str], consultant: str = "", request_id: str = "") -> str:
    if not missing:
        return ""
    if len(missing) == 1:
        faltando = missing[0]
    else:
        faltando = ", ".join(missing[:-1]) + " e " + missing[-1]
    head = f"âš ï¸ *{consultant}*, para simular preciso de:" if consultant else "âš ï¸ Para simular preciso de:"
    tail = f"\n\n_Ref.: {request_id}_" if request_id else ""
    return f"{head}\n{faltando}.{tail}"


def format_queued(request: ParsedRequest, position: int, request_id: str) -> str:
    fila = "prÃ³xima da fila" if position <= 1 else f"{position}Âª na fila"
    return (
        "ðŸ“¥ *SimulaÃ§Ã£o recebida*\n\n"
        f"ðŸ‘¤ Consultor: {request.consultant_name}\n"
        f"ðŸ“„ CPF: {mask_cpf(request.cpf)}\n"
        f"ðŸ¦ Banco: {request.bank}\n"
        f"ðŸ“‹ Contrato: {request.contract}\n\n"
        f"â³ PosiÃ§Ã£o: {fila}\n"
        f"ðŸ†” {request_id}"
    )


def format_result(result: SimulationResult, request_id: str) -> str:
    req = result.job.request

    cliente = req.customer_name if req.customer_name != "Lead" else ""

    if not result.ok:
        linhas = ["âŒ *NÃ£o foi possÃ­vel concluir a simulaÃ§Ã£o*", ""]
        if cliente:
            linhas.append(f"ðŸ§‘ {cliente}")
        linhas += [
            f"ðŸ“„ CPF: {mask_cpf(req.cpf)}",
            f"ðŸ‘¤ Consultor: {req.consultant_name}",
            "",
            f"Motivo: {result.error or result.status or 'falha na consulta'}",
            f"ðŸ†” {request_id}",
        ]
        return "\n".join(linhas)

    refin = "Sim âœ…." if result.has_refin else "NÃ£o"
    linhas = ["âœ… *SimulaÃ§Ã£o realizada*", ""]
    if cliente:
        linhas.append(f"ðŸ‘¤ {cliente}")
    linhas += [
        f"ðŸ“„ CPF: {mask_cpf(req.cpf)}",
        f"ðŸ’¼ Consultor: {req.consultant_name}",
        "",
    ]

    matriculas = getattr(result, "matriculas", getattr(req, "matriculas", None))
    if matriculas:
        if len(matriculas) == 1:
            linhas.append(f"ðŸ“‹ Simulei da matrÃ­cula: {matriculas[0]}")
        else:
            linhas.append(f"ðŸ“‹ Simulei das matrÃ­culas: {', '.join(matriculas)}")

    linhas.append(f"Refinanciamento: {refin}")

    if result.has_refin:
        linhas += [
            f"ðŸ’° Valor disponÃ­vel: {format_brl(result.reduction_value)}",
            f"ðŸ“Š Soma das parcelas: {format_brl(result.installment_sum)}",
            f"ðŸ“† Total de parcelas: {result.installment_count or '-'}",
            f"ðŸ“‰ Saldo devedor: {format_brl(result.debt_sum)}",
        ]
    else:
        if result.motivos:
            motivos_fmt = "\n".join(f"  â€¢ {m.get('texto', m)}" if isinstance(m, dict) else f"  â€¢ {m}" for m in result.motivos)
            linhas.append(f"âŒ NÃ£o liberou por restriÃ§Ã£o. Motivos encontrados:\n{motivos_fmt}")
        else:
            linhas.append("âŒ NÃ£o liberou no cÃ¡lculo.")

    linhas += [
        f"ðŸ’³ Margem livre: {result.margin or '-'}",
        f"ðŸ·ï¸ Contratos encontrados: {len(result.contracts)}",
        "",
        f"ðŸ” {request_id}",
    ]
    return "\n".join(linhas)


def format_unsupported_bank(consultant: str, bank: str, supported: set[str]) -> str:
    bancos = ", ".join(sorted(b.title() for b in supported)) or "Santander"
    return (
        "âš ï¸ *Banco nÃ£o suportado para simulaÃ§Ã£o automÃ¡tica*\n\n"
        f"ðŸ‘¤ Consultor: {consultant}\n"
        f"ðŸ¦ Banco informado: {bank}\n"
        f"âœ… DisponÃ­veis: {bancos}"
    )


def format_interrupted(consultant: str, request_id: str) -> str:
    return (
        "ðŸ”„ *Sua simulaÃ§Ã£o foi retomada*\n\n"
        f"ðŸ‘¤ Consultor: {consultant}\n"
        "O sistema reiniciou durante o processamento e a solicitaÃ§Ã£o voltou para a fila.\n"
        f"ðŸ†” {request_id}"
    )


def format_stored_result(linha: dict, mask: bool = True) -> str:
    """Monta a resposta a partir da LINHA DO BANCO, sem o objeto do job.

    Usado no reenvio: quando a entrega falha, o ``SimulationResult`` original
    ja' se foi -- so' resta o que ficou gravado. O texto e' o mesmo de sempre,
    com um aviso de que e' reenvio, para o consultor nao achar que simulamos
    duas vezes.
    """
    cpf = linha.get("cpf") or ""
    cpf_txt = mask_cpf(cpf) if mask else cpf
    cliente = (linha.get("customer_name") or "").strip()
    consultor = (linha.get("consultant_name") or "Consultor").strip()
    request_id = linha.get("request_id") or "?"
    status = (linha.get("status") or "").lower()

    linhas = ["ðŸ” *Resultado da sua simulaÃ§Ã£o* (reenvio)", ""]
    if cliente:
        linhas.append(f"ðŸ§‘ {cliente}")
    linhas.append(f"ðŸ“„ CPF: {cpf_txt}")
    linhas.append(f"ðŸ‘¤ Consultor: {consultor}")
    linhas.append("")

    if status == "error":
        motivo = (linha.get("error_message") or "").strip() or "falha na consulta"
        linhas.append(f"âŒ NÃ£o foi possÃ­vel concluir: {motivo}")
    else:
        try:
            reducao = float(linha.get("reduction_value") or 0)
        except (TypeError, ValueError):
            reducao = 0.0
        if reducao > 0:
            linhas.append(f"âœ… *Libera {format_brl(reducao)}*")
        else:
            linhas.append("âš ï¸ *NÃ£o libera*")
            linhas.append("Nenhum contrato encontrado para este CPF no banco informado.")

    linhas.append(f"ðŸ†” {request_id}")
    return "\n".join(linhas)

