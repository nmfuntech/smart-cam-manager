import { fetchJson } from "./api.js";
import { createTelegramController } from "./telegram-ui.js";
import { setPillState } from "./ui.js";

const JSON_HEADERS = { "Content-Type": "application/json" };
const el = (id) => document.getElementById(id);
let selectedPerformanceProfile = null;

// Stessa mappa a 3 scenari usata storicamente dal viewer: la UI espone un
// livello 1-3, il backend riceve il valore reale.
const thresholdSteps = [
  { level: 1, value: 70, title: "Filtro alto", hint: "Scena difficile o luce variabile: riduce i falsi trigger." },
  { level: 2, value: 48, title: "Bilanciato", hint: "Uso normale in casa: buon equilibrio tra sensibilita e stabilita." },
  { level: 3, value: 30, title: "Alta sensibilita", hint: "Soggetti lenti, piccoli o lontani: rileva di piu." },
];
const minAreaSteps = [
  { level: 1, value: 12000, title: "Solo soggetti grandi", hint: "Passaggi vicini o evidenti: ignora movimenti piccoli." },
  { level: 2, value: 5000, title: "Bilanciato", hint: "Persone o animali in interno: profilo consigliato." },
  { level: 3, value: 2200, title: "Anche soggetti piccoli", hint: "Animali piccoli o soggetti lontani: rileva prima." },
];

function nearestLevel(steps, rawValue) {
  const numeric = Number(rawValue);
  if (!Number.isFinite(numeric)) return 2;
  return steps.reduce((best, step) =>
    Math.abs(step.value - numeric) < Math.abs(best.value - numeric) ? step : best
  ).level;
}

function levelToValue(steps, level) {
  const step = steps.find((s) => s.level === Math.round(Number(level)));
  return (step || steps[1]).value;
}

function updateSliderCopy(input, hintEl, valueEl, steps, unit = "") {
  if (!input) return;
  const step = steps.find((s) => s.level === Math.round(Number(input.value))) || steps[1];
  if (hintEl) hintEl.textContent = step.hint;
  if (valueEl) {
    valueEl.textContent = `Scenario ${step.level}/3 · ${step.title} · Valore reale ${step.value}${unit}`;
  }
}

// Sezioni runtime: chiave env -> { element, tipo, conversioni opzionali }.
const sections = {
  movimento: {
    save: "set-save-movimento",
    feedback: "set-feedback-movimento",
    fields: {
      MOTION_ENABLED: { id: "set-motion-enabled", type: "bool" },
      MOTION_THRESHOLD: {
        id: "set-motion-threshold",
        type: "int",
        toUi: (v) => nearestLevel(thresholdSteps, v),
        fromUi: (v) => levelToValue(thresholdSteps, v),
      },
      MOTION_MIN_AREA: {
        id: "set-motion-min-area",
        type: "int",
        toUi: (v) => nearestLevel(minAreaSteps, v),
        fromUi: (v) => levelToValue(minAreaSteps, v),
      },
      MOTION_MOG2_HISTORY: { id: "set-mog2-history", type: "int" },
      MOTION_GLOBAL_CHANGE_RATIO: { id: "set-global-change-ratio", type: "float" },
      MOTION_MORPH_KERNEL: { id: "set-morph-kernel", type: "int" },
      MOTION_BLUR_SIZE: { id: "set-blur-size", type: "int" },
    },
  },
  riconoscimento: {
    save: "set-save-riconoscimento",
    feedback: "set-feedback-riconoscimento",
    fields: {
      CLASSIFICATION_ENABLED: { id: "set-classification-enabled", type: "bool" },
      CLASSIFICATION_DETECT_PERSON: { id: "set-detect-person", type: "bool" },
      CLASSIFICATION_DETECT_PET: { id: "set-detect-pet", type: "bool" },
      CLASSIFICATION_BACKEND: { id: "set-classification-backend", type: "str" },
      CLASSIFICATION_MIN_CONFIDENCE: { id: "set-min-confidence", type: "float" },
      CLASSIFICATION_SAMPLE_POLICY: { id: "set-sample-policy", type: "str" },
    },
  },
  registrazione: {
    save: "set-save-registrazione",
    feedback: "set-feedback-registrazione",
    fields: {
      RECORD_ENABLED: { id: "set-record-enabled", type: "bool" },
      RECORD_FPS: { id: "set-record-fps", type: "float" },
      RECORD_PREROLL_SEC: { id: "set-record-preroll", type: "float" },
      RECORD_MAX_DURATION_SEC: { id: "set-record-max-duration", type: "float" },
      RECORD_MAX_WIDTH: { id: "set-record-max-width", type: "int" },
      CONTINUOUS_RECORD_ENABLED: { id: "set-continuous-enabled", type: "bool" },
      CONTINUOUS_RECORD_SEGMENT_MIN: { id: "set-continuous-segment-min", type: "float" },
      CONTINUOUS_RECORD_RETAIN_HOURS: { id: "set-continuous-retain-hours", type: "float" },
      MOTION_RETENTION_DAYS: { id: "set-retention-days", type: "float" },
      MOTION_RETENTION_MAX_MB: { id: "set-retention-max-mb", type: "float" },
    },
  },
};

function setFeedback(feedbackEl, text, isError = false) {
  if (!feedbackEl) return;
  feedbackEl.textContent = text;
  feedbackEl.style.color = isError ? "#ff89ad" : "";
}

function applyConfig(config) {
  for (const section of Object.values(sections)) {
    for (const [key, spec] of Object.entries(section.fields)) {
      const element = el(spec.id);
      const value = config[key];
      if (!element || value === undefined || value === null) continue;
      if (spec.type === "bool") {
        element.checked = Boolean(value);
      } else {
        element.value = spec.toUi ? spec.toUi(value) : value;
      }
    }
  }
  const interval = el("set-notify-min-interval");
  if (interval && config.NOTIFY_MIN_INTERVAL_SEC !== undefined) {
    interval.value = config.NOTIFY_MIN_INTERVAL_SEC;
  }
  refreshSliderCopy();
}

function refreshSliderCopy() {
  updateSliderCopy(
    el("set-motion-threshold"),
    el("set-motion-threshold-hint"),
    el("set-motion-threshold-value"),
    thresholdSteps
  );
  updateSliderCopy(
    el("set-motion-min-area"),
    el("set-motion-min-area-hint"),
    el("set-motion-min-area-value"),
    minAreaSteps,
    " px"
  );
  const confidence = el("set-min-confidence");
  const confidenceValue = el("set-min-confidence-value");
  if (confidence && confidenceValue) {
    const parsed = Number.parseFloat(confidence.value);
    confidenceValue.textContent = `Soglia minima ${(Number.isFinite(parsed) ? parsed : 0.55).toFixed(2)}`;
  }
}

function collectSectionUpdates(section) {
  const updates = {};
  for (const [key, spec] of Object.entries(section.fields)) {
    const element = el(spec.id);
    if (!element) continue;
    if (spec.type === "bool") {
      updates[key] = element.checked;
      continue;
    }
    const raw = spec.type === "int" ? parseInt(element.value, 10) : parseFloat(element.value);
    if (spec.type !== "str" && !Number.isFinite(raw)) continue;
    updates[key] = spec.type === "str" ? element.value : spec.fromUi ? spec.fromUi(raw) : raw;
  }
  return updates;
}

async function saveSection(name) {
  const section = sections[name];
  const feedbackEl = el(section.feedback);
  const button = el(section.save);
  const updates = collectSectionUpdates(section);
  if (Object.keys(updates).length === 0) {
    setFeedback(feedbackEl, "Nessun valore da salvare", true);
    return;
  }
  if (button) button.disabled = true;
  setFeedback(feedbackEl, "Salvataggio in corso...");
  try {
    const { response, data } = await fetchJson("/api/runtime_config", {
      method: "PATCH",
      headers: JSON_HEADERS,
      body: JSON.stringify({ updates }),
    });
    if (!response.ok || !data.ok) {
      setFeedback(feedbackEl, data.error || "Salvataggio fallito", true);
      return;
    }
    setFeedback(feedbackEl, "Impostazioni salvate e applicate");
    applyConfig(data.config || {});
    loadPerformanceProfiles();
  } catch {
    setFeedback(feedbackEl, "Errore di rete durante il salvataggio", true);
  } finally {
    if (button) button.disabled = false;
  }
}

// ── Profili prestazioni ──────────────────────────────────────────────────

function profileMeta(profile) {
  const req = profile.requirements;
  const cameraLabel = req.max_monitored_cameras === 1 ? "camera" : "camere";
  return `${req.min_ram_gb} GB RAM min · ${req.min_cpu_threads} thread CPU · ` +
    `${req.max_monitored_cameras} ${cameraLabel} max`;
}

function renderPerformanceProfiles(data) {
  const list = el("performance-profile-list");
  if (!list) return;
  list.replaceChildren();
  const hardware = data.hardware || {};
  const monitoredLabel = hardware.monitored_cameras === 1 ? "camera monitorata" : "camere monitorate";
  el("performance-hardware").textContent =
    `Hardware: ${hardware.ram_gb ?? "?"} GB RAM · ${hardware.cpu_threads ?? "?"} thread CPU · ` +
    `${hardware.monitored_cameras ?? 1} ${monitoredLabel}`;

  const activeText = data.active
    ? `${data.active}${data.customized ? " · personalizzato" : ""}`
    : "Nessun profilo applicato";
  setPillState(el("performance-active-pill"), activeText, data.active ? "ok" : "");

  for (const profile of data.profiles || []) {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "performance-profile-card";
    if (profile.name === data.active) card.classList.add("is-active");
    if (!profile.compatibility.compatible) card.classList.add("is-incompatible");

    const title = document.createElement("strong");
    title.textContent = profile.label;
    const badges = document.createElement("span");
    badges.className = "performance-profile-badges";
    if (profile.name === data.recommended) badges.append(makeProfileBadge("Consigliato", "ok"));
    if (profile.name === data.active) badges.append(makeProfileBadge("Attivo", "active"));
    const description = document.createElement("span");
    description.textContent = profile.description;
    const meta = document.createElement("small");
    meta.textContent = profileMeta(profile);
    card.append(title, badges, description, meta);
    if (!profile.compatibility.compatible) {
      const warning = document.createElement("small");
      warning.className = "performance-profile-warning";
      warning.textContent = profile.compatibility.reasons.join(" · ");
      card.append(warning);
    }
    card.addEventListener("click", () => previewPerformanceProfile(profile.name));
    list.append(card);
  }
}

function makeProfileBadge(text, state) {
  const badge = document.createElement("span");
  badge.className = `performance-mini-badge ${state}`;
  badge.textContent = text;
  return badge;
}

async function loadPerformanceProfiles() {
  try {
    const { response, data } = await fetchJson("/api/performance_profiles");
    if (!response.ok || !data.ok) throw new Error(data.error || "Errore profili");
    renderPerformanceProfiles(data);
  } catch {
    setFeedback(el("performance-feedback"), "Impossibile caricare i profili", true);
  }
}

function formatProfileValue(value) {
  if (value === null || value === undefined) return "non impostato";
  if (typeof value === "boolean") return value ? "attivo" : "disattivo";
  return String(value);
}

async function previewPerformanceProfile(profileName) {
  const feedback = el("performance-feedback");
  setFeedback(feedback, "Calcolo differenze...");
  try {
    const { response, data } = await fetchJson("/api/performance_profiles/preview", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ profile: profileName }),
    });
    if (!response.ok || !data.ok) {
      setFeedback(feedback, data.error || "Anteprima fallita", true);
      return;
    }
    selectedPerformanceProfile = profileName;
    el("performance-preview-title").textContent = `Anteprima · ${data.label}`;
    el("performance-change-count").textContent = `${data.changes.length} modifiche`;
    const body = el("performance-diff-body");
    body.replaceChildren();
    for (const change of data.changes) {
      const row = document.createElement("tr");
      for (const value of [
        change.key,
        formatProfileValue(change.current),
        formatProfileValue(change.recommended),
        change.requires_restart ? "Riavvio" : "Immediata",
      ]) {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.append(cell);
      }
      body.append(row);
    }
    el("performance-restart-hint").textContent = data.restart_required.length
      ? `Richiedono riavvio: ${data.restart_required.join(", ")}`
      : "Tutte le modifiche sono applicabili subito.";
    el("performance-preview").hidden = false;
    setFeedback(feedback, data.changes.length ? "Controlla le modifiche prima di applicare" : "Profilo già allineato");
  } catch {
    setFeedback(feedback, "Errore di rete durante anteprima", true);
  }
}

async function applyPerformanceProfile() {
  if (!selectedPerformanceProfile) return;
  const button = el("performance-apply");
  const feedback = el("performance-feedback");
  button.disabled = true;
  setFeedback(feedback, "Applicazione profilo...");
  try {
    const { response, data } = await fetchJson("/api/performance_profiles/apply", {
      method: "POST",
      headers: JSON_HEADERS,
      body: JSON.stringify({ profile: selectedPerformanceProfile }),
    });
    if (!response.ok || !data.ok) {
      setFeedback(feedback, data.error || "Applicazione fallita", true);
      return;
    }
    applyConfig(data.config || {});
    el("performance-preview").hidden = true;
    selectedPerformanceProfile = null;
    const suffix = data.restart_required.length || !data.live_apply_ok
      ? " Riavvia il servizio per completare."
      : "";
    setFeedback(feedback, `Profilo applicato.${suffix}`);
    loadPerformanceProfiles();
  } catch {
    setFeedback(feedback, "Errore di rete durante applicazione", true);
  } finally {
    button.disabled = false;
  }
}

function bindPerformanceProfiles() {
  el("performance-apply")?.addEventListener("click", applyPerformanceProfile);
  el("performance-cancel")?.addEventListener("click", () => {
    selectedPerformanceProfile = null;
    el("performance-preview").hidden = true;
    setFeedback(el("performance-feedback"), "");
  });
}

async function loadRuntimeConfig() {
  try {
    const { data } = await fetchJson("/runtime_config");
    applyConfig(data.config || {});
  } catch {
    setFeedback(el("set-feedback-movimento"), "Impossibile leggere la configurazione", true);
  }
}

// ── Agente ────────────────────────────────────────────────────────────────

async function loadAgentStatus() {
  const pill = el("set-agent-pill");
  const toggle = el("set-agent-toggle");
  try {
    const { data } = await fetchJson("/api/agente/status");
    const enabled = Boolean(data.enabled);
    setPillState(pill, enabled ? "Agente: attivo" : "Agente: spento", enabled ? "ok" : "");
    if (toggle) toggle.checked = enabled;
  } catch {
    setPillState(pill, "Agente: errore", "error");
  }
}

async function toggleAgent(event) {
  const feedbackEl = el("set-feedback-agente");
  try {
    const { response, data } = await fetchJson("/api/agente/toggle", {
      method: "PATCH",
      headers: JSON_HEADERS,
      body: JSON.stringify({ enabled: event.target.checked }),
    });
    if (!response.ok || !data.ok) {
      setFeedback(feedbackEl, data.error || "Aggiornamento fallito", true);
    } else {
      setFeedback(feedbackEl, data.enabled ? "Agente attivato" : "Agente disattivato");
    }
  } catch {
    setFeedback(feedbackEl, "Errore di rete", true);
  } finally {
    loadAgentStatus();
  }
}

// ── Sistema ───────────────────────────────────────────────────────────────

async function loadSystemInfo() {
  const stream = el("set-stream-status");
  if (stream) {
    try {
      const { data } = await fetchJson("/stream_status");
      stream.textContent = data.connected ? "connesso" : data.error || "disconnesso";
    } catch {
      stream.textContent = "errore";
    }
  }
  const disk = el("set-disk-estimate");
  if (disk) {
    try {
      const { data } = await fetchJson("/api/disk_estimate?retain_hours=24");
      disk.textContent = data.ok ? `~${Math.round(data.estimated_mb)} MB` : "-";
    } catch {
      disk.textContent = "-";
    }
  }
}

function bindSystemActions() {
  el("set-open-folder")?.addEventListener("click", async () => {
    const feedbackEl = el("set-feedback-sistema");
    try {
      const { data } = await fetchJson("/api/open_captures_folder", { method: "POST" });
      setFeedback(feedbackEl, data.ok ? "Cartella aperta sul server" : data.error || "Errore", !data.ok);
    } catch {
      setFeedback(feedbackEl, "Errore di rete", true);
    }
  });
  el("set-clear-events")?.addEventListener("click", async () => {
    if (!window.confirm("Cancellare TUTTI gli eventi di movimento salvati?")) return;
    const feedbackEl = el("set-feedback-sistema");
    try {
      const { data } = await fetchJson("/api/motion_captures", { method: "DELETE" });
      setFeedback(feedbackEl, data.ok ? `Rimossi ${data.removed} elementi` : data.error || "Errore", !data.ok);
    } catch {
      setFeedback(feedbackEl, "Errore di rete", true);
    }
  });
}

// ── Telegram (riusa il controller del viewer, in versione sezione) ────────

async function saveNotifyInterval() {
  const input = el("set-notify-min-interval");
  const parsed = parseFloat(input?.value ?? "");
  if (!Number.isFinite(parsed)) return;
  try {
    await fetchJson("/api/runtime_config", {
      method: "PATCH",
      headers: JSON_HEADERS,
      body: JSON.stringify({ updates: { NOTIFY_MIN_INTERVAL_SEC: parsed } }),
    });
  } catch {
    // il feedback principale resta quello del controller Telegram
  }
}

function initTelegram() {
  const controller = createTelegramController({
    tokenInput: el("set-tg-token"),
    tokenHint: el("set-tg-token-hint"),
    chatIdInput: el("set-tg-chat-id"),
    discoverButton: el("set-tg-discover"),
    chatList: el("set-tg-chat-list"),
    preferVideo: el("set-tg-prefer-video"),
    enabled: el("set-tg-enabled"),
    testButton: el("set-tg-test"),
    saveButton: el("set-tg-save"),
    feedback: el("set-tg-feedback"),
    inviteCodeInput: el("set-tg-invite-code"),
    inviteHint: el("set-tg-invite-hint"),
    inviteLinkBox: el("set-tg-invite-link-box"),
    inviteLinkText: el("set-tg-invite-link-text"),
    inviteCopyButton: el("set-tg-invite-copy"),
  });
  controller.bind();
  controller.load();
  // L'intervallo minimo è una chiave runtime, non fa parte del POST
  // telegram_config: si salva in parallelo sullo stesso pulsante.
  el("set-tg-save")?.addEventListener("click", saveNotifyInterval);
}

// ── Bootstrap ─────────────────────────────────────────────────────────────

function loadAll() {
  loadRuntimeConfig();
  loadPerformanceProfiles();
  loadAgentStatus();
  loadSystemInfo();
}

for (const name of Object.keys(sections)) {
  el(sections[name].save)?.addEventListener("click", () => saveSection(name));
}
for (const id of ["set-motion-threshold", "set-motion-min-area", "set-min-confidence"]) {
  el(id)?.addEventListener("input", refreshSliderCopy);
}
el("set-agent-toggle")?.addEventListener("change", toggleAgent);
bindSystemActions();
bindPerformanceProfiles();
initTelegram();
loadAll();

// Niente polling: la pagina si aggiorna solo quando torna visibile.
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) loadAll();
});
