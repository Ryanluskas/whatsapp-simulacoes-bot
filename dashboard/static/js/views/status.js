/** Status do sistema: conexão do WhatsApp, QR Code, simuladores e configuração ativa. */

import { api } from "../core/api.js";
import { h, mount } from "../core/dom.js";
import * as fmt from "../core/format.js";
import * as store from "../core/store.js";
import { chip, empty, statusDot, toast } from "../core/ui.js";
import { stateLabel } from "./monitor.js";

export function render(root) {
  const waCard = h("div.card");
  const qrCard = h("div.card");
  const simCard = h("div.card");
  const configCard = h("div.card");

  mount(root,
    h("div.section-head", h("h3", "Status do sistema")),
    h("div.grid.cols-2", waCard, qrCard),
    h("div.grid.cols-2", { style: { marginTop: "16px" } }, simCard, configCard),
  );

  let qrTimer = null;
  let uptimeTimer = null;

  function drawWhatsApp(wa) {
    const connected = wa.connected;
    mount(waCard,
      h("header", h("h3", "WhatsApp")),
      h("div", { style: { display: "flex", alignItems: "center", gap: "14px", margin: "6px 0 18px" } },
        statusDot(wa.state, { large: true, pulse: connected }),
        h("div",
          h("strong", { style: { fontSize: "20px" } }, stateLabel(wa.state)),
          h("div.muted", { style: { fontSize: "12px" } },
            wa.phone || (connected ? "número não identificado" : wa.last_error || "—")),
        ),
      ),
      h("div", { style: { display: "flex", gap: "8px", flexWrap: "wrap", marginBottom: "18px" } },
        chip("Grupo", wa.chat_name || wa.group_name || "—"),
        // Qual camada esta no ar. As duas falham de jeitos bem diferentes:
        // sem isto, quem olha o painel nao sabe se um "conectado" significa
        // navegador aberto ou instancia da API respondendo.
        chip("Camada", wa.mode === "evolution"
          ? `Evolution${wa.instance ? " · " + wa.instance : ""}`
          : "WhatsApp Web"),
        chip("Recebidas", fmt.int(wa.received)),
        chip("Enviadas", fmt.int(wa.sent)),
        connected ? chip("Tempo online", fmt.uptime(wa.online_since)) : null,
        wa.last_poll ? chip("Última leitura", fmt.relative(wa.last_poll)) : null,
      ),
      h("button.btn", {
        type: "button",
        onclick: async (event) => {
          event.currentTarget.disabled = true;
          try {
            await api.whatsappReconnect();
            toast("info", "Reconectando", "O navegador do WhatsApp será reiniciado.");
          } catch (error) {
            toast("error", "Falha ao reconectar", error.message);
          } finally {
            setTimeout(() => { event.currentTarget.disabled = false; }, 3000);
          }
        },
      }, wa.mode === "evolution" ? "Reiniciar instância" : "Reconectar WhatsApp"),
    );

    // O QR expira em segundos: só faz sentido buscar enquanto estiver na tela dele
    clearInterval(qrTimer);
    if (wa.state === "qr" || !connected) {
      loadQr();
      qrTimer = setInterval(loadQr, 12000);
    } else {
      mount(qrCard,
        h("header", h("h3", "Conexão")),
        empty({
          mark: "check",
          title: "Sessão ativa",
          desc: "Não é necessário ler o QR Code. Ele reaparece aqui se a sessão cair.",
        }),
      );
    }
  }

  async function loadQr() {
    let data;
    try {
      data = await api.whatsappQr();
    } catch {
      data = { qr: "" };
    }
    mount(qrCard,
      h("header", h("h3", "Conectar por QR Code"), h("span.hint", "atualiza a cada 12s")),
      data.qr
        ? h("div.qr-box",
          h("img", { src: data.qr, alt: "QR Code para conectar o WhatsApp" }),
          h("p.muted", { style: { fontSize: "12px", maxWidth: "34ch" } },
            "No celular: WhatsApp → Aparelhos conectados → Conectar um aparelho."),
        )
        : empty({
          mark: "alert",
          title: "QR Code indisponível",
          desc: "O navegador do WhatsApp ainda está abrindo, ou a sessão já está ativa. "
              + "A janela do Chrome também mostra o código.",
        }),
    );
  }

  function drawSystem(system) {
    if (!system) return;
    mount(simCard,
      h("header", h("h3", "Simulador"), h("span.hint", `${system.worker_count} worker(s)`)),
      h("div.grid", { style: { gap: "12px" } },
        (system.simulators || []).map((sim) => h("div",
          { style: { display: "flex", alignItems: "center", gap: "10px" } },
          statusDot(simStateOf(sim), { pulse: sim.busy }),
          h("div", { style: { flex: 1 } },
            h("div", { style: { fontSize: "13px" } }, sim.name),
            h("div.muted", { style: { fontSize: "12px" } }, simLabel(sim)),
          ),
        )),
      ),
      (system.simulators || []).some((s) => s.last_error)
        ? h("div", {
          style: {
            marginTop: "14px", padding: "10px 12px", borderRadius: "6px",
            background: "var(--st-error-bg)", fontSize: "12px",
          },
        }, system.simulators.find((s) => s.last_error).last_error)
        : null,
      h("div", { style: { marginTop: "16px", display: "flex", gap: "8px", flexWrap: "wrap" } },
        chip("Na fila", fmt.int(system.queue_depth)),
        chip("Em execução", fmt.int(Object.keys(system.active_jobs || {}).length)),
      ),
    );

    mount(configCard,
      h("header", h("h3", "Configuração ativa")),
      h("div.kv",
        row("Grupo monitorado", system.group_name || "conversa aberta na tela"),
        row("Camada de WhatsApp", (system.whatsapp || {}).mode === "evolution"
          ? "Evolution API" : "WhatsApp Web (navegador)"),
        row("Bancos aceitos", (system.supported_banks || []).join(", ") || "—"),
        row("Fuso horário", system.timezone),
        row("Mascarar CPF", system.mask_cpf ? "sim" : "não"),
        row("Caminho do simulador", h("span.mono", { style: { fontSize: "12px" } }, system.simulator_path)),
        row("Sistema no ar desde", fmt.dateTime(system.started_at)),
        ...evolutionRows((system.whatsapp || {}).evolution, (system.whatsapp || {}).last_webhook_at),
      ),
    );
  }

  async function refreshSystem() {
    try {
      store.set("system", await api.system());
    } catch { /* a tela continua com o último estado conhecido */ }
  }

  drawWhatsApp(store.get("whatsapp"));
  drawSystem(store.get("system"));
  refreshSystem();

  uptimeTimer = setInterval(() => {
    drawWhatsApp(store.get("whatsapp"));
    refreshSystem();
  }, 15000);

  const off = [
    store.subscribe("whatsapp", drawWhatsApp),
    store.subscribe("system", drawSystem),
  ];
  return () => {
    clearInterval(qrTimer);
    clearInterval(uptimeTimer);
    off.forEach((fn) => fn());
  };
}

const row = (label, value) => h("div", h("span.k", label), h("span.v", value));

// --- diagnóstico da Evolution -------------------------------------------
// Responde, em ordem, o que trava o fluxo: a API responde? a chave vale? a
// instância existe e está conectada? o webhook aponta para cá? Nenhum segredo
// aparece — só os booleanos que o servidor calcula.
const SIM_NAO = (valor) => (valor === true ? "sim" : valor === false ? "NÃO" : "não sei");

const ESTADO_EVOLUTION = {
  open: "conectada", connecting: "conectando", close: "desconectada",
  unreachable: "inacessível", unauthorized: "chave recusada",
  not_found: "instância não existe", license_required: "licença não ativada",
  unknown: "desconhecido",
};

function evolutionRows(diagnostico, ultimoWebhook) {
  if (!diagnostico) return [];
  const estado = diagnostico.evolution_state || "unknown";
  return [
    row("Evolution responde", SIM_NAO(diagnostico.evolution_api_reachable)),
    row("Chave aceita", SIM_NAO(diagnostico.api_key_valid)),
    row("Instância encontrada", SIM_NAO(diagnostico.instance_found)),
    row("Instância", `${diagnostico.evolution_instance || "—"} · ${ESTADO_EVOLUTION[estado] || estado}`),
    row("Grupo configurado", SIM_NAO(diagnostico.group_configured)),
    row("Webhook na instância", SIM_NAO(diagnostico.webhook_configured)),
    row("Webhook aponta para o bot", SIM_NAO(diagnostico.webhook_points_to_bot)),
    row("Token do webhook", SIM_NAO(diagnostico.webhook_token_configured)),
    row("Último webhook recebido", ultimoWebhook ? fmt.dateTime(ultimoWebhook) : "nenhum"),
    row("Diagnóstico atualizado em", diagnostico.checked_at ? fmt.dateTime(diagnostico.checked_at) : "—"),
  ];
}

function simStateOf(sim) {
  if (!sim.running) return "error";
  if (sim.busy) return "processing";
  return sim.ready ? "connected" : "queued";
}

function simLabel(sim) {
  if (!sim.running) return "parado";
  if (sim.busy) return `processando há ${fmt.duration(sim.busy_seconds)}`;
  return sim.ready ? "pronto e ocioso" : "carregando…";
}
