import time
from datetime import timedelta
import pytest
from app.clock import utc_now
from app.evolution_webhook import IncomingMessage

@pytest.fixture
def manager_with_fake_history(tmp_path, monkeypatch):
    from app.config import load_config
    from app.db import Database
    from app.events import EventHub
    from app.manager import BotManager
    
    monkeypatch.setenv("DB_PATH", str(tmp_path / "t.db"))
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "s.json"))
    monkeypatch.setenv("SIMULATOR_ENABLED", "false")
    monkeypatch.setenv("EVOLUTION_CATCHUP_ENABLED", "true")
    monkeypatch.setenv("WHATSAPP_MODE", "evolution")
    monkeypatch.setenv("EVOLUTION_GROUP_JID", "120363@g.us")
    monkeypatch.setenv("EVOLUTION_CATCHUP_MAX_AGE_HOURS", "24")
    
    config = load_config()
    db = Database(tmp_path / "t.db")
    mgr = BotManager(config, db, EventHub(db))
    
    # Reset meta
    mgr.db.execute("DELETE FROM meta WHERE key='last_evolution_sync'")
    
    class FakeEvolution:
        def __init__(self):
            self.history = []
            self.fail_next = False
            self.called = 0
            
        def find_messages(self, limit=50, page=1):
            if self.fail_next:
                self.fail_next = False
                raise Exception("Fake network error")
            self.called += 1
            # Return history paginated
            start = (page - 1) * limit
            end = start + limit
            page_items = self.history[start:end]
            return {"messages": {"records": page_items}}
            
        def start(self): pass
        def inbox(self): return mgr.whatsapp.inbox
        
    fake_whatsapp = FakeEvolution()
    # Pega o inbox verdadeiro pra nao quebrar o loop
    fake_whatsapp.inbox = mgr.whatsapp.inbox
    
    # Nao podemos trocar o whatsapp inteiro senao quebra o receber_mensagem que
    # usa manager.whatsapp.inbox, vamos apenas mockar find_messages no original.
    original_find = getattr(mgr.whatsapp, "find_messages", None)
    
    mgr.fake_history = []
    mgr.fail_next_sync = False
    
    def fake_find_messages(limit=50, page=1):
        if mgr.fail_next_sync:
            mgr.fail_next_sync = False
            raise Exception("Fake network error")
        start = (page - 1) * limit
        end = start + limit
        return {"messages": {"records": mgr.fake_history[start:end]}}
        
    monkeypatch.setattr(mgr.whatsapp, "find_messages", fake_find_messages, raising=False)
    
    return mgr

def make_record(ts, msg_id, remote_jid="120363@g.us", text="simula 123", from_me=False):
    return {
        "key": {
            "id": msg_id,
            "remoteJid": remote_jid,
            "fromMe": from_me,
            "participant": "5511999999999@s.whatsapp.net"
        },
        "messageTimestamp": ts,
        "pushName": "Consultor Teste",
        "message": {
            "conversation": text
        }
    }

def test_catchup_recupera_mensagens_perdidas(manager_with_fake_history):
    mgr = manager_with_fake_history
    agora = int(utc_now().timestamp())
    
    mgr.fake_history = [
        make_record(agora - 100, "MSG3"), # mais nova
        make_record(agora - 200, "MSG2"),
        make_record(agora - 300, "MSG1"), # mais velha
    ]
    
    mgr._sincronizar_historico_evolution()
    
    logs = mgr.db.fetchall("SELECT * FROM logs")
    for log in logs: print("LOG:", log)
    
    # As 3 devem estar no banco
    linhas = mgr.db.fetchall("SELECT wa_message_id FROM messages ORDER BY id")
    ids = [r["wa_message_id"] for r in linhas]
    assert ids == ["MSG1", "MSG2", "MSG3"], "A ordem deve ser crescente (da mais velha pra mais nova)"
    
    checkpoint = mgr.db.get_meta("last_evolution_sync")
    assert checkpoint == str(agora - 100)

def test_catchup_ignora_fora_do_limite(manager_with_fake_history):
    mgr = manager_with_fake_history
    agora = int(utc_now().timestamp())
    
    velha_demais = agora - (25 * 3600) # 25 horas
    no_limite = agora - (10 * 3600)
    
    mgr.fake_history = [
        make_record(no_limite, "BOA"),
        make_record(velha_demais, "VELHA"),
    ]
    
    mgr._sincronizar_historico_evolution()
    
    linhas = mgr.db.fetchall("SELECT wa_message_id FROM messages")
    assert len(linhas) == 1
    assert linhas[0]["wa_message_id"] == "BOA"

def test_catchup_ignora_from_me_e_outro_grupo(manager_with_fake_history):
    mgr = manager_with_fake_history
    agora = int(utc_now().timestamp())
    
    mgr.fake_history = [
        make_record(agora - 10, "MSG_BOA"),
        make_record(agora - 20, "MSG_ME", from_me=True),
        make_record(agora - 30, "MSG_OUTRO", remote_jid="OUTRO@g.us"),
    ]
    
    mgr._sincronizar_historico_evolution()
    
    linhas = mgr.db.fetchall("SELECT wa_message_id FROM messages")
    assert len(linhas) == 1
    assert linhas[0]["wa_message_id"] == "MSG_BOA"

def test_falha_nao_avanca_checkpoint(manager_with_fake_history):
    mgr = manager_with_fake_history
    agora = int(utc_now().timestamp())
    
    mgr.fake_history = [
        make_record(agora - 10, "A")
    ]
    mgr.fail_next_sync = True
    
    mgr._sincronizar_historico_evolution()
    
    # Como falhou, nao gravou no banco nem rodou as mensagens
    linhas = mgr.db.fetchall("SELECT wa_message_id FROM messages")
    assert len(linhas) == 0
    assert mgr.db.get_meta("last_evolution_sync") == ""

def test_deduplicacao_webhook_e_historico(manager_with_fake_history):
    mgr = manager_with_fake_history
    agora = int(utc_now().timestamp())
    
    # Simulamos o webhook recebendo MSG1
    msg = IncomingMessage(
        message_id="MSG1", chat_id="120363@g.us", chat_name="", sender_id="x",
        sender_name="y", text="simula", timestamp=agora-5, participant="x"
    )
    mgr.receber_mensagem(msg)
    
    # Catch-up acha MSG1 e MSG2
    mgr.fake_history = [
        make_record(agora - 2, "MSG2"),
        make_record(agora - 5, "MSG1"),
    ]
    
    mgr._sincronizar_historico_evolution()
    
    linhas = mgr.db.fetchall("SELECT wa_message_id, status FROM messages ORDER BY id")
    ids = [r["wa_message_id"] for r in linhas]
    
    assert len(ids) == 2
    # webhook inseriu MSG1 primeiro, entao MSG1 ta no banco antes. E MSG2 depois.
    assert "MSG1" in ids
    assert "MSG2" in ids
