"""Leitura da mensagem do consultor.

O formato real do grupo é livre, não rotulado::

    LUIZ FERNANDO TESTE
    31611176034

    Paulo Testes
    Amapá
    182.841.754.87

Nome, órgão/estado (opcional) e CPF. **Não há** ``Banco:``, ``Contrato:`` nem
palavra-gatilho. A versão anterior deste arquivo exigia os três campos
rotulados mais um "fazer simulação" — e por isso ignoraria toda mensagem real
do grupo.

Duas consequências de projeto:

* **O CPF é o gatilho.** Conversa comum ("bom dia", "alguém mandou o
  relatório?") não tem CPF; pedido tem. Isso separa os dois casos sem depender
  de o consultor lembrar de uma palavra mágica. Como a validação usa os dígitos
  verificadores, um número de telefone ou um contrato solto não passa por CPF.
* **O contrato é RESULTADO, não entrada.** Ele aparece nos cards que a
  simulação devolve (``7*****56``). Exigi-lo na entrada era inverter o fluxo.

O formato rotulado antigo continua aceito, para quem já escreve assim.
"""

from __future__ import annotations

import re
import unicodedata

from .models import ParsedRequest

TRIGGER_RE = re.compile(
    r"\b(fazer\s+simula(?:c|ç)(?:[aã]o|oes|ões)|simula(?:r|c|ç)[aã]?o?|simular|"
    r"consulta(?:r)?|verificar\s+margem)\b",
    re.IGNORECASE,
)
FIELD_RE = re.compile(r"^\s*([\w\s/.çãáàâéêíóôõú-]{2,30}?)\s*[:=]\s*(.+?)\s*$", re.IGNORECASE)

# 11 dígitos, aceitando os separadores que aparecem no grupo:
# 31611176034 · 182.841.754.87 · 529.982.247-25 · 118 902 594 97
CPF_SOLTO_RE = re.compile(r"(?<!\d)(\d[\d.\-\s]{9,17}\d)(?!\d)")

_ACCENTS = str.maketrans("çãáàâäéêëíïóôõöúüñ", "caaaaaeeeiioooouun")

FIELD_ALIASES = {
    "cpf/cnpj": "cpf", "cpf do cliente": "cpf", "documento": "cpf",
    "numero contrato": "contrato", "numero do contrato": "contrato",
    "n contrato": "contrato", "n do contrato": "contrato",
    "num contrato": "contrato", "contrato n": "contrato",
    "cel": "celular", "whatsapp": "telefone", "fone": "telefone",
    "instituicao": "banco", "instituicao financeira": "banco",
    "vendedor": "consultor", "corretor": "consultor",
    "cliente": "nome", "nome do cliente": "nome",
    "orgao": "orgao", "estado": "orgao", "uf": "orgao", "convenio": "orgao",
}

# Linhas que são comentário do consultor, não dado do cliente.
RUIDO = {
    "bom dia", "boa tarde", "boa noite", "obrigado", "obrigada", "ok", "blz",
    "por favor", "pfv", "urgente", "segue", "favor simular", "simula ai",
}


def only_digits(value: str | None) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def normalize_key(key: str) -> str:
    key = unicodedata.normalize("NFC", key.strip().lower()).translate(_ACCENTS)
    key = re.sub(r"[^a-z0-9\s/]", " ", key)
    key = re.sub(r"\s+", " ", key).strip()
    return FIELD_ALIASES.get(key, key)


def is_valid_cpf(cpf: str) -> bool:
    """Validação dos dois dígitos verificadores."""
    d = only_digits(cpf)
    if len(d) != 11 or d == d[0] * 11:
        return False
    for size in (9, 10):
        total = sum(int(d[i]) * (size + 1 - i) for i in range(size))
        check = (total * 10) % 11
        check = 0 if check == 10 else check
        if check != int(d[size]):
            return False
    return True


def has_trigger(text: str) -> bool:
    return bool(TRIGGER_RE.search(text or ""))


def find_cpf(text: str) -> str:
    """Primeiro CPF *válido* do texto. É o que identifica um pedido."""
    for bruto in CPF_SOLTO_RE.findall(text or ""):
        digitos = only_digits(bruto)
        if len(digitos) == 11 and is_valid_cpf(digitos):
            return digitos
    return ""


def _e_ruido(linha: str) -> bool:
    limpa = unicodedata.normalize("NFC", linha.strip().lower()).translate(_ACCENTS)
    limpa = re.sub(r"[^a-z\s]", "", limpa).strip()
    return limpa in RUIDO or len(limpa) < 2


def parse_request(
    text: str,
    fallback_consultant: str = "",
    require_trigger: bool = False,
    validate_cpf: bool = True,
    default_bank: str = "Santander",
) -> tuple[ParsedRequest | None, list[str]]:
    """Devolve ``(pedido, pendências)``.

    ``(None, [])``  -> não é um pedido de simulação, ignorar.
    ``(None, [..])`` -> é um pedido, mas falta informação; responder ao consultor.
    """
    text = text or ""
    if require_trigger and not has_trigger(text):
        return None, []

    linhas_brutas = [ln.strip() for ln in text.replace("\r", "\n").split("\n")]
    linhas_brutas = [ln for ln in linhas_brutas if ln]

    campos: dict[str, str] = {}
    livres: list[str] = []
    for linha in linhas_brutas:
        m = FIELD_RE.match(linha)
        if m:
            chave = normalize_key(m.group(1))
            valor = m.group(2).strip()
            if chave and valor:
                campos.setdefault(chave, valor)
        elif not TRIGGER_RE.fullmatch(linha.strip()):
            livres.append(linha)

    # ------------------------------------------------------------------ CPF
    cpf = only_digits(campos.get("cpf", "")) or find_cpf(text)

    if not cpf:
        # Sem CPF não há pedido. Mas convém separar dois casos bem diferentes:
        # o consultor mandou um número que PARECE CPF e está errado (avisar
        # que os dígitos não conferem), ou foi conversa comum (ignorar calado).
        candidatos = [only_digits(b) for b in CPF_SOLTO_RE.findall(text)]
        if any(len(d) == 11 for d in candidatos):
            return None, ["CPF válido (dígitos verificadores não conferem)"]
        if has_trigger(text) or any(len(d) >= 9 for d in candidatos):
            return None, ["CPF"]
        return None, []

    if len(cpf) != 11:
        return None, ["CPF com 11 dígitos"]
    if validate_cpf and not is_valid_cpf(cpf):
        return None, ["CPF válido (dígitos verificadores não conferem)"]

    # ------------------------------------------------ nome, órgão, consultor
    # As linhas livres, na ordem: nome do cliente, depois órgão/estado.
    uteis = [
        ln for ln in livres
        if only_digits(ln) != cpf and not _e_ruido(ln) and not TRIGGER_RE.search(ln)
    ]
    # descarta a linha que era só o CPF (com ou sem pontuação)
    uteis = [ln for ln in uteis if only_digits(ln) not in {cpf, ""} or not any(c.isdigit() for c in ln)]

    nome_cliente = (campos.get("nome") or "").strip()
    orgao = (campos.get("orgao") or "").strip()
    if not nome_cliente and uteis:
        nome_cliente = uteis[0]
    if not orgao and len(uteis) > 1:
        orgao = uteis[1]

    consultor = (campos.get("consultor") or fallback_consultant or "Consultor").strip()

    return (
        ParsedRequest(
            consultant_name=consultor,
            cpf=cpf,
            # Banco e contrato são opcionais: o grupo não os informa, e o
            # contrato só existe depois que a simulação roda.
            bank=(campos.get("banco") or default_bank).strip(),
            contract=re.sub(r"\s+", "", campos.get("contrato", "")),
            simulation_type=(campos.get("tipo") or "consignado").strip().lower(),
            customer_name=nome_cliente or "Lead",
            origin=orgao,
            phone=only_digits(campos.get("telefone") or campos.get("celular") or ""),
        ),
        [],
    )
