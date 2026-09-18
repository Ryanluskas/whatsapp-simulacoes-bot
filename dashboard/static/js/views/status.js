/** Status do sistema: WhatsApp, QR Code, simuladores, Evolution e configuração. */

import { api } from "../core/api.js";
import { h, mount } from "../core/dom.js";
import * as fmt from "../core/format.js";
import { icon } from "../core/icons.js";
import * as store from "../core/store.js";
import { WHATSAPP_LABEL } from "../core/status.js";
import { card, chip, empty, statusDot, toast } from "../core/ui.js";

export function render(root) {
  const whats = card("WhatsApp");
  const conexao = card("Conexão");
  const simulador = card("Simulador");
  const evolution = card("Diagnóstico da Evolution", { hint: "sem segredos" });
  const config = card("Configuração ativa");

  mount(root,
    h("div.stack",
      h("div.grid.cols-2", whats.node, conexao.node),
      h("div.grid.cols-2", simulador.node, evolution.node),
      config.node,
    ),
  );

  let qrTimer = null;
  let relogio = null;

  function desenharWhatsApp(wa) {
    const estado = wa.state || (wa.connected ? "connected" : "disconnected");
    whats.set(
      h("div", { style: { display: "flex", alignItems: "center", gap: "14px", marginBottom: "16px" } },
        statusDot(estado, { large: true, pulse: Boolean(wa.connected) }),
        h("div",
          h("strong", { style: { fontSize: "var(--t-h2)" } }, WHATSAPP_LABEL[estado] || "Desconhecido"),
          h("div.muted", { style: { fontSize: "var(--t-meta)" } },
            wa.phone || (wa.connected ? "número não identificado" : wa.last_error || "—")),
        ),
      ),
      h("div.toolbar",
        chip("Grupo", wa.chat_name || wa.group_name || "—"),
        // Qual camada está no ar. As duas falham de jeitos bem diferentes:
        // sem isto, quem olha o painel não sabe se um "conectado" significa
        // navegador aberto ou instância da API respondendo.
        chip("Camada", wa.mode === "evolution"
          ? `Evolution${wa.instance ? ` · ${wa.instance}` : ""}`
          : "WhatsApp Web"),
        chip("Recebidas", fmt.int(wa.received)),
        chip("Enviadas", fmt.int(wa.sent)),
        wa.mode === "evolution" ? chip("ACKs de entrega", fmt.int(wa.acks_received || 0)) : null,
        wa.connected ? chip("Tempo online", fmt.uptime(wa.online_since)) : null,
        wa.last_poll ? chip("Última leitura", fmt.relative(wa.last_poll)) : null,
      ),
      h("button.btn", {
        type: "button",
        onclick: async (event) => {
          const botao = event.currentTarget;
          botao.disabled = true;
          try {
            await api.whatsappReconnect();
            toast("info", "Reconectando",
              wa.mode === "evolution" ? "A instância será reiniciada." : "O navegador do WhatsApp será reiniciado.");
          } catch (error) {
            toast("error", "Falha ao reconectar", error.message);
          } finally {
            setTimeout(() => { botao.disabled = false; }, 3000);
          }
        },
      }, icon("refresh", 15), wa.mode === "evolution" ? "Reiniciar instância" : "Reconectar WhatsApp"),
    );

    // O QR expira em segundos: só faz sentido buscar enquanto for útil
    clearInterval(qrTimer);
    if (estado === "qr" || !wa.connected) {
      carregarQr();
      qrTimer = setInterval(carregarQr, 12000);
    } else {
      conexao.setHint("");
      conexao.set(empty({
        mark: "check",
        title: "Sessão ativa",
        desc: "Não é necessário ler o QR Code. Ele reaparece aqui se a sessão cair.",
      }));
    }
  }

  async function carregarQr() {
    let dados;
    try {
      dados = await api.whatsappQr();
    } catch {
      dados = { qr: "" };
    }
    conexao.setHint(dados.qr ? "atualiza a cada 12s" : "");
    conexao.set(dados.qr
      ? h("div.qr-box",
        h("img", { src: dados.qr, alt: "QR Code para conectar o WhatsApp" }),
        h("p.muted", { style: { fontSize: "var(--t-meta)", maxWidth: "34ch" } },
          "No celular: WhatsApp → Aparelhos conectados → Conectar um aparelho."),
      )
      : empty({
        mark: "alert",
        title: "QR Code indisponível",
        desc: "O navegador do WhatsApp ainda está abrindo, ou a sessão já está ativa. "
            + "A janela do Chrome também mostra o código.",
      }));
  }

  function desenharSistema(system) {
    if (!system) return;
    const sims = system.simulators || [];
    simulador.setHint(`${system.worker_count} worker(s)`);
    simulador.set(
      h("div.checks", sims.map((sim) => h("div.check", { dataset: { tone: tomDoSimulador(sim) } },
        h("span.mark", icon(iconeDoSimulador(sim), 15)),
        h("div", h("div.cell-main", sim.name), h("div.cell-sub", rotuloDoSimulador(sim))),
        h("span.val", sim.busy ? fmt.duration(sim.busy_seconds) : ""),
      ))),
      sims.some((s) => s.last_error)
        ? h("p.muted", { style: { marginTop: "12px", fontSize: "var(--t-meta)" } },
          sims.find((s) => s.last_error).last_error)
        : null,
      h("div.toolbar", { style: { marginTop: "14px", marginBottom: 0 } },
        chip("Na fila", fmt.int(system.queue_depth)),
        chip("Em execução", fmt.int(Object.keys(system.active_jobs || {}).length)),
      ),
    );

    const wa = system.whatsapp || {};
    const diag = wa.evolution;
    if (!diag) {
      evolution.setHint("");
      evolution.set(empty({
        compact: true, mark: "plugOff",
        title: "Evolution não está em uso",
        desc: "A camada ativa é o WhatsApp Web (navegador).",
      }));
    } else {
      evolution.setHint(diag.checked_at ? `conferido ${fmt.relative(diag.checked_at)}` : "");
      evolution.set(
        h("div.checks", linhasDaEvolution(diag, wa.last_webhook_at).map(([rotulo, valor, tom]) =>
          h("div.check", { dataset: { tone: tom } },
            h("span.mark", icon(tom === "success" ? "check" : tom === "error" ? "ban" : "info", 15)),
            h("span", rotulo),
            h("span.val", valor),
          ))),
      );
    }

    config.set(h("div.kv",
      linha("Grupo monitorado", system.group_name || "conversa aberta na tela"),
      linha("Camada de WhatsApp", wa.mode === "evolution" ? "Evolution API" : "WhatsApp Web (navegador)"),
      linha("Bancos aceitos", (system.supported_banks || []).join(", ") || "—"),
      linha("Fuso horário", system.timezone),
      linha("Mascarar CPF", system.mask_cpf ? "sim" : "não"),
      linha("Caminho do simulador", h("span.mono", system.simulator_path)),
      linha("No ar desde", fmt.dateTime(system.started_at)),
    ));
  }

  async function atualizarSistema() {
    try {
      store.set("system", await api.system());
    } catch { /* a tela continua com o último estado conhecido */ }
  }

  desenharWhatsApp(store.get("whatsapp"));
  desenharSistema(store.get("system"));
  atualizarSistema();

  relogio = setInterval(() => {
    desenharWhatsApp(store.get("whatsapp"));
    atualizarSistema();
  }, 15000);

  const off = [
    store.subscribe("whatsapp", desenharWhatsApp),
    store.subscribe("system", desenharSistema),
  ];
  return () => {
    clearInterval(qrTimer);
    clearInterval(relogio);
    off.forEach((fn) => fn());
  };
}

const linha = (k, v) => h("div", h("span.k", k), h("span.v", v));

/* ----------------------------------------------------------- simulador --- */

function tomDoSimulador(sim) {
  if (!sim.running) return "error";
  if (sim.busy) return "info";
  return sim.ready ? "success" : "warning";
}

function iconeDoSimulador(sim) {
  if (!sim.running) return "ban";
  if (sim.busy) return "spinner";
  return sim.ready ? "check" : "clock";
}

function rotuloDoSimulador(sim) {
  if (!sim.running) return "parado";
  if (sim.busy) return "processando";
  return sim.ready ? "pronto e ocioso" : "carregando…";
}

/* ---------------------------------------------- diagnóstico da Evolution --- */
// Responde, em ordem, o que trava o fluxo: a API responde? a chave vale? a
// instância existe e está conectada? o webhook aponta para cá? Nenhum segredo
// aparece — só os booleanos que o servidor calcula.

const ESTADO_EVOLUTION = {
  open: "conectada", connecting: "conectando", close: "desconectada",
  unreachable: "inacessível", unauthorized: "chave recusada",
  not_found: "instância não existe", license_required: "licença não ativada",
  unknown: "desconhecido",
};

const SIM_NAO = (valor) => (valor === true
  ? ["sim", "success"]
  : valor === false ? ["NÃO", "error"] : ["não sei", "neutral"]);

function linhasDaEvolution(diag, ultimoWebhook) {
  const estado = diag.evolution_state || "unknown";
  const booleanas = [
    ["Evolution responde", diag.evolution_api_reachable],
    ["Chave aceita", diag.api_key_valid],
    ["Instância encontrada", diag.instance_found],
    ["Grupo configurado", diag.group_configured],
    ["Webhook na instância", diag.webhook_configured],
    ["Webhook aponta para o bot", diag.webhook_points_to_bot],
    ["Token do webhook", diag.webhook_token_configured],
  ].map(([rotulo, valor]) => {
    const [texto, tom] = SIM_NAO(valor);
    return [rotulo, texto, tom];
  });

  return [
    ...booleanas,
    ["Instância", `${diag.evolution_instance || "—"} · ${ESTADO_EVOLUTION[estado] || estado}`,
      estado === "open" ? "success" : estado === "connecting" ? "warning" : "error"],
    ["Último webhook recebido", ultimoWebhook ? fmt.dateTime(ultimoWebhook) : "nenhum",
      ultimoWebhook ? "success" : "neutral"],
  ];
}
