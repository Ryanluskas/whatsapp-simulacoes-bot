"""Tudo o que o bot fala no WhatsApp, num arquivo so'.

Por que centralizar: os textos estavam divididos entre ``formatter.py`` e
``cards.py``, entao mudar o tom do bot exigia caçar frase em dois lugares e
o resultado saia desencontrado -- a legenda da imagem falava de um jeito e a
resposta em texto de outro.

O tom, decidido com o operador: **direto, curto, natural**. Uma pessoa
trabalhando, nao um sistema anunciando etapas. Na pratica:

* Nada de "Sua solicitacao foi processada com sucesso".
* Nada de anunciar o que esta' fazendo ("processando", "consultando").
* Emoji so' quando carrega informacao (o resultado), nunca como enfeite.
* Uma mensagem boa em vez de cinco pequenas.

A regra que manda em tudo isto: **o bot nao fala por falar.** Cada frase
daqui existe porque o consultor precisa dela para trabalhar.
"""

from __future__ import annotations

from .security import mask_cpf


def _brl(valor: float | None) -> str:
    try:
        numero = float(valor or 0)
    except (TypeError, ValueError):
        numero = 0.0
    return f"R$ {numero:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def _primeiro_nome(nome: str) -> str:
    """Como uma pessoa chamaria: "Ryan", nao "Ryan Silva Santos".

    Telefone NAO e' nome. Quando o consultor nao esta' na agenda, o WhatsApp
    entrega "+55 62 8000-1002" no lugar do nome, e a versao anterior cortava
    no primeiro espaco: o grupo recebia "+55, peguei. Tem uma na frente".
    Feio, e pior que feio -- **consultores diferentes viravam o mesmo "+55"**,
    entao duas respostas para duas pessoas pareciam duas respostas repetidas
    para a mesma pessoa.

    Sem nome de verdade, o certo e' nao chamar ninguem pelo nome. A frase
    funciona sem vocativo; ela nao funciona chamando alguem de "+55".
    """
    limpo = (nome or "").strip()
    if not limpo:
        return ""
    primeiro = limpo.split(" ")[0]
    # Precisa ter letra. "+55", "62", "(62)" nao sao nome de ninguem.
    if not any(ch.isalpha() for ch in primeiro):
        return ""
    return primeiro


def _abrindo(consultor: str, frase: str) -> str:
    """Abre a frase chamando pelo nome -- ou sem vocativo nenhum.

    Sem nome, a frase precisa comecar com maiuscula: "peguei." em minusculo
    no meio do grupo parece mensagem cortada. Um lugar so' para essa decisao,
    porque ela estava repetida em quatro funcoes e so' duas acertavam.
    """
    quem = _primeiro_nome(consultor)
    if quem:
        return f"{quem}, {frase}"
    return frase[:1].upper() + frase[1:]


# --------------------------------------------------------------- pedido ruim
def faltando(campos: list[str], consultor: str = "") -> str:
    """Falta dado para simular.

    Curta de proposito: o consultor sabe o que mandar, so' precisa saber que
    faltou. Sem "Ref.: REQ000000" -- nao houve simulacao, nao ha' o que
    referenciar.
    """
    if not campos:
        return ""
    if len(campos) == 1:
        lista = campos[0]
    else:
        lista = ", ".join(campos[:-1]) + " e " + campos[-1]

    quem = _primeiro_nome(consultor)
    abertura = f"{quem}, preciso" if quem else "Preciso"
    return f"{abertura} d{'o' if len(campos) == 1 else 'os'} {lista} pra consultar."


def banco_nao_atendido(consultor: str, banco: str, suportados: set[str]) -> str:
    lista = ", ".join(sorted(s.title() for s in suportados)) or "Santander"
    return _abrindo(consultor,
                    f"só consigo simular {lista}. Esse pedido é {banco}.")


# ------------------------------------------------------------------- na fila
def na_fila(posicao: int, consultor: str = "") -> str:
    """Aviso de espera. **NAO E' MAIS ENVIADO AO GRUPO.**

    Mantido porque a fala continua correta e pode voltar a ser util (num
    painel, num resumo diario), mas o ``manager`` deixou de manda-la: ela
    dobrava o numero de mensagens por pedido sem trazer nada que o consultor
    precise. Quem mandou o CPF sabe que mandou; o que ele espera e' o
    resultado.

    Numa leva de pedidos o grupo recebia uma fileira de "Ryan, peguei. Tem 14
    na frente" -- catorze mensagens antes da primeira resposta util.

    A posicao na fila continua no log e no painel, que e' onde ela informa
    alguma coisa.
    """
    if posicao <= 2:
        return _abrindo(consultor, "peguei. Tem uma na frente, já te respondo.")
    return _abrindo(consultor,
                    f"peguei. Tem {posicao - 1} na frente, já te respondo.")


# ---------------------------------------------------------------- resultados
#
# DOIS formatos, e a diferenca entre eles e' o ponto.
#
# A LEGENDA acompanha a imagem do card. O card ja' mostra nome, CPF, banco,
# contratos e parcelas -- repetir isso na legenda era duplicacao pura, o
# consultor lia a mesma informacao duas vezes na mesma mensagem.
#
# O TEXTO vai sozinho, quando a imagem nao sai. Ele precisa se sustentar: e'
# tudo que o consultor vai ver.
#
# Formatacao e' a do WhatsApp -- *negrito*, _italico_. Markdown nao existe
# aqui: "**" aparece literal na tela e "###" tambem.

#: Um por mensagem, sempre no comeco. Ele diz o desfecho antes da leitura.
EMOJI_LIBERA = "✅"
EMOJI_NAO_LIBERA = "⛔"
EMOJI_ERRO = "⚠️"


def _cliente_do(pedido) -> str:
    """O nome do cliente, ou vazio quando nao ha' nome de verdade.

    "Lead" e' o preenchimento que o parser usa quando o consultor nao mandou
    nome. Imprimi-lo seria pior que omitir.
    """
    nome = (pedido.customer_name or "").strip()
    return "" if nome.lower() == "lead" else nome


def _rodape(request_id: str, consultor: str = "", citou: bool = True) -> list[str]:
    """As linhas finais, na ordem que as duas regras exigem.

    O REQ fica SEMPRE por ultimo -- e' a chave de busca no painel, e quem
    procura olha o fim da mensagem. Por isso o "↩ consultor" entra ACIMA
    dele, e nao depois.

    O nome do consultor so' aparece quando a citacao falhou. Com a citacao
    funcionando a mensagem ja' esta' grudada no pedido dele, e repetir o nome
    e' ruido -- foi exatamente o "Ryan," no meio de toda resposta que poluiu
    o grupo.
    """
    linhas = []
    if not citou and consultor:
        linhas.append(f"↩ {consultor}")
    linhas.append(f"_{request_id}_")
    return linhas


def legenda(result, request_id: str, consultor: str = "", citou: bool = True) -> str:
    """Legenda da imagem. Curta: o card carrega o detalhe.

    Nao leva CPF nem contagem de contratos de proposito -- os dois estao
    impressos no card, logo acima.
    """
    pedido = result.job.request
    cliente = _cliente_do(pedido)

    if not result.ok:
        # O motivo ENTRA na legenda, ao contrario dos outros casos.
        #
        # A regra geral e' nao repetir o que o card ja' mostra. Num erro ela
        # nao se aplica: o motivo e' a unica informacao util da resposta, e
        # deixa-lo so' dentro da imagem obriga o consultor a abrir e ler o
        # card para descobrir se o problema foi dele, do CPF ou do banco.
        motivo = (result.error or result.status or "").strip()
        cabeca = f"{EMOJI_ERRO} *Não consegui simular*"
        cliente = _cliente_do(pedido)
        linhas = [cabeca + (f" · {cliente}" if cliente else "")]
        if motivo:
            linhas.append(motivo)
        return "\n".join([*linhas, *_rodape(request_id, consultor, citou)])
    libera = float(result.reduction_value or 0)
    liberou = bool(result.has_refin and libera > 0)
    cabeca = (f"{EMOJI_LIBERA} *{_brl(libera)}*" if liberou
              else f"{EMOJI_NAO_LIBERA} *Não libera*")
    if cliente:
        cabeca += f" · {cliente}"

    # A legenda de resultado fica em DUAS linhas, mesmo numa recusa.
    #
    # Cheguei a pôr o motivo do portal aqui, e era excesso: o princípio do
    # desenho é legenda curta, card com o detalhe. O motivo é detalhe, e o
    # lugar dele é o card — que passou a mostrá-lo (ver `cards.py`). Repetir
    # nos dois é exatamente a duplicação que os dois formatos evitam.
    #
    # O ERRO continua sendo a exceção: ali não há card com informação
    # nenhuma além do motivo.
    return "\n".join([cabeca, *_rodape(request_id, consultor, citou)])


def texto(result, request_id: str, mascarar: bool = True,
          consultor: str = "", citou: bool = True) -> str:
    """Resposta em texto, usada quando a imagem nao sai.

    Precisa se sustentar sozinha. O nome do cliente vai em CAIXA ALTA e
    negrito: num grupo com dezenas de respostas, e' o que o consultor varre
    procurando a dele.
    """
    pedido = result.job.request
    cliente = _cliente_do(pedido)
    titulo = cliente.upper() if cliente else ""

    if not result.ok:
        linhas = [f"{EMOJI_ERRO} *{titulo}*" if titulo
                  else f"{EMOJI_ERRO} *Não consegui simular*"]
        motivo = (result.error or result.status or "não consegui concluir").strip()
        if titulo:
            linhas.append(f"Não consegui simular: {motivo}")
        else:
            linhas.append(motivo)
        if result.retryable:
            linhas.append("Tentando de novo")
        return "\n".join([*linhas, *_rodape(request_id, consultor, citou)])

    libera = float(result.reduction_value or 0)
    liberou = bool(result.has_refin and libera > 0)

    emoji = EMOJI_LIBERA if liberou else EMOJI_NAO_LIBERA
    linhas = [f"{emoji} *{titulo}*" if titulo
              else f"{emoji} *{'Libera' if liberou else 'Não libera'}*"]
    if liberou:
        linhas.append(f"Libera *{_brl(libera)}*")

    identidade = f"CPF {mask_cpf(pedido.cpf) if mascarar else pedido.cpf}"
    origem = (pedido.origin or "").strip()
    if origem:
        identidade += f" · {origem}"
    linhas.append(identidade)

    if liberou:
        quantos = len(result.contracts)
        if quantos:
            contratos = f"{quantos} contrato{'s' if quantos != 1 else ''}"
            if result.installment_sum:
                contratos += f" · parcela {_brl(result.installment_sum)}"
            linhas.append(contratos)
    else:
        linhas.extend(_por_que_nao(result))

    return "\n".join([*linhas, *_rodape(request_id, consultor, citou)])


def _por_que_nao(result) -> list[str]:
    """As linhas que explicam a recusa — o texto do portal, literal.

    Uma frase por linha, na ORDEM em que o portal mostrou: a ordem é
    informação, o impedimento principal costuma vir primeiro. Em caixa alta
    como vieram, sem emoji no meio e sem comentário do bot explicando — o
    consultor conhece essas frases melhor que nós.

    "Nenhum contrato encontrado no banco" passou a valer só quando ela é
    VERDADE: o portal não disse nada e não trouxe resultado. Antes era a
    resposta de toda recusa, e um cliente com "CLIENTE EM ATRASO EM PRODUTOS
    DO BANCO" — que TEM contrato — recebia essa frase. O consultor lia "sem
    contrato", oferecia produto novo, e perdia a venda que existia.
    """
    return _so_os_textos(getattr(result, "motivos", None))


def _so_os_textos(motivos) -> list[str]:
    """Os textos literais, ou a frase genérica quando não houve motivo."""
    textos = [str((m or {}).get("texto") or "").strip() for m in (motivos or [])]
    return [t for t in textos if t] or ["Nenhum contrato encontrado no banco"]


def _motivos_guardados(linha: dict) -> list[str]:
    """Os motivos que ficaram no banco, para o reenvio repetir os mesmos.

    Uma linha antiga (de antes desta coluna existir) não tem nada guardado e
    cai na frase genérica — que para ela continua sendo tudo o que sabemos.
    """
    import json

    bruto = linha.get("motivos_portal")
    if not bruto:
        return _so_os_textos(None)
    try:
        return _so_os_textos(json.loads(bruto))
    except (TypeError, ValueError):
        return _so_os_textos(None)


def duas_versoes(montar, *args, consultor: str = "", **kwargs) -> tuple[str, str]:
    """A mesma resposta com e sem citacao.

    Quem envia so' descobre se a citacao pegou DEPOIS de tentar -- e nesse
    ponto a mensagem ja' teria de estar pronta. Entao as duas saem daqui
    juntas, e a camada de WhatsApp escolhe qual manda.

    A alternativa seria o ``manager`` adivinhar antes do envio, que e'
    exatamente o tipo de suposicao que ja' custou caro neste projeto.
    """
    return (montar(*args, consultor=consultor, citou=True, **kwargs),
            montar(*args, consultor=consultor, citou=False, **kwargs))


def resultado_reenviado(linha: dict, mascarar: bool = True) -> str:
    """Reenvio: monta a partir do que ficou no banco.

    Mesmo formato do texto -- o consultor nao deveria ter de aprender dois
    jeitos de ler a mesma coisa. So' a ultima linha muda, para ele nao achar
    que simulamos duas vezes.
    """
    cpf_bruto = linha.get("cpf") or ""
    cpf = mask_cpf(cpf_bruto) if mascarar else cpf_bruto
    cliente = (linha.get("customer_name") or "").strip()
    titulo = cliente.upper() if cliente and cliente.lower() != "lead" else ""
    status = (linha.get("status") or "").lower()
    request_id = (linha.get("request_id") or "").strip()

    # "interrupted" tambem e' falha: a simulacao travou e nao voltou.
    if status in ("error", "interrupted"):
        motivo = (linha.get("error_message") or "").strip() or "não consegui concluir"
        linhas = [f"{EMOJI_ERRO} *{titulo}*" if titulo
                  else f"{EMOJI_ERRO} *Não consegui simular*",
                  f"Não consegui simular: {motivo}"]
    else:
        try:
            libera = float(linha.get("reduction_value") or 0)
        except (TypeError, ValueError):
            libera = 0.0
        if libera > 0:
            linhas = [f"{EMOJI_LIBERA} *{titulo}*" if titulo
                      else f"{EMOJI_LIBERA} *Libera*",
                      f"Libera *{_brl(libera)}*", f"CPF {cpf}"]
        else:
            # O reenvio repete o MOTIVO, não a frase genérica: uma segunda
            # mensagem dizendo "nenhum contrato" sobre um cliente que tem
            # contrato erra duas vezes em vez de uma.
            linhas = [f"{EMOJI_NAO_LIBERA} *{titulo}*" if titulo
                      else f"{EMOJI_NAO_LIBERA} *Não libera*",
                      f"CPF {cpf}", *_motivos_guardados(linha)]

    linhas.append("_reenvio — a primeira não saiu_")
    if request_id:
        linhas.append(f"_{request_id}_")
    return "\n".join(linhas)


def interrompido(consultor: str) -> str:
    return _abrindo(consultor, "essa consulta parou no meio. "
                               "Me manda de novo que eu tento outra vez.")


# Marcas que identificam o que ESTE bot escreve. Usadas para o bot nunca
# tratar a propria resposta como um pedido -- ler a si mesmo ja' rendeu 53
# mensagens em cadeia no grupo do cliente.
ASSINATURAS = (
    # --- formato novo (legenda curta + texto completo) ---
    #
    # O identificador em italico e' a marca mais confiavel: ele so' existe em
    # resposta nossa, e esta' em TODAS elas -- inclusive na legenda de
    # "liberado", que nao contem nenhuma das outras marcas.
    #
    # Com o zero, e nao so' "_REQ": os ids sao REQ000001 em diante, e "_REQ"
    # sozinho casava com "LICENSE_REQUIRED" (e casaria com qualquer texto do
    # consultor que tivesse essa sequencia). Assinatura frouxa faz o bot
    # ignorar pedido de verdade, que e' pior do que ler a propria resposta.
    "_REQ0",
    # O emoji de status abre TODA resposta de resultado, e nao depende do
    # numero da solicitacao. "_REQ0" sozinho deixaria de casar em REQ100000.
    "✅ *",
    "⛔ *",
    "⚠️ *",
    "Libera *",
    "*Não libera*",
    "Nenhum contrato encontrado no banco",
    "Não consegui simular",
    "Tentando de novo",

    # --- falas atuais ---
    "pra consultar.",
    "já te respondo",
    "Não consegui simular",
    "Não libera",
    "✅ Libera",
    "só consigo simular",
    "_reenvio — a primeira não saiu_",
    "Me manda de novo que eu tento outra vez",

    # --- falas ANTIGAS, do formato anterior ---
    #
    # Continuam necessarias: o historico do grupo tem dezenas de mensagens
    # no formato velho, e o bot pode rolar ate' elas. Reconhecer so' as
    # falas novas deixaria essas reabrirem o laco de responder a si mesmo.
    # Sao baratas de manter e o custo de esquecer e' alto.
    "para simular preciso de",
    "*Simulação recebida*",
    "*Simulação realizada*",
    "*Resultado da sua simulação*",
    "*Não libera*",
    "*Libera R$",
    "não foi possível simular",
    "não foi possível concluir",
    "🆔 REQ",
)
