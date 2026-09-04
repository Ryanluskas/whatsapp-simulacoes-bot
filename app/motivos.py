"""O que o portal disse — literal, e classificado sem ser filtrado.

O defeito que este módulo encerra
---------------------------------
Toda recusa virava a mesma frase::

    Nenhum contrato encontrado para esse CPF no banco.

Isso é **factualmente errado** na maioria dos casos. Um cliente com
``CLIENTE EM ATRASO EM PRODUTOS DO BANCO`` tem contrato — ele foi recusado
por outro motivo. E a ação seguinte do consultor muda por completo:

===============================================  ===========================
motivo real                                      o que o consultor faz
===============================================  ===========================
sem matrícula / matrícula inválida               pede a matrícula ao cliente
cliente em atraso em produtos do banco           orienta a regularizar
negado pela política de crédito                  parte para outro banco
rating riscos = 2                                não insiste
não passível a decisão manual                    não pede análise
nenhum contrato                                  oferece novo, não refin
===============================================  ===========================

Com a mensagem genérica todos viram "não deu", e o consultor perde tempo ou
desiste de um caso que daria.

A regra
-------
**Copiar o texto do portal literalmente.** Nunca parafrasear, traduzir,
resumir ou inventar. O consultor já conhece essas frases: ele lê
``NEGADO PELA POLITICA DE CREDITO`` e sabe o que fazer. A versão anterior
traduzia para "Não foi possível contatar a averbadora" e afins — mais bonito
e menos útil.

O mapa abaixo **classifica**, não filtra. Nada é descartado por não estar
nele: motivo desconhecido vai literal para o consultor e vira INFO no log,
para o mapa crescer com a realidade em vez de com suposição.
"""

from __future__ import annotations

import re

from .security import mask_cpf

# --------------------------------------------------------------- categorias
SEM_MATRICULA = "sem_matricula"
RESTRICAO = "restricao"
POLITICA = "politica"
SEM_CONTRATO = "sem_contrato"
FALHA_TECNICA = "falha_tecnica"
DESCONHECIDO = "desconhecido"

#: (padrao, categoria). A ordem importa: o primeiro que casar decide.
#:
#: Estes padroes vieram dos prints do grupo -- sao frases que o portal do
#: Santander mostra de verdade, nao suposicao. Acrescentar aqui muda apenas
#: a CLASSIFICACAO; o texto enviado ao consultor e' sempre o literal.
_MAPA = (
    (re.compile(r"matr[íi]cula", re.I), SEM_MATRICULA),
    (re.compile(r"em atraso|inadimpl|restri[çc][ãa]o|impedimento", re.I), RESTRICAO),
    (re.compile(r"rating|risco", re.I), RESTRICAO),
    (re.compile(r"pol[íi]tica de cr[ée]dito|negad[oa]|recusad[oa]", re.I), POLITICA),
    (re.compile(r"decis[ãa]o manual|n[ãa]o pass[íi]vel", re.I), POLITICA),
    (re.compile(r"nenhum contrato|sem contrato|n[ãa]o possui contrato", re.I),
     SEM_CONTRATO),
    (re.compile(r"averbadora|indispon[íi]vel|fora do ar|instabilidade|"
                r"tempo|timeout|n[ãa]o foi poss[íi]vel completar", re.I),
     FALHA_TECNICA),
)

#: Categorias em que repetir a simulacao tem chance de dar outro resultado.
#: Uma recusa de politica repetida cem vezes da' cem recusas -- so' atrasa a
#: resposta e castiga o portal.
_VALE_REPETIR = {FALHA_TECNICA}

#: Ruido conhecido da tela: texto que aparece e NAO e' motivo de recusa.
#:
#: "Carregado" chegou a virar motivo: o consultor recebeu "não foi possível
#: simular / Carregado", que nao explica nada e ainda passa a impressao de
#: que o sistema esta' confuso. Este filtro e' o UNICO ponto em que um texto
#: e' descartado, e ele lista o que ja' apareceu na pratica.
_RUIDO = re.compile(
    r"^(carregado|carregando|aguarde|processando|ok|sucesso|conclu[íi]do|"
    r"simula[çc][ãa]o|resultado|pesquisar|limpar|voltar|sair|menu|in[íi]cio)\W*$",
    re.I,
)


def classificar(texto: str) -> str:
    """Em que balde este motivo cai. Nunca decide se ele e' enviado."""
    limpo = (texto or "").strip()
    for padrao, categoria in _MAPA:
        if padrao.search(limpo):
            return categoria
    return DESCONHECIDO


def eh_ruido(texto: str) -> bool:
    """Texto de tela que nao e' motivo de recusa."""
    limpo = (texto or "").strip()
    return not limpo or bool(_RUIDO.match(limpo))


def _mascarar(texto: str) -> str:
    """Esconde CPF que venha dentro do motivo.

    A regra de mascaramento vale para tudo que sai do sistema, e um motivo
    do portal pode trazer o documento do cliente no meio da frase.
    """
    def trocar(casamento: re.Match) -> str:
        return mask_cpf(casamento.group(0))

    return re.sub(r"\d{3}\.?\d{3}\.?\d{3}-?\d{2}", trocar, texto or "")


def motivos_do_portal(textos) -> list[dict]:
    """Transforma o que foi lido da tela em motivos, sem perder nenhum.

    Devolve ``[{"texto": <literal, mascarado>, "categoria": <balde>}]`` na
    ORDEM em que o portal mostrou -- a ordem é informação: o portal costuma
    pôr o impedimento principal primeiro.

    Duplicatas saem (a mesma frase costuma aparecer em dois elementos da
    tela), e ruído conhecido sai. Nada mais é descartado.
    """
    vistos: list[dict] = []
    ja: set[str] = set()
    for bruto in textos or []:
        limpo = " ".join(str(bruto or "").split())
        if not limpo or eh_ruido(limpo):
            continue
        chave = limpo.casefold()
        if chave in ja:
            continue
        ja.add(chave)
        vistos.append({"texto": _mascarar(limpo)[:200],
                       "categoria": classificar(limpo)})
    return vistos


def vale_repetir(motivos) -> bool:
    """Repetir a simulacao pode dar outro resultado?

    Só quando TODO motivo for falha técnica. Um "negado pela política" no
    meio já decide: repetir daria a mesma recusa.
    """
    lista = list(motivos or [])
    if not lista:
        return False
    return all(m.get("categoria") in _VALE_REPETIR for m in lista)


def desconhecidos(motivos) -> list[str]:
    """Os que o mapa ainda não conhece — para o log INFO fazer o mapa crescer."""
    return [m["texto"] for m in (motivos or [])
            if m.get("categoria") == DESCONHECIDO]
