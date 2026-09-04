@echo off
REM ===========================================================================
REM  ALLANA - Agente do simulador (Windows)
REM
REM  Esta e' a METADE WINDOWS do arranjo com Docker.
REM
REM  O container roda o painel, o WhatsApp e a fila. A automacao do Santander
REM  NAO roda la' dentro: ela precisa do Brave com o SEU perfil logado, e disso
REM  o Linux do container nao tem como dar conta. Entao este agente fica aqui,
REM  pega os trabalhos da fila do painel, executa nesta maquina e devolve o
REM  resultado.
REM
REM  Sem Docker (SIMULATOR_MODE=local) voce NAO precisa deste arquivo:
REM  o iniciar.bat ja' faz tudo sozinho.
REM
REM  Ordem correta: primeiro "docker compose up -d", depois este arquivo.
REM
REM  As armadilhas do cmd.exe estao documentadas no iniciar.bat; as mesmas
REM  regras valem aqui (expansao atrasada, nada de "timeout", nenhum ")"
REM  dentro de aspas em bloco).
REM ===========================================================================
setlocal enabledelayedexpansion
title Allana - Agente do Simulador
cd /d "%~dp0"

echo.
echo  ==========================================================
echo        A L L A N A  -  AGENTE DO SIMULADOR
echo  ==========================================================
echo.

REM ---------------------------------------------------------------- [1/3] ---
echo  [1/3] Procurando o Python...
set "PY="
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"
if not defined PY (
  where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo  [X] Python nao encontrado.
  echo      Rode o iniciar.bat uma vez: ele cria o ambiente .venv.
  goto :FALHA
)
echo       ok: %PY%

REM ---------------------------------------------------------------- [2/3] ---
echo  [2/3] Conferindo a configuracao...
if not exist ".env" (
  echo.
  echo  [X] Arquivo .env nao encontrado em "%CD%".
  goto :FALHA
)

REM "tokens=1,* delims==" separa a chave do valor. Assim um unico teste
REM cobre os dois casos ruins: a chave nao existe (o laco nem roda) e a chave
REM existe vazia (o valor sai vazio). Nos dois, TOKEN fica indefinido.
set "TOKEN="
for /f "tokens=1,* delims==" %%a in ('findstr /b /c:"AGENT_TOKEN=" ".env"') do set "TOKEN=%%b"
if not defined TOKEN (
  echo.
  echo  [X] AGENT_TOKEN ausente ou vazio no .env.
  echo      Ele e' a senha entre o painel e este agente; sem ela o painel
  echo      recusa a conexao. Gere uma e coloque no .env:
  echo.
  echo          AGENT_TOKEN=cole-aqui-um-valor-longo-e-aleatorio
  echo.
  echo      O MESMO valor tem de estar no .env que o container le.
  goto :FALHA
)

set "ALVO="
for /f "tokens=1,* delims==" %%a in ('findstr /b /c:"PANEL_URL=" ".env"') do set "ALVO=%%b"
REM Sem parenteses no valor: um ")" entre aspas fecha bloco no cmd.
if not defined ALVO set "ALVO=http://127.0.0.1:8000 - padrao"
echo       ok: token presente, painel em !ALVO!

REM ---------------------------------------------------------------- [3/3] ---
echo  [3/3] Conectando ao painel...
echo.
echo  ----------------------------------------------------------
echo   O agente fica rodando. DEIXE ESTA JANELA ABERTA.
echo   Se o painel cair, ele tenta de novo sozinho a cada 10s.
echo   Para parar: Ctrl+C, ou feche a janela.
echo  ----------------------------------------------------------
echo.

%PY% agente.py
set "CODIGO=%ERRORLEVEL%"

echo.
if "%CODIGO%"=="0" (
  echo  Agente encerrado.
) else (
  echo  [X] O agente parou com codigo %CODIGO%.
  echo      As causas mais comuns:
  echo        - o container nao esta no ar        ^(docker compose ps^)
  echo        - AGENT_TOKEN daqui difere do dele  ^(erro 401^)
  echo        - o painel esta em SIMULATOR_MODE=local ^(erro 503^)
  echo      A mensagem exata do erro esta logo acima desta caixa.
)
echo.
pause
exit /b %CODIGO%

:FALHA
echo.
pause
exit /b 1
