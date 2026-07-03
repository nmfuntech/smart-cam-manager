import { fetchJson } from "./api.js";
import { setPillState } from "./ui.js";

const JSON_HEADERS = { "Content-Type": "application/json" };

const statusPill = document.getElementById("agent-status-pill");
const toggleCheckbox = document.getElementById("agent-toggle");
const feedback = document.getElementById("agent-feedback");
const chatEmpty = document.getElementById("agent-chat-empty");
const chatLog = document.getElementById("agent-chat-log");
const form = document.getElementById("agent-form");
const input = document.getElementById("agent-input");
const sendBtn = document.getElementById("btn-agent-send");
const newChatBtn = document.getElementById("agent-new-chat");

const STATUS_REFRESH_MS = 30000;

const timeFormatter = new Intl.DateTimeFormat("it-IT", { hour: "2-digit", minute: "2-digit" });
const dateFormatter = new Intl.DateTimeFormat("it-IT", { day: "2-digit", month: "2-digit" });

function formatTimestamp(tsSeconds) {
  if (!Number.isFinite(tsSeconds)) {
    return "";
  }
  const date = new Date(tsSeconds * 1000);
  const now = new Date();
  const sameDay = date.toDateString() === now.toDateString();
  return sameDay
    ? timeFormatter.format(date)
    : `${dateFormatter.format(date)} ${timeFormatter.format(date)}`;
}

let pendingId = null;
let confirmRow = null;
let agentEnabled = true;
let busy = false;

function setFeedback(text, isError = false) {
  feedback.textContent = text || "";
  feedback.classList.toggle("error", Boolean(isError));
}

function scrollChatToBottom() {
  chatLog.scrollTop = chatLog.scrollHeight;
}

function updateEmptyState() {
  if (!chatEmpty) {
    return;
  }
  chatEmpty.hidden = chatLog.childElementCount > 0;
}

function appendBubble(text, role, { isError = false, ts = null } = {}) {
  if (chatEmpty) {
    chatEmpty.hidden = true;
  }

  const row = document.createElement("div");
  row.className = "agent-chat-row";
  row.dataset.role = role;

  const bubble = document.createElement("div");
  bubble.className = `agent-bubble agent-bubble-${role}${isError ? " agent-bubble-error" : ""}`;

  const label = document.createElement("p");
  label.className = "agent-bubble-label";
  label.textContent = role === "user" ? "Tu" : "Agente";

  const stamp = formatTimestamp(ts ?? Date.now() / 1000);
  if (stamp) {
    const timeEl = document.createElement("time");
    timeEl.className = "agent-bubble-time";
    timeEl.textContent = stamp;
    label.append(" · ", timeEl);
  }

  const body = document.createElement("p");
  body.className = "agent-bubble-text";
  body.textContent = text;

  bubble.append(label, body);
  row.append(bubble);
  chatLog.append(row);
  scrollChatToBottom();
  return row;
}

async function loadHistory() {
  try {
    const { response, data } = await fetchJson("/api/agente/history?limit=100");
    if (!response.ok || !data.ok) {
      return;
    }
    chatLog.innerHTML = "";
    for (const message of data.messages || []) {
      appendBubble(message.text, message.role === "user" ? "user" : "agent", {
        isError: message.kind === "error",
        ts: message.ts,
      });
    }
  } catch {
    // history assente: si parte dalla chat vuota
  } finally {
    updateEmptyState();
    scrollChatToBottom();
  }
}

async function clearHistory() {
  if (!window.confirm("Svuotare la conversazione? La cronologia verrà eliminata.")) {
    return;
  }
  await cancelPendingIfAny({ silent: true });
  try {
    const { response, data } = await fetchJson("/api/agente/history", { method: "DELETE" });
    if (!response.ok || !data.ok) {
      setFeedback(data.error || "Impossibile svuotare la chat", true);
      return;
    }
    chatLog.innerHTML = "";
    updateEmptyState();
    setFeedback("");
  } catch {
    setFeedback("Errore di rete", true);
  }
}

function removeConfirmRow() {
  if (confirmRow) {
    confirmRow.remove();
    confirmRow = null;
  }
  pendingId = null;
}

async function cancelPendingIfAny({ silent = true } = {}) {
  if (!pendingId) {
    return;
  }

  const id = pendingId;
  removeConfirmRow();
  try {
    await fetchJson("/api/agente/cancel", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ pending_id: id }),
    });
  } catch {
    // ignore — UI già aggiornata
  }
  if (!silent) {
    appendBubble("Annullato.", "agent");
  }
  updateEmptyState();
}

function showInlineConfirm(pendingIdValue, description) {
  removeConfirmRow();
  pendingId = pendingIdValue;
  if (chatEmpty) {
    chatEmpty.hidden = true;
  }

  confirmRow = document.createElement("div");
  confirmRow.className = "agent-chat-row agent-confirm-row";
  confirmRow.dataset.role = "agent";

  const bubble = document.createElement("div");
  bubble.className = "agent-bubble agent-bubble-agent agent-bubble-confirm";

  const label = document.createElement("p");
  label.className = "agent-bubble-label";
  label.textContent = "Agente";

  const body = document.createElement("p");
  body.className = "agent-bubble-text";
  body.textContent = `Ho capito: ${description || "comando"} — confermi?`;

  const actions = document.createElement("div");
  actions.className = "agent-confirm-actions";

  const confirmBtn = document.createElement("button");
  confirmBtn.className = "ghost-button compact-button";
  confirmBtn.type = "button";
  confirmBtn.textContent = "Conferma";
  confirmBtn.addEventListener("click", confirmPending);

  const cancelBtn = document.createElement("button");
  cancelBtn.className = "ghost-button compact-button";
  cancelBtn.type = "button";
  cancelBtn.textContent = "Annulla";
  cancelBtn.addEventListener("click", () => cancelPendingIfAny({ silent: false }));

  actions.append(confirmBtn, cancelBtn);
  bubble.append(label, body, actions);
  confirmRow.append(bubble);
  chatLog.append(confirmRow);
  scrollChatToBottom();
}

function setComposerEnabled() {
  const allow = agentEnabled && !busy;
  input.disabled = !allow;
  sendBtn.disabled = !allow;
  if (!agentEnabled) {
    input.placeholder = "Assistente disabilitato — riattivalo dal toggle in alto.";
  } else if (busy) {
    input.placeholder = "Interpretazione in corso...";
  } else {
    input.placeholder = "es. spegni il rilevamento movimento";
  }
}

function setBusy(isBusy) {
  busy = isBusy;
  setComposerEnabled();
}

async function loadStatus() {
  try {
    const { response, data } = await fetchJson("/api/agente/status");
    if (!response.ok || !data.ok) {
      return;
    }
    renderStatus(data.enabled);
  } catch {
    // ignore — non-critical poll
  }
}

function renderStatus(enabled) {
  agentEnabled = Boolean(enabled);
  setPillState(statusPill, enabled ? "Attivo" : "Disabilitato", enabled ? "ok" : "");
  toggleCheckbox.checked = agentEnabled;
  setComposerEnabled();
  if (!agentEnabled) {
    cancelPendingIfAny({ silent: true });
  }
}

async function setEnabled(enabled) {
  toggleCheckbox.disabled = true;
  setFeedback(enabled ? "Abilitazione..." : "Disabilitazione...");
  try {
    const { response, data } = await fetchJson("/api/agente/toggle", {
      method: "PATCH",
      headers: JSON_HEADERS,
      body: JSON.stringify({ enabled }),
    });
    if (!response.ok || !data.ok) {
      setFeedback(data.error || "Errore toggle", true);
      toggleCheckbox.checked = !enabled;
      return;
    }
    renderStatus(data.enabled);
    setFeedback("");
  } catch {
    setFeedback("Errore di rete durante il toggle", true);
    toggleCheckbox.checked = !enabled;
  } finally {
    toggleCheckbox.disabled = false;
  }
}

async function sendMessage(text) {
  if (!agentEnabled || busy) {
    return;
  }

  await cancelPendingIfAny({ silent: true });
  appendBubble(text, "user");
  setBusy(true);
  setFeedback("Interpretazione in corso...");
  try {
    const { response, data } = await fetchJson("/api/agente/interpret", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ text }),
    });
    if (!response.ok || !data.ok) {
      appendBubble(data.error || "Non ho capito.", "agent", { isError: true });
      return;
    }
    if (data.executed) {
      appendBubble(data.result_text || `Eseguito: ${data.command}`, "agent");
      return;
    }
    showInlineConfirm(data.pending_id, data.description);
  } catch {
    setFeedback("Errore di rete", true);
  } finally {
    setBusy(false);
    setFeedback("");
    updateEmptyState();
  }
}

async function confirmPending() {
  if (!pendingId || busy) {
    return;
  }

  const id = pendingId;
  removeConfirmRow();
  setBusy(true);
  setFeedback("Esecuzione in corso...");
  try {
    const { response, data } = await fetchJson("/api/agente/confirm", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ pending_id: id }),
    });
    if (!response.ok || !data.ok) {
      appendBubble(data.error || "Esecuzione fallita.", "agent", { isError: true });
      return;
    }
    appendBubble(data.result_text || `Eseguito: ${data.command}`, "agent");
  } catch {
    setFeedback("Errore di rete", true);
  } finally {
    setBusy(false);
    setFeedback("");
    updateEmptyState();
  }
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  const text = input.value.trim();
  if (!text) {
    return;
  }
  input.value = "";
  sendMessage(text);
});

toggleCheckbox.addEventListener("change", () => setEnabled(toggleCheckbox.checked));

newChatBtn?.addEventListener("click", clearHistory);

document.querySelectorAll(".agent-suggestion").forEach((button) => {
  button.addEventListener("click", () => {
    const text = String(button.dataset.text || "").trim();
    if (!text) {
      return;
    }
    sendMessage(text);
  });
});

loadStatus();
loadHistory();

// La pill di stato prima era caricata una volta sola e restava stantia se
// l'agente veniva attivato/spento altrove (es. da /impostazioni o Telegram).
setInterval(loadStatus, STATUS_REFRESH_MS);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    loadStatus();
  }
});
