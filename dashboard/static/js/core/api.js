/** Cliente HTTP. Uma sessão expirada devolve o usuário ao login em vez de falhar em silêncio. */

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

const listeners = new Set();
export const onUnauthorized = (fn) => listeners.add(fn);

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { credentials: "same-origin", ...options });
  } catch {
    throw new ApiError("Sem conexão com o servidor.", 0);
  }

  if (response.status === 401) {
    // Uma senha errada no login também responde 401, mas não é sessão
    // expirada: avisar os ouvintes aqui derrubaria o app e limparia o campo.
    if (!path.startsWith("/api/login")) listeners.forEach((fn) => fn());
    throw new ApiError(await readError(response), 401);
  }
  if (!response.ok) {
    throw new ApiError(await readError(response), response.status);
  }
  if (response.status === 204) return null;
  return response.json();
}

async function readError(response) {
  try {
    const body = await response.json();
    return body.detail || body.message || `Erro ${response.status}`;
  } catch {
    return `Erro ${response.status}`;
  }
}

const qs = (params = {}) => {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== "" && value !== null && value !== undefined) search.set(key, value);
  }
  const text = search.toString();
  return text ? `?${text}` : "";
};

const json = (method) => (path, body) =>
  request(path, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

export const api = {
  qs,
  get: (path, params) => request(path + qs(params)),
  post: json("POST"),
  put: json("PUT"),
  del: (path) => request(path, { method: "DELETE" }),

  login: (password) =>
    request("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: `password=${encodeURIComponent(password)}`,
    }),
  logout: () => request("/api/logout", { method: "POST" }),
  session: () => request("/api/session"),

  bootstrap: () => request("/api/bootstrap"),
  metrics: () => request("/api/metrics"),
  system: () => request("/api/system"),
  queue: () => request("/api/queue"),
  monitor: (limit = 150) => request(`/api/monitor?limit=${limit}`),

  simulations: (params) => request("/api/simulations" + qs(params)),
  simulation: (id) => request(`/api/simulations/${id}`),

  consultants: (params) => request("/api/consultants" + qs(params)),
  createConsultant: (data) => json("POST")("/api/consultants", data),
  updateConsultant: (id, data) => json("PUT")(`/api/consultants/${id}`, data),
  deactivateConsultant: (id) => request(`/api/consultants/${id}`, { method: "DELETE" }),

  reports: (params) => request("/api/reports" + qs(params)),
  logs: (params) => request("/api/logs" + qs(params)),

  whatsappStatus: () => request("/api/whatsapp/status"),
  whatsappQr: () => request("/api/whatsapp/qr"),
  whatsappReconnect: () => request("/api/whatsapp/reconnect", { method: "POST" }),

  exportUrl: (params) => "/api/export" + qs(params),
};
