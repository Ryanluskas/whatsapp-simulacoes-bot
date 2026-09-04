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
    head = f"⚠️ *{consultant}*, para simular preciso de:" if consultant else "⚠️ Para simular preciso de:"
    tail = f"\n\n_Ref.: {request_id}_" if request_id else ""
    return f"{head}\n{faltando}.{tail}"


def format_queued(request: ParsedRequest, position: int, request_id: str) -> str:
    fila = "próxima da fila" if position <= 1 else f"{position}ª na fila"
    return (
        "📥 *Simulação recebida*\n\n"
        f"👤 Consultor: {request.consultant_name}\n"
        f"📄 CPF: {mask_cpf(request.cpf)}\n"
        f"🏦 Banco: {request.bank}\n"
        f"📋 Contrato: {request.contract}\n\n"
        f"⏳ Posição: {fila}\n"
        f"🆔 {request_id}"
    )


def format_result(result: SimulationResult, request_id: str) -> str:
    req = result.job.request

    cliente = req.customer_name if req.customer_name != "Lead" else ""

    if not result.ok:
        linhas = ["❌ *Não foi possível concluir a simulação*", ""]
        if cliente:
            linhas.append(f"🧑 {cliente}")
        linhas += [
            f"📄 CPF: {mask_cpf(req.cpf)}",
            f"👤 Consultor: {req.consultant_name}",
            "",
            f"Motivo: {result.error or result.status or 'falha na consulta'}",
            f"🆔 {request_id}",
        ]
        return "\n".join(linhas)

    refin = "Sim ✅" if result.has_refin else "Não"
    linhas = ["🔎 *Simulação realizada*", ""]
    if cliente:
        linhas.append(f"🧑 {cliente}")
    linhas += [
        f"📄 CPF: {mask_cpf(req.cpf)}",
        f"👤 Consultor: {req.consultant_name}",
        "",
        f"Refinanciamento: {refin}",
    ]
    if result.has_refin:
        linhas += [
            f"💰 Valor disponível: {format_brl(result.reduction_value)}",
            f"📉 Soma das parcelas: {format_brl(result.installment_sum)}",
            f"🔢 Total de parcelas: {result.installment_count or '-'}",
            f"🧾 Saldo devedor: {format_brl(result.debt_sum)}",
        ]
    linhas += [
        f"📊 Margem livre: {result.margin or '-'}",
        f"📁 Contratos encontrados: {len(result.contracts)}",
        "",
        f"🆔 {request_id}",
    ]
    return "\n".join(linhas)


def format_unsupported_bank(consultant: str, bank: str, supported: set[str]) -> str:
    bancos = ", ".join(sorted(b.title() for b in supported)) or "Santander"
    return (
        "⚠️ *Banco não suportado para simulação automática*\n\n"
        f"👤 Consultor: {consultant}\n"
        f"🏦 Banco informado: {bank}\n"
        f"✅ Disponíveis: {bancos}"
    )


def format_interrupted(consultant: str, request_id: str) -> str:
    return (
        "🔄 *Sua simulação foi retomada*\n\n"
        f"👤 Consultor: {consultant}\n"
        "O sistema reiniciou durante o processamento e a solicitação voltou para a fila.\n"
        f"🆔 {request_id}"
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

    linhas = ["🔁 *Resultado da sua simulação* (reenvio)", ""]
    if cliente:
        linhas.append(f"🧑 {cliente}")
    linhas.append(f"📄 CPF: {cpf_txt}")
    linhas.append(f"👤 Consultor: {consultor}")
    linhas.append("")

    if status == "error":
        motivo = (linha.get("error_message") or "").strip() or "falha na consulta"
        linhas.append(f"❌ Não foi possível concluir: {motivo}")
    else:
        try:
            reducao = float(linha.get("reduction_value") or 0)
        except (TypeError, ValueError):
            reducao = 0.0
        if reducao > 0:
            linhas.append(f"✅ *Libera {format_brl(reducao)}*")
        else:
            linhas.append("⚠️ *Não libera*")
            linhas.append("Nenhum contrato encontrado para este CPF no banco informado.")

    linhas.append(f"🆔 {request_id}")
    return "\n".join(linhas)
