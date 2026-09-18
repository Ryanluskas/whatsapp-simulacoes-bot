@echo off
REM ===========================================================================
REM  ALLANA - Sistema de Simulacoes via WhatsApp
REM  Duplo clique: prepara o ambiente, sobe o sistema e abre o painel.
REM
REM  Tres armadilhas do cmd.exe em que a versao anterior deste arquivo caiu e
REM  que estao resolvidas aqui:
REM
REM  1. Expansao atrasada. O cmd expande %VAR% quando ANALISA um bloco entre
REM     parenteses, nao quando o executa. O script antigo definia %PRONTO%
REM     dentro do bloco e o lia no mesmo bloco, entao valia sempre vazio e
REM     avisava "o servidor nao respondeu" mesmo tendo subido.
REM     Solucao: setlocal enabledelayedexpansion + !VAR!.
REM  2. "timeout" aborta quando a entrada padrao esta redirecionada.
REM     Solucao: "ping -n 2 127.0.0.1".
REM  3. Um ")" dentro de aspas fecha o bloco parentizado mesmo assim.
REM     Solucao: a consulta de status vive numa subrotina, nao num bloco.
REM ===========================================================================
setlocal enabledelayedexpansion
title Allana - Sistema de Simulacoes
cd /d "%~dp0"
set "RAIZ=%CD%"

set "PASSOS=6"
set "ERRO=0"

echo.
echo  ==========================================================
echo           A L L A N A  -  SIMULACOES
echo  ==========================================================
echo.

REM ---------------------------------------------------------------- [1/6] ---
echo  [1/%PASSOS%] Procurando o Python...
set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo         [ERRO] Python nao encontrado.
  echo                Instale o Python 3.10 ou superior de python.org
  echo                e marque "Add Python to PATH" na instalacao.
  goto :FALHA
)
for /f "tokens=2" %%v in ('%PY% --version 2^>^&1') do set "PYVER=%%v"
echo         OK - Python !PYVER!

REM ---------------------------------------------------------------- [2/6] ---
echo  [2/%PASSOS%] Preparando o ambiente virtual...
if not exist "%RAIZ%\.venv\Scripts\python.exe" (
  echo         Criando .venv - isso leva alguns segundos...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo         [ERRO] Falha ao criar o ambiente virtual.
    goto :FALHA
  )
  echo         OK - ambiente criado
) else (
  echo         OK - ambiente ja existe
)
set "VPY=%RAIZ%\.venv\Scripts\python.exe"
set "PYTHONUNBUFFERED=1"
set "PYTHONUTF8=1"

REM ---------------------------------------------------------------- [3/6] ---
REM O marcador guarda o hash do requirements.txt: se as dependencias mudarem,
REM a instalacao roda de novo sozinha. (O marcador antigo era um arquivo vazio,
REM entao uma dependencia nova nunca chegava a ser instalada.)
echo  [3/%PASSOS%] Verificando dependencias...
set "HASH="
for /f "skip=1 tokens=*" %%h in ('certutil -hashfile requirements.txt MD5 2^>nul') do (
  if not defined HASH set "HASH=%%h"
)
set "MARCADOR=%RAIZ%\.venv\deps.marker"
set "HASH_ATUAL="
if exist "%MARCADOR%" set /p HASH_ATUAL=<"%MARCADOR%"

if "!HASH_ATUAL!"=="!HASH!" (
  echo         OK - dependencias em dia
) else (
  echo         Instalando/atualizando - pode levar alguns minutos...
  "%VPY%" -m pip install --upgrade pip --quiet --disable-pip-version-check
  "%VPY%" -m pip install -r requirements.txt --quiet --disable-pip-version-check
  if errorlevel 1 (
    echo         [ERRO] Falha ao instalar as dependencias.
    echo                Verifique a conexao com a internet e tente de novo.
    goto :FALHA
  )
  echo !HASH!>"%MARCADOR%"
  echo         OK - dependencias instaladas
)

REM ---------------------------------------------------------------- [4/6] ---
REM O Playwright precisa baixar o Chromium uma vez. Sem este passo, o sistema
REM subia e so' falhava depois, na hora de abrir o navegador do WhatsApp.
echo  [4/%PASSOS%] Verificando o navegador do Playwright...
if not exist "%RAIZ%\.venv\playwright.marker" (
  echo         Baixando o Chromium - so' na primeira vez...
  "%VPY%" -m playwright install chromium
  if errorlevel 1 (
    echo         [AVISO] Nao consegui baixar o Chromium agora.
    echo                 O painel sobe, mas o WhatsApp so' conecta depois de:
    echo                 .venv\Scripts\python.exe -m playwright install chromium
  ) else (
    echo pronto>"%RAIZ%\.venv\playwright.marker"
    echo         OK - navegador pronto
  )
) else (
  echo         OK - navegador ja instalado
)

REM ---------------------------------------------------------------- [5/6] ---
echo  [5/%PASSOS%] Iniciando os servicos...

if not exist ".env" (
  if exist ".env.example" (
    copy /y ".env.example" ".env" >nul
    echo         .env criado a partir do exemplo - revise as configuracoes.
  )
)

set "PORTA=8000"
set "DESKTOP_MODE=true"
if exist ".env" (
  for /f "usebackq tokens=1,2 delims==" %%a in (".env") do (
    if /i "%%a"=="WEB_PORT" set "PORTA=%%b"
    if /i "%%a"=="DESKTOP_MODE" set "DESKTOP_MODE=%%b"
  )
)
for /f "tokens=* delims= " %%p in ("!PORTA!") do set "PORTA=%%p"
for /f "tokens=* delims= " %%p in ("!DESKTOP_MODE!") do set "DESKTOP_MODE=%%p"

powershell -NoProfile -Command "try{(New-Object Net.Sockets.TcpClient('127.0.0.1',!PORTA!)).Dispose();exit 0}catch{exit 1}" >nul 2>nul
if not errorlevel 1 (
  echo         Ja existe um sistema rodando na porta !PORTA! - reaproveitando.
) else (
  start "Allana - Backend + Bot + Fila" cmd /k ""%VPY%" main.py"
  echo         Backend, bot do WhatsApp e fila iniciados em uma nova janela.
)

REM ---------------------------------------------------------------- [6/6] ---
echo  [6/%PASSOS%] Aguardando o sistema responder...
set "PRONTO=0"
for /L %%i in (1,1,90) do (
  if "!PRONTO!"=="0" (
    powershell -NoProfile -Command "try{$r=Invoke-WebRequest -Uri 'http://127.0.0.1:!PORTA!/api/health' -TimeoutSec 2 -UseBasicParsing;if($r.StatusCode -eq 200){exit 0}else{exit 1}}catch{exit 1}" >nul 2>nul
    if not errorlevel 1 (
      set "PRONTO=1"
    ) else (
      ping -n 2 127.0.0.1 >nul
    )
  )
)

echo.
echo  ==========================================================
if "!PRONTO!"=="1" (
  call :MOSTRAR_STATUS
) else (
  echo    [ERRO] O sistema nao respondeu em 90 segundos.
  echo.
  echo           Veja a janela "Allana - Backend + Bot + Fila" para a mensagem
  echo           de erro. Causas mais comuns:
  echo             - porta !PORTA! ocupada por outro programa
  echo             - alguma dependencia faltando
  echo             - erro de configuracao no arquivo .env
  echo  ==========================================================
  set "ERRO=1"
)

echo.
echo    Mantenha a janela do backend aberta enquanto usar o sistema.
echo    Para encerrar: feche aquela janela ou pressione Ctrl+C nela.
echo.
pause
endlocal
exit /b %ERRO%


REM ===========================================================================
REM  Le o estado real dos servicos em /api/health e imprime.
REM  Nada aqui e' declarado "[OK]" pelo script: os valores vem do sistema.
REM ===========================================================================
:MOSTRAR_STATUS
set "PS=$ProgressPreference='SilentlyContinue';"
set "PS=!PS! try { $j = Invoke-RestMethod -Uri 'http://127.0.0.1:!PORTA!/api/health' -TimeoutSec 4;"
set "PS=!PS! $s = $j.services;"
set "PS=!PS! $w = switch ($s.whatsapp) { 'connected' { 'conectado' } 'qr' { 'aguardando leitura do QR Code' } 'starting' { 'conectando...' } default { 'desconectado' } };"
set "PS=!PS! Write-Output ($w + ';' + $s.simulators_running + '/' + $s.simulators_total + ';' + $s.queue_depth) }"
set "PS=!PS! catch { Write-Output 'indisponivel;0/0;0' }"

set "WA=indisponivel"
set "SIMS=0/0"
set "FILA=0"
for /f "usebackq tokens=1,2,3 delims=;" %%a in (`powershell -NoProfile -Command "!PS!"`) do (
  set "WA=%%a"
  set "SIMS=%%b"
  set "FILA=%%c"
)

echo    [OK]   Backend / API .......... escutando na porta !PORTA!
echo    [OK]   Painel ................. http://localhost:!PORTA!
echo    [OK]   Simulador .............. !SIMS! ativo^(s^)
echo    [OK]   Fila ................... !FILA! aguardando
echo    [--]   WhatsApp ............... !WA!
echo  ==========================================================
echo.
set "ABRIR_NAVEGADOR=0"
if /i "!DESKTOP_MODE!"=="false" set "ABRIR_NAVEGADOR=1"
if /i "!DESKTOP_MODE!"=="0" set "ABRIR_NAVEGADOR=1"

if "!ABRIR_NAVEGADOR!"=="1" (
  echo    Abrindo o painel no navegador...
  start "" "http://localhost:!PORTA!"
  echo.
)

if /i "!WA!"=="conectado" (
  echo    Tudo pronto. Acompanhe o bot pela aba Monitor.
) else (
  echo    O WhatsApp ainda nao conectou. Abra a aba Status no painel e leia
  echo    o QR Code com o celular: WhatsApp ^> Aparelhos conectados.
)
goto :eof


REM ===========================================================================
:FALHA
echo.
echo  ==========================================================
echo    Nao foi possivel iniciar o sistema.
echo  ==========================================================
echo.
pause
endlocal
exit /b 1
