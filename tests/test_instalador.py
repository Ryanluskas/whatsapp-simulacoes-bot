"""Contrato dos scripts de instalação do Windows.

Oito dos últimos quinze commits mexeram em ``instalador/`` e nenhum deles tinha
teste: cada correção foi conferida à mão, numa máquina só, e três delas
voltaram atrás. Este arquivo trava por escrito as invariantes que quebraram de
verdade, lendo os scripts como texto.

O que ele **não** é: prova de que o instalador funciona. Instalar de verdade
exige uma máquina limpa, Windows, NSIS e rede — nada disso roda aqui. O que
ele garante é que quem editar os scripts não desfaz, sem perceber, uma decisão
que já custou uma release.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

INSTALADOR = Path(__file__).resolve().parent.parent / "instalador"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


def _texto(nome: str) -> str:
    caminho = INSTALADOR / nome
    assert caminho.is_file(), f"{nome} sumiu de instalador/"
    return caminho.read_text(encoding="utf-8", errors="replace")


@pytest.fixture(scope="module")
def instalar() -> str:
    return _texto("instalar.ps1")


@pytest.fixture(scope="module")
def remover() -> str:
    return _texto("remover.ps1")


@pytest.fixture(scope="module")
def montar() -> str:
    return _texto("montar-setup.ps1")


@pytest.fixture(scope="module")
def nsi() -> str:
    return _texto("AllanaBot.nsi")


class TestConfiguracaoQueOArqueiroLe:
    """O arquivo que o instalador cria tem de ser o que o Arqueiro lê.

    Durante uma release inteira o instalador escreveu ``[santander]`` e o
    ``bot.py`` procurava ``[acesso]``: o consultor preenchia CPF e senha, o
    login não acontecia e nada dizia por quê.
    """

    def test_credenciais_ini_usa_a_secao_acesso(self, instalar, montar):
        for nome, texto in (("instalar.ps1", instalar), ("montar-setup.ps1", montar)):
            assert "[acesso]" in texto, f"{nome} não escreve a seção [acesso]"
            assert "[santander]" not in texto, (
                f"{nome} voltou a escrever [santander]; o Arqueiro lê [acesso]")

    def test_a_pasta_do_arqueiro_e_criada_antes_do_arquivo(self, instalar):
        """Sem a pasta, o `Set-Content` derruba a instalação inteira.

        E derruba no fim, depois da .venv e dos 200 MB do Chromium.
        """
        trecho = instalar[instalar.index('$cred = Join-Path'):]
        trecho = trecho[:trecho.index("Set-Content")]
        assert "New-Item" in trecho and "Directory" in trecho, (
            "o credenciais.ini é escrito sem garantir a pasta arqueiro\\")


class TestDesinstalarNaoApagaDadosSozinho:
    """Desinstalar não pode apagar banco, comprovantes e sessão do WhatsApp
    sem alguém dizer que sim. O desinstalador do NSIS roda em modo silencioso:
    é justamente o caminho em que ninguém está olhando."""

    def test_silencioso_nao_liga_o_apagar(self, remover):
        assert re.search(r"if \(-not \$apagar -and -not \$Silencioso\)", remover), (
            "a pergunta sobre apagar dados mudou de forma; confira se o modo "
            "silencioso ainda cai no caminho que PRESERVA os dados")

    def test_sem_apagar_os_dados_sao_guardados(self, remover):
        guardar = remover[remover.index("if (-not $apagar)"):]
        for pasta in ("dados", "comprovantes", "perfis"):
            assert pasta in guardar, f"{pasta} não é preservada na desinstalação"
        assert "Move-Item" in guardar and "MyDocuments" in guardar

    def test_a_confirmacao_pede_palavra_inteira(self, remover):
        assert '-eq "APAGAR"' in remover, (
            "confirmar com 's' ou Enter apaga dados que não existem em outro lugar")

    def test_o_desinstalador_do_nsis_chama_o_modo_silencioso(self, nsi, remover):
        assert "remover.ps1" in nsi and "-Silencioso" in nsi
        assert "-ApagarDados" not in nsi, (
            "o desinstalador do NSIS passaria -ApagarDados: dados sem volta, sem pergunta")


class TestSecaoDeDesinstalacaoDoNsis:
    """Uma seção de desinstalação SEM o prefixo `un.` roda na INSTALAÇÃO: o
    setup instalava e desinstalava na sequência."""

    def test_a_secao_tem_o_prefixo(self, nsi):
        secoes = re.findall(r'^Section\s+"([^"]+)"', nsi, re.MULTILINE)
        desinstalar = [s for s in secoes if "esinstalar" in s]
        assert desinstalar, "a seção de desinstalação sumiu do .nsi"
        for nome in desinstalar:
            assert nome.startswith("un."), (
                f'Section "{nome}" roda durante a INSTALAÇÃO; falta o prefixo un.')

    def test_instala_sem_admin_na_pasta_do_usuario(self, nsi):
        assert "RequestExecutionLevel user" in nsi
        assert "$LOCALAPPDATA" in nsi, "o destino saiu da pasta do usuário"


class TestOPacoteNaoLevaSegredo:
    """O `montar-setup.ps1` roda na máquina de quem publica. O que escapar
    dali vai para o PC de todo mundo."""

    @pytest.mark.parametrize("proibido", [
        "credenciais.ini", "credentials.json", ".env", "bot.db", "*.csv", "*.exe",
    ])
    def test_a_lista_de_recusa_cobre_o_que_ja_escapou(self, montar, proibido):
        lista = montar[montar.index("$PROIBIDOS = @("):montar.index("function Passo")]
        assert f'"{proibido}"' in lista, (
            f"{proibido} saiu da lista de recusa; foi assim que um setup antigo "
            "entrou dentro do pacote novo")

    def test_o_conferente_para_a_montagem(self, montar):
        assert "throw \"o pacote levaria arquivo proibido" in montar, (
            "o conferente virou aviso; um pacote com segredo seria publicado")

    def test_so_o_codigo_do_arqueiro_entra(self, montar):
        permitido = montar[montar.index("$ARQUEIRO_PERMITIDO = @("):
                            montar.index("$PROIBIDOS = @(")]
        assert '"bot.py"' in permitido and '"otp_flow.py"' in permitido
        for proibido in ("clientes", "*.xlsx", "credenciais.ini"):
            assert proibido not in permitido


class TestNadaDependeDaMaquinaDeQuemPublica:
    """Um caminho da máquina de desenvolvimento dentro do instalador é uma
    instalação que só funciona aqui."""

    @pytest.mark.parametrize("arquivo", ["instalar.ps1", "AllanaBot.nsi", "remover.ps1"])
    def test_sem_caminho_absoluto_de_usuario(self, arquivo):
        texto = _texto(arquivo)
        achados = re.findall(r"[A-Za-z]:\\\\?Users\\\\?[A-Za-z0-9._-]+", texto)
        assert not achados, f"{arquivo} tem caminho de máquina: {achados}"

    @pytest.mark.parametrize("arquivo", ["app/config.py", ".env.example", "iniciar.bat"])
    def test_o_que_e_entregue_nao_aponta_para_a_pasta_de_ninguem(self, arquivo):
        """O que chega no PC do outro não pode apontar para o PC de quem fez.

        `SIM_BOT_PATH` tinha a pasta da máquina de desenvolvimento como padrão
        no código E como exemplo no `.env.example` — que o `iniciar.bat` copia
        para `.env` quando não existe. Em qualquer outra máquina o simulador
        morre dizendo que não achou o ``bot.py`` numa pasta de usuário que não
        é a dela — e o nome de quem publicou viaja junto do programa.
        """
        caminho = INSTALADOR.parent / arquivo
        texto = caminho.read_text(encoding="utf-8", errors="replace")
        achados = re.findall(r"[A-Za-z]:\\?Users\\?[A-Za-z0-9._-]+", texto)
        # %USERPROFILE%, $env:USERPROFILE e afins são o jeito certo e não casam.
        assert not achados, f"{arquivo} aponta para a pasta de alguém: {achados}"

    def test_o_instalador_baixa_o_python_de_fonte_oficial(self, instalar):
        url = re.search(r'\$PYTHON_URL\s*=\s*"([^"]+)"', instalar)
        assert url and url.group(1).startswith("https://www.python.org/"), (
            "o Python viria de uma origem não oficial")


class TestAtalhoNaoMenteQueFoiCriado:
    """O atalho é a única porta de entrada para quem instalou.

    O script Python que o cria engole toda exceção (`except: pass`) — o que faz
    sentido, um atalho que falha não deve derrubar a instalação — mas o
    PowerShell imprimia "atalho criado" logo depois, sem olhar. A instalação
    terminava dizendo "Pronto" e não havia ícone nenhum na área de trabalho.
    """

    def test_o_instalador_confere_o_lnk_antes_de_dizer_que_criou(self, instalar):
        trecho = instalar[instalar.index("function Criar-Atalhos"):]
        trecho = trecho[:trecho.index("Registrar-Desinstalador")]
        assert "Test-Path" in trecho.split("& $py $pyfile", 1)[1], (
            "o instalador anuncia o atalho sem conferir se o .lnk existe")

    def test_o_caminho_do_atalho_nao_leva_barra_dobrada(self, instalar):
        """`r'...'` do Python + `.Replace('\\','\\\\')` do PowerShell escapam
        duas vezes. O Windows tolera separador repetido, mas o que fica gravado
        no atalho é `C:\\\\Users\\\\...` — e basta uma API menos tolerante para
        o atalho apontar para lugar nenhum."""
        trecho = instalar[instalar.index("function Criar-Atalhos"):]
        trecho = trecho[:trecho.index("Registrar-Desinstalador")]
        assert '.Replace("\\", "\\\\")' not in trecho, (
            "o caminho é escapado duas vezes (raw string do Python já basta)")


@pytest.mark.skipif(not POWERSHELL, reason="sem PowerShell nesta máquina")
@pytest.mark.parametrize("arquivo", ["instalar.ps1", "remover.ps1", "montar-setup.ps1"])
def test_o_script_ao_menos_compila(arquivo):
    """Um erro de sintaxe aqui é uma instalação que morre na primeira linha.

    Só ANALISA o script (`Parser::ParseFile`); nada é executado — ninguém
    instala, copia ou apaga coisa alguma para este teste passar.
    """
    caminho = str(INSTALADOR / arquivo)
    ps = (
        "$e = $null; "
        f"$null = [System.Management.Automation.Language.Parser]::ParseFile('{caminho}', "
        "[ref]$null, [ref]$e); "
        "if ($e -and $e.Count) { $e | ForEach-Object "
        "{ Write-Output ($_.Extent.StartLineNumber.ToString() + ': ' + $_.Message) }; exit 1 }"
    )
    saida = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=120)
    assert saida.returncode == 0, f"{arquivo} não compila:\n{saida.stdout}{saida.stderr}"


class TestBinarioNaoVoltaParaOGit:
    """Executável já entrou no repositório duas vezes ("chore: remove
    executavel do controle de versao", duas vezes seguidas).

    As linhas que evitariam isso existiam no `.gitignore` — mas tinham sido
    gravadas em UTF-16 no fim de um arquivo UTF-8. O git lia cada letra
    separada por um byte zero e nenhum padrão casava: o `.gitignore` parecia
    certo para quem abria o arquivo e não ignorava nada.
    """

    GITIGNORE = INSTALADOR.parent / ".gitignore"

    def test_o_gitignore_e_utf8_sem_byte_zero(self):
        bruto = self.GITIGNORE.read_bytes()
        assert b"\x00" not in bruto, (
            "há byte zero no .gitignore: alguém acrescentou linha em UTF-16 "
            "(Add-Content/Out-File do PowerShell) e esses padrões não valem")
        bruto.decode("utf-8")   # levanta se não for UTF-8

    @pytest.mark.skipif(not shutil.which("git"), reason="sem git nesta máquina")
    @pytest.mark.parametrize("caminho", [
        "instalador/rcedit.exe",
        "instalador/dist/AllanaBot_v1.0.0-setup.exe",
        "AllanaBot_v1.0.0-setup.exe",
    ])
    def test_o_git_realmente_ignora_o_binario(self, caminho):
        """Pergunta ao git, não ao arquivo: é o git que decide."""
        saida = subprocess.run(
            [shutil.which("git"), "check-ignore", "-q", caminho],
            cwd=str(self.GITIGNORE.parent), capture_output=True, timeout=60)
        assert saida.returncode == 0, f"{caminho} NÃO está sendo ignorado pelo git"


class TestOArquivoDaSenhaSeExplica:
    """Numa instalação silenciosa (como o NSIS sempre roda) a senha do painel
    é sorteada, a janela fecha sozinha e o `.txt` na área de trabalho é o
    ÚNICO registro dela. Então ele tem de se explicar inteiro — e dizer que
    pode ser apagado depois, porque quem lê o painel lê CPF de cliente.
    """

    @pytest.fixture()
    def bloco(self) -> str:
        """Só a função que escreve o arquivo, do `function` ao fim do bloco."""
        texto = _texto("instalar.ps1")
        inicio = texto.index("function Escrever-Senha-Na-Area-De-Trabalho")
        return texto[inicio:texto.index("# --------", inicio)]

    def test_continua_gravando_o_arquivo(self, instalar):
        assert "Senha do Painel Allana.txt" in instalar, (
            "o arquivo da senha sumiu; numa instalação silenciosa ninguém mais "
            "descobre a senha do painel")
        assert "Escrever-Senha-Na-Area-De-Trabalho" in instalar

    @pytest.mark.parametrize("trecho,porque", [
        ("SENHA DO PAINEL", "não diz do que é a senha"),
        ("Guarde esta senha", "não manda guardar a senha"),
        ("APAGUE ESTE ARQUIVO", "não avisa que o arquivo pode ser apagado"),
        ("DASHBOARD_PASSWORD", "não diz como trocar a senha"),
    ])
    def test_o_texto_cobre_o_que_a_pessoa_precisa(self, bloco, trecho, porque):
        assert trecho in bloco, f"o arquivo da senha {porque}"

    def test_nao_depende_do_console_nem_de_pause(self, bloco):
        """A janela fecha sozinha no modo silencioso: nada de esperar tecla.

        Olha o CÓDIGO, não os comentários — que falam de `pause` justamente
        para explicar por que ele não pode estar aqui.
        """
        codigo = re.sub(r"<#.*?#>", "", bloco, flags=re.S)
        codigo = "\n".join(ln for ln in codigo.splitlines()
                           if not ln.lstrip().startswith("#"))
        for proibido in ("Read-Host", "pause", "[Console]::ReadKey"):
            assert proibido not in codigo, (
                f"o arquivo da senha depende de {proibido}, e nesse modo não há "
                "ninguém olhando a janela")

    def test_gravar_a_senha_nao_pode_derrubar_a_instalacao(self, bloco):
        """É o último passo, com tudo já instalado.

        `Join-Path` valida a unidade e LEVANTA quando ela não existe; com
        `$ErrorActionPreference = "Stop"` isso abortaria a instalação inteira
        no fim. A montagem do caminho é `[IO.Path]::Combine`, e a escrita é
        protegida.
        """
        assert "[IO.Path]::Combine" in bloco
        assert "Join-Path $desk" not in bloco
        assert "try {" in bloco and "catch {" in bloco

    def test_a_falha_de_escrita_mostra_a_senha(self, bloco):
        """Sem o arquivo, a senha tem de aparecer em algum lugar."""
        assert "Anote agora" in bloco and "$script:Senha" in bloco

    def test_a_area_de_trabalho_vem_do_shell(self, instalar):
        """Com o OneDrive, a área de trabalho não é %USERPROFILE%\Desktop.

        É o mesmo motivo pelo qual o atalho passou a usar `SpecialFolders`.
        """
        inicio = instalar.index("function Pasta-Da-Area-De-Trabalho")
        bloco = instalar[inicio:instalar.index("function Escrever-Senha")]
        assert "SpecialFolders('Desktop')" in bloco
        assert 'GetFolderPath("Desktop")' in bloco, "sem reserva se o python falhar"

    def test_o_script_continua_em_ascii(self):
        """O .ps1 não tem BOM: no PowerShell 5.1, um acento no arquivo vira
        símbolo estranho dentro do texto que a pessoa vai ler."""
        bruto = (INSTALADOR / "instalar.ps1").read_bytes()
        assert bruto[:3] != b"\xef\xbb\xbf", "o arquivo ganhou BOM"
        fora = [b for b in bruto if b > 127]
        assert not fora, f"{len(fora)} byte(s) não-ASCII em instalar.ps1"
