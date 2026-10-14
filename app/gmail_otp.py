import imaplib
import email
import re
import time
from email.header import decode_header
from typing import Optional
import logging

logger = logging.getLogger(__name__)

def extrair_codigo_do_corpo(texto: str) -> Optional[str]:
    # Exemplo: "Seu token de validação de acesso ao é: 823765 ESTAMOS CONECTADOS"
    # Procuramos exatamente 6 dígitos soltos após as palavras
    match = re.search(r'(?:token|c[óo]digo).{0,40}?\b(\d{6})\b', texto, re.IGNORECASE)
    if match:
        return match.group(1)
    
    # Fallback genérico: primeiro bloco de 6 dígitos
    match2 = re.search(r'\b(\d{6})\b', texto)
    if match2:
        return match2.group(1)
        
    return None

def buscar_codigo_santander(usuario: str, senha_app: str, timeout_sec: int = 60) -> Optional[str]:
    """
    Fica em polling no Gmail por até `timeout_sec` procurando um e-mail 
    não lido do Santander (santander@santander.com.br) para extrair o OTP.
    """
    if not usuario or not senha_app:
        logger.error("Credenciais do Gmail não configuradas (GMAIL_USER ou GMAIL_APP_PASSWORD).")
        return None

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(usuario, senha_app)
    except Exception as e:
        logger.error(f"Falha ao conectar no Gmail: {e}")
        return None

    start_time = time.monotonic()
    
    try:
        while time.monotonic() - start_time < timeout_sec:
            mail.select("inbox")
            
            # Busca e-mails não lidos de remetente santander
            status, messages = mail.search(None, '(UNSEEN FROM "santander")')
            if status == "OK" and messages[0]:
                for num in reversed(messages[0].split()):
                    typ, msg_data = mail.fetch(num, "(RFC822)")
                    for response_part in msg_data:
                        if isinstance(response_part, tuple):
                            msg = email.message_from_bytes(response_part[1])
                            
                            corpo = ""
                            if msg.is_multipart():
                                for part in msg.walk():
                                    if part.get_content_type() in ["text/plain", "text/html"]:
                                        try:
                                            corpo += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                                        except Exception:
                                            pass
                            else:
                                try:
                                    corpo = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
                                except Exception:
                                    pass
                                    
                            codigo = extrair_codigo_do_corpo(corpo)
                            if codigo:
                                # Marca o e-mail como lido
                                mail.store(num, '+FLAGS', '\\Seen')
                                return codigo
            
            time.sleep(3)
    except Exception as e:
        logger.error(f"Erro durante a busca IMAP: {e}")
    finally:
        try:
            mail.close()
            mail.logout()
        except Exception:
            pass
            
    return None
