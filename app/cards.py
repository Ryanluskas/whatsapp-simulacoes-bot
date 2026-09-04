"""Imagem de resultado enviada no grupo.

Reproduz os cards de contrato que os consultores hoje mandam como print da
tela do Santander, e acrescenta em destaque o numero que eles de fato
procuram: quanto a operacao libera.

Por que gerar a imagem em vez de fotografar o portal
----------------------------------------------------
Os cards do Santander sao Web Components em shadow DOM e nao tem seletor
estavel - o proprio ``bot.py`` do Arqueiro os le varrendo texto profundo, e
nunca tira screenshot. Um print da tela dependeria de coordenadas fixas e
quebraria na primeira mudanca de layout deles. Como os seis campos do card
(``contrato``, ``parcelas``, ``valor_parcela``, ``taxa``, ``parcelas_pagas``,
``saldo_devedor``) ja' sao extraidos, a imagem gerada mostra exatamente o mesmo
conteudo sem depender do layout de terceiro.

O desenho
---------
Documento de banco, nao cartaz: sobrio, denso, hierarquia clara. Fundo
branco, **nenhum gradiente**. A cor do banco aparece em um lugar so' -- a
linha de 3px sob a faixa superior -- e num selo de 24px ao lado do nome do
banco. A marca em destaque e' a Capital Cred, nao a do banco: o comprovante
e' nosso.

Tudo e' local
-------------
Fonte e logos vao embutidos em ``data:`` URI. Nada e' buscado em tempo de
execucao: uma falha de rede nao pode transformar o comprovante do consultor
num quadrado vazio, e o mesmo pedido tem de gerar a mesma imagem no Windows
do operador e no container Linux -- fonte do sistema faria as larguras
mudarem e a tabela desalinhar.

O HTML e' renderizado pelo navegador que ja' esta' aberto (ver
``whatsapp.render_png``), em 2x, e reduzido na gravacao.
"""

from __future__ import annotations

import base64
from functools import lru_cache
from html import escape
from pathlib import Path

from .clock import parse_iso, utc_now
from .formatter import format_brl
from .models import SimulationResult
from .security import format_cpf, mask_cpf

LARGURA = 1080

ASSETS = Path(__file__).resolve().parent / "assets"

# --------------------------------------------------------------------- cores
#
# Escala fechada, e ela e' curta de proposito: cor demais num comprovante vira
# ruido e tira a atencao do unico numero que o consultor procura.
TINTA = "#0F172A"          # texto forte, faixa superior
TINTA_MEDIA = "#64748B"    # metadados, rodape
TINTA_FRACA = "#94A3B8"    # rotulos pequenos, cabecalho de tabela
LINHA = "#E2E8F0"
FUNDO_BLOCO = "#F8FAFC"
ZEBRA = "#FAFAFA"
VERDE = "#059669"          # so' quando libera

#: Cor institucional de cada banco. Usada na linha de 3px e no selo.
CORES_DO_BANCO = {
    "santander": "#EC0000",
    "caixa": "#0066A1",
    "caixa economica federal": "#0066A1",
}
COR_BANCO_PADRAO = TINTA

# Escala de espacamento: multiplos de 8. Valor solto no meio do CSS e' como
# a versao anterior foi ficando desalinhada.
E1, E2, E3, E4, E5 = 8, 16, 24, 32, 40


@lru_cache(maxsize=8)
def _fonte_embutida(arquivo: str) -> str:
    """A fonte em ``data:`` URI. Lida uma vez por processo."""
    caminho = ASSETS / "fonts" / arquivo
    if not caminho.exists():
        return ""
    dados = base64.b64encode(caminho.read_bytes()).decode("ascii")
    return f"data:font/woff2;base64,{dados}"


@lru_cache(maxsize=8)
def _logo_do_banco(banco: str) -> str:
    """SVG do banco, ja' com o fill branco. Vazio quando nao temos o logo."""
    chave = (banco or "").strip().lower().split()[0] if banco else ""
    caminho = ASSETS / "bancos" / f"{chave}.svg"
    if not caminho.exists():
        return ""
    return caminho.read_text(encoding="utf-8").strip()


def cor_do_banco(banco: str) -> str:
    return CORES_DO_BANCO.get((banco or "").strip().lower(), COR_BANCO_PADRAO)


def _tamanho_do_nome(nome: str) -> int:
    """Encolhe a fonte ate' o nome caber. Nunca corta com reticencias.

    Cortar um nome de cliente e' pior que uma fonte menor: o consultor usa o
    card para conferir de quem e' o resultado, e "MARIA DAS GRACAS..." nao
    confere nada. A largura util e' ~1000px; a 38px o Inter Bold rende cerca
    de 26 caracteres em caixa alta.
    """
    n = len(nome)
    if n <= 26:
        return 38
    if n <= 32:
        return 32
    if n <= 40:
        return 27
    return 22


def _campo(contrato: dict, chave: str, padrao: str = "—") -> str:
    valor = str(contrato.get(chave) or "").strip()
    return valor or padrao


def _taxa(contrato: dict) -> str:
    valor = _campo(contrato, "taxa", "")
    if not valor:
        return "—"
    return valor if "a.m" in valor or "%" in valor else f"{valor}% a.m"


def _marca_capital_cred() -> str:
    """Monograma da Capital Cred. O mesmo do painel, embutido."""
    return (
        "<svg viewBox='0 0 40 40' width='34' height='34' "
        "xmlns='http://www.w3.org/2000/svg'>"
        "<rect width='40' height='40' rx='10' fill='rgba(255,255,255,.14)'/>"
        "<path d='M12.2 28.6 L20 11.6 L27.8 28.6' stroke='#fff' stroke-width='2.7' "
        "fill='none' stroke-linecap='round' stroke-linejoin='round'/>"
        "<path d='M16.1 22.4 H23.9' stroke='#fff' stroke-width='2.7' "
        "stroke-linecap='round'/></svg>"
    )


def _selo_do_banco(banco: str) -> str:
    """Circulo de 24px com o logo do banco, ao lado do nome dele."""
    logo = _logo_do_banco(banco)
    cor = cor_do_banco(banco)
    if not logo:
        return ""
    # O SVG da biblioteca tem viewBox 0 0 108 108; encaixa direto no circulo.
    return (
        f"<span class='selo' style='background:{cor}'>"
        + logo.replace("<svg ", "<svg width='16' height='16' ")
        + "</span>"
    )


def _motivos_html(result) -> str:
    """O que o portal disse, literal, dentro do bloco do resultado.

    Uma frase por linha, na ordem em que o portal mostrou. Em caixa alta como
    vieram: o consultor reconhece essas frases pela forma como o banco as
    escreve, e "reescrever bonito" só tiraria a familiaridade.

    Sem motivo nenhum devolve vazio -- e aí o card diz apenas "Não libera",
    que nesse caso e' toda a verdade disponivel.
    """
    motivos = [str((m or {}).get("texto") or "").strip()
               for m in (getattr(result, "motivos", None) or [])]
    motivos = [m for m in motivos if m]
    if not motivos:
        return ""
    linhas = "".join(f"<div class='motivo'>{escape(m)}</div>" for m in motivos)
    return f"<div class='motivos'>{linhas}</div>"


def _linha_de_contrato(contrato: dict, par: bool) -> str:
    classe = " class='par'" if par else ""
    return (
        f"<tr{classe}>"
        f"<td class='txt'>{escape(_campo(contrato, 'contrato'))}</td>"
        f"<td>{escape(_campo(contrato, 'parcelas'))}</td>"
        f"<td>{escape(_campo(contrato, 'valor_parcela'))}</td>"
        f"<td>{escape(_taxa(contrato))}</td>"
        f"<td>{escape(_campo(contrato, 'parcelas_pagas'))}</td>"
        f"<td>{escape(_campo(contrato, 'saldo_devedor'))}</td>"
        "</tr>"
    )


def build_result_html(
    result: SimulationResult,
    request_id: str,
    *,
    show_client_data: bool = True,
    tz=None,
) -> str:
    """Monta o HTML do comprovante. Todo texto de terceiro passa por ``escape``."""
    req = result.job.request
    contratos = list(result.contracts or [])
    libera = float(result.reduction_value or 0)
    tem_oferta = result.ok and result.has_refin and libera > 0

    momento = (parse_iso(result.job.created_at.isoformat())
               if hasattr(result.job.created_at, "isoformat") else None)
    agora = momento or utc_now()
    if tz is not None:
        agora = agora.astimezone(tz)
    carimbo = agora.strftime("%d/%m/%Y às %H:%M")

    banco = (req.bank or "").strip()
    cor = cor_do_banco(banco)

    # "Lead" e' o marcador de "o consultor nao informou"; nao vai para a imagem.
    nome_cliente = (req.customer_name or "").strip()
    if nome_cliente.lower() in {"", "lead"}:
        nome_cliente = "CLIENTE NÃO INFORMADO"
    nome_cliente = nome_cliente.upper()

    cpf_texto = format_cpf(req.cpf) if show_client_data else mask_cpf(req.cpf)

    # ------------------------------------------------------- metadados
    meta = [escape(f"CPF {cpf_texto}")]
    if getattr(req, "origin", ""):
        meta.append(escape(req.origin))
    selo = _selo_do_banco(banco)
    meta.append(f"{selo}{escape(banco or '—')}")

    # ----------------------------------------------------- bloco do valor
    if not result.ok:
        motivo = escape(result.error or result.status or "falha na consulta")
        valor_html = (
            f"<div class='valor' style='border-left-color:{TINTA_MEDIA}'>"
            "<div class='valor-rotulo'>NÃO FOI POSSÍVEL SIMULAR</div>"
            f"<div class='valor-cinza motivo'>{motivo}</div>"
            "</div>"
        )
    elif tem_oferta:
        quantos = (f"<div class='contagem'>{len(contratos)} contratos</div>"
                   if len(contratos) > 1 else "")
        valor_html = (
            f"<div class='valor' style='border-left-color:{VERDE}'>"
            "<div class='valor-rotulo'>VALOR LIBERADO</div>"
            "<div class='valor-linha'>"
            f"<div class='valor-verde'>{escape(format_brl(libera))}</div>"
            f"{quantos}</div></div>"
        )
    else:
        # Cinza, nunca vermelho: nao liberar e' um resultado NORMAL da
        # consulta, e vermelho faria o consultor ler como erro do sistema.
        #
        # O MOTIVO do portal entra aqui, literal. Sem ele o card diz "Não
        # libera" e nada mais -- e a acao do consultor muda por completo
        # conforme o motivo: pedir a matricula, orientar a regularizar, ou
        # partir para outro banco. A legenda fica curta justamente porque
        # este bloco carrega o detalhe.
        valor_html = (
            f"<div class='valor' style='border-left-color:{TINTA_MEDIA}'>"
            "<div class='valor-rotulo'>RESULTADO DA CONSULTA</div>"
            "<div class='valor-cinza'>Não libera</div>"
            + _motivos_html(result)
            + "</div>"
        )

    # -------------------------------------------------------- a tabela
    if contratos:
        linhas = "".join(_linha_de_contrato(c, i % 2 == 1)
                         for i, c in enumerate(contratos))
        tabela = (
            "<table class='contratos'><thead><tr>"
            "<th class='txt'>CONTRATO</th><th>PARCELAS</th><th>VALOR PARCELA</th>"
            "<th>TAXA</th><th>PAGAS</th><th>SALDO</th>"
            f"</tr></thead><tbody>{linhas}</tbody></table>"
        )
    elif getattr(result, "motivos", None):
        # O portal DISSE por que recusou, e o motivo ja' esta' logo acima.
        #
        # Repetir "nenhum contrato encontrado" aqui seria o mesmo erro
        # factual que esta versao existe para corrigir: um cliente
        # "NEGADO PELA POLITICA DE CREDITO" tem contrato -- ele foi recusado
        # por outro motivo. As duas frases juntas no mesmo card diriam
        # coisas contraditorias.
        tabela = ""
    else:
        # Aqui a frase e' verdade: o portal nao disse nada e nao trouxe
        # contrato nenhum.
        tabela = ("<div class='vazio'>Nenhum contrato encontrado para este CPF "
                  "no banco informado.</div>")

    # -------------------------------------------------------- o rodape
    direita = []
    if result.installment_sum:
        direita.append(f"Parcelas: {escape(format_brl(result.installment_sum))}")
    if result.margin:
        direita.append(f"Margem: {escape(str(result.margin))}")

    regular = _fonte_embutida("Inter-Regular.woff2")
    semibold = _fonte_embutida("Inter-SemiBold.woff2")
    bold = _fonte_embutida("Inter-Bold.woff2")

    return f"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><style>
  @font-face {{ font-family:'InterCard'; font-weight:400; font-display:block;
               src:url('{regular}') format('woff2'); }}
  @font-face {{ font-family:'InterCard'; font-weight:600; font-display:block;
               src:url('{semibold}') format('woff2'); }}
  @font-face {{ font-family:'InterCard'; font-weight:700; font-display:block;
               src:url('{bold}') format('woff2'); }}

  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:#fff; }}
  .folha {{
    width:{LARGURA}px; background:#fff; color:{TINTA};
    font-family:'InterCard','DejaVu Sans',sans-serif;
    -webkit-font-smoothing:antialiased;
  }}

  /* 1. faixa superior ------------------------------------------------ */
  .faixa {{
    background:{TINTA}; color:#fff; height:110px;
    padding:0 {E5}px; display:flex; align-items:center;
    justify-content:space-between;
  }}
  .faixa-esq {{ display:flex; align-items:center; gap:{E2}px; }}
  .marca-nome {{ font-size:17px; font-weight:700; letter-spacing:.02em; }}
  .marca-sub {{
    font-size:11px; font-weight:600; letter-spacing:.18em;
    color:{TINTA_FRACA}; margin-top:5px;
  }}
  .faixa-dir {{ text-align:right; }}
  .req {{
    font-family:'DejaVu Sans Mono',ui-monospace,monospace;
    font-size:17px; font-weight:700; letter-spacing:.04em;
  }}
  .carimbo {{ font-size:11px; color:{TINTA_FRACA}; margin-top:5px; }}
  .risco {{ height:3px; background:{cor}; }}

  /* 2. cliente -------------------------------------------------------- */
  .cliente {{ padding:{E5}px {E5}px {E3}px; }}
  .nome {{
    font-size:{_tamanho_do_nome(nome_cliente)}px; font-weight:700;
    line-height:1.15; letter-spacing:-.01em;
  }}
  .meta {{
    margin-top:{E2}px; font-size:15px; color:{TINTA_MEDIA};
    display:flex; align-items:center; gap:{E1}px; flex-wrap:wrap;
  }}
  .meta .sep {{ color:{LINHA}; }}
  .meta span.item {{ display:inline-flex; align-items:center; gap:6px; }}
  .selo {{
    width:24px; height:24px; border-radius:50%;
    display:inline-flex; align-items:center; justify-content:center;
  }}

  /* 3. valor ---------------------------------------------------------- */
  .valor {{
    margin:0 {E5}px; padding:{E3}px {E4}px;
    background:{FUNDO_BLOCO}; border-left:4px solid {TINTA_MEDIA};
  }}
  .valor-rotulo {{
    font-size:12px; font-weight:600; letter-spacing:.14em; color:{TINTA_MEDIA};
  }}
  .valor-linha {{ display:flex; align-items:baseline; justify-content:space-between; }}
  .valor-verde {{
    font-size:56px; font-weight:700; color:{VERDE};
    letter-spacing:-.02em; margin-top:{E1}px;
  }}
  .valor-cinza {{
    font-size:40px; font-weight:700; color:{TINTA_MEDIA};
    letter-spacing:-.01em; margin-top:{E1}px;
  }}
  .valor-cinza.motivo {{ font-size:24px; font-weight:600; line-height:1.3; }}
  .contagem {{ font-size:14px; color:{TINTA_MEDIA}; font-weight:600; }}
  /* O motivo do portal: legível, mas sem competir com o resultado. */
  .motivos {{ margin-top:{E2}px; }}
  .motivo {{
    font-size:17px; font-weight:600; color:{TINTA};
    line-height:1.45; letter-spacing:.01em;
  }}

  /* 4. contratos ------------------------------------------------------ */
  .contratos {{
    width:calc(100% - {E5 * 2}px); margin:{E5}px {E5}px 0;
    border-collapse:collapse;
  }}
  .contratos th {{
    font-size:11px; font-weight:600; letter-spacing:.1em; color:{TINTA_FRACA};
    text-align:right; padding-bottom:{E1}px; border-bottom:1px solid {LINHA};
  }}
  .contratos th.txt, .contratos td.txt {{ text-align:left; }}
  .contratos td {{
    font-size:15px; padding:14px 0; text-align:right;
    font-variant-numeric:tabular-nums; font-feature-settings:'tnum' 1;
  }}
  .contratos tr.par td {{ background:{ZEBRA}; }}
  .contratos td.txt {{ font-weight:600; padding-left:{E1}px; }}
  .contratos tr.par td:first-child {{ padding-left:{E1}px; }}
  .contratos tr.par td:last-child {{ padding-right:{E1}px; }}
  .vazio {{
    margin:{E5}px {E5}px 0; padding:{E3}px; background:{FUNDO_BLOCO};
    color:{TINTA_MEDIA}; font-size:15px; text-align:center;
  }}

  /* 5. rodape --------------------------------------------------------- */
  .rodape {{
    margin-top:{E5}px; padding:{E3}px {E5}px;
    background:{FUNDO_BLOCO}; border-top:1px solid {LINHA};
  }}
  .rodape-linha {{
    display:flex; justify-content:space-between; align-items:baseline;
    font-size:13px; color:{TINTA_MEDIA};
  }}
  .aviso {{ margin-top:{E2}px; font-size:11px; color:{TINTA_FRACA}; }}
</style></head><body>
<div class="folha">

  <div class="faixa">
    <div class="faixa-esq">
      {_marca_capital_cred()}
      <div>
        <div class="marca-nome">Capital Cred</div>
        <div class="marca-sub">SIMULAÇÃO DE CONSIGNADO</div>
      </div>
    </div>
    <div class="faixa-dir">
      <div class="req">{escape(request_id)}</div>
      <div class="carimbo">{escape(carimbo)}</div>
    </div>
  </div>
  <div class="risco"></div>

  <div class="cliente">
    <div class="nome">{escape(nome_cliente)}</div>
    <div class="meta">{'<span class="sep">·</span>'.join(
        f'<span class="item">{p}</span>' for p in meta)}</div>
  </div>

  {valor_html}

  {tabela}

  <div class="rodape">
    <div class="rodape-linha">
      <div>Consultor: {escape(req.consultant_name or '—')}</div>
      <div>{escape(' · '.join(direita)) if direita else ''}</div>
    </div>
    <div class="aviso">Simulação sujeita à confirmação do banco.
      Valores podem sofrer alteração.</div>
  </div>

</div></body></html>"""
