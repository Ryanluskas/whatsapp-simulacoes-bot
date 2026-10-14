import time
import queue
import pytest
from unittest.mock import MagicMock
from app.db import Database
from app.models import Status, Stage
from app.manager import BotManager
from app.events import EventHub
from app.clock import now_iso

from tests.test_api import _config

class FakeWhatsApp:
    def __init__(self):
        self.inbox = queue.Queue()
        self.status = MagicMock()
        self.status.connected = True
    def stop(self): pass

class FakeSimulator:
    name = "FakeSim"
    def iniciar(self): pass
    def parar(self): pass

@pytest.fixture
def env(tmp_path):
    config = _config(tmp_path)
    db = Database(config.db_path)
    hub = EventHub(db)
    manager = BotManager(config, db, hub)
    manager.whatsapp = FakeWhatsApp()
    manager.simulators = [FakeSimulator()]
    return db, manager

def test_cenario_a_queued_orfao(env):
    """Cenário A - QUEUED órfão"""
    db, manager = env
    q_time = "2023-01-01T12:00:00Z" 
    sim_id = db.insert("simulations", {
        "request_id": "REQ_A", "consultant_name": "Consultor Teste",
        "chat_id": "chat1", "cpf": "111.111.111-11", "bank": "Santander",
        "status": Status.QUEUED, "stage": Stage.QUEUED, "queued_at": q_time,
        "attempts": 0, "max_attempts": 1, "created_at": now_iso(), "updated_at": now_iso()
    })
    
    resolvidas = manager.queue._resolver_orfaos()
    assert resolvidas == 1
    assert sim_id in manager.queue._queued_ids
    
    _, _, job = manager.queue._pending.get_nowait()
    assert job.simulation_id == sim_id
    assert manager.queue._resolver_orfaos() == 0

def test_cenario_b_entrada_recebida_presa(env):
    """Cenário B - ENTRADA_RECEBIDA presa"""
    db, manager = env
    e_time = now_iso()
    db.insert("messages", {
        "request_id": "REQ_B", "direction": "in", "status": Stage.RECEIVED,
        "chat_id": "chat1", "wa_message_id": "msgB", "sender_id": "sender1",
        "consultant_name": "Consultor", "text": "Fazer de 111",
        "created_at": e_time
    })
    
    manager._retomar_entradas()
    msg = manager.whatsapp.inbox.get_nowait()
    assert msg.message_id == "msgB"

def test_cenario_c_erro_durante_enfileiramento(env):
    """Cenário C - erro durante enfileiramento"""
    db, manager = env
    q_time = "2023-01-01T12:00:00Z" 
    sim_id = db.insert("simulations", {
        "request_id": "REQ_C", "status": Status.QUEUED, "stage": Stage.QUEUED,
        "queued_at": q_time, "attempts": 0, "max_attempts": 1,
        "created_at": now_iso(), "updated_at": now_iso(), "chat_id": "c1", "cpf": "11", "bank": "B", "consultant_name": "A"
    })
    resolvidas = manager.queue._resolver_orfaos()
    assert resolvidas == 1
    assert sim_id in manager.queue._queued_ids
    
def test_cenario_d_duplicidade(env):
    """Cenário D - Duplicidade (Worker e Watchdog)"""
    db, manager = env
    sim_id = db.insert("simulations", {
        "request_id": "REQ_D", "status": Status.QUEUED, "stage": Stage.QUEUED,
        "queued_at": "2023-01-01T12:00:00Z", "attempts": 0, "max_attempts": 1,
        "created_at": now_iso(), "updated_at": now_iso(), "chat_id": "c1", "cpf": "11", "bank": "B", "consultant_name": "A"
    })
    manager.queue._queued_ids.add(sim_id)
    rec = manager.queue._resolver_orfaos()
    assert rec == 0 
    
def test_cenario_e_reinicio(env):
    """Cenário E - Reinício (DB = QUEUED, memória = vazia)"""
    db, manager = env
    sim_id = db.insert("simulations", {
        "request_id": "REQ_E", "status": Status.QUEUED, "stage": Stage.QUEUED,
        "attempts": 0, "max_attempts": 1, "queued_at": now_iso(),
        "created_at": now_iso(), "updated_at": now_iso(), "chat_id": "c1", "cpf": "11", "bank": "B", "consultant_name": "A"
    })
    rec = manager.queue.recover()
    assert rec == 1
    assert sim_id in manager.queue._queued_ids
