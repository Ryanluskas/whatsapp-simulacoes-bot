# =============================================================================
#  ALLANA — painel + bot do WhatsApp
#
#  O QUE ENTRA AQUI: painel web, bot do WhatsApp, fila e banco.
#  O QUE NÃO ENTRA: a automação do Santander. Ela continua no Windows, ao lado
#  do Brave, porque depende de navegador real, perfil logado e de você por
#  perto no relogin/OTP — ver `agente.py` e SIMULATOR_MODE=remote.
#
#  A imagem parte da base oficial do Playwright: ela já traz o Chromium e as
#  dezenas de bibliotecas de sistema que ele exige (fontes, libnss, libatk...).
#  Montar isso à mão sobre python:slim custa mais e quebra a cada atualização.
# =============================================================================
FROM mcr.microsoft.com/playwright/python:v1.62.0-noble

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/Sao_Paulo

WORKDIR /app

# As dependências primeiro, sozinhas: assim o cache de camada só é invalidado
# quando o requirements muda, e não a cada alteração de código.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt  && python -c "from importlib.metadata import version; v = version('playwright'); esperado = '1.62.0'; assert v == esperado, f'playwright {v} nao casa com a imagem base {esperado} — os navegadores da imagem ficam em outro caminho'; print(f'playwright {v} casa com a imagem base')" 

COPY app/ ./app/
COPY dashboard/ ./dashboard/
COPY main.py agente.py ./

# Usuário sem privilégios. A base do Playwright já traz "pwuser".
RUN mkdir -p /app/dados /app/comprovantes /app/logs /app/.whatsapp-profile \
    && chown -R pwuser:pwuser /app
USER pwuser

# Padrões do container; o docker-compose sobrescreve o que for necessário.
ENV WEB_HOST=0.0.0.0 \
    WEB_PORT=8000 \
    SIMULATOR_MODE=remote \
    WHATSAPP_HEADLESS=true \
    DB_PATH=/app/dados/simulacoes.db \
    STATE_PATH=/app/dados/state.json \
    WHATSAPP_PROFILE_DIR=/app/.whatsapp-profile

EXPOSE 8000

# O healthcheck usa a mesma rota que o iniciar.bat consulta.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status==200 else 1)"

CMD ["python", "main.py"]
