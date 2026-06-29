import { fetchJson } from "./api.js";
import { setPillState } from "./ui.js";

const JSON_HEADERS = { "Content-Type": "application/json" };

export function createAutomationController(elements) {
  const {
    feedback,
    statusPill,
    toggleCheckbox,
    tabButtons,
    panelDevices,
    panelRules,
    deviceList,
    ruleList,
    addDeviceBtn,
    addRuleBtn,
    deviceDialog,
    deviceDialogTitle,
    deviceFeedback,
    saveDeviceBtn,
    closeDeviceBtns,
    ruleDialog,
    ruleDialogTitle,
    ruleFeedback,
    saveRuleBtn,
    closeRuleBtns,
    addActionBtn,
    ruleActionsList,
  } = elements;

  // ── State ──────────────────────────────────────────────────
  let devices = [];
  let editingDevice = null; // null = new device
  let editingRule = null; // null = new rule

  // ── Feedback helpers ───────────────────────────────────────
  function setFeedback(text, isError = false) {
    feedback.textContent = text;
    feedback.style.color = isError ? "#ff89ad" : "";
  }

  function setDeviceFeedback(text, isError = false) {
    deviceFeedback.textContent = text;
    deviceFeedback.style.color = isError ? "#ff89ad" : "";
  }

  function setRuleFeedback(text, isError = false) {
    ruleFeedback.textContent = text;
    ruleFeedback.style.color = isError ? "#ff89ad" : "";
  }

  // ── Tabs ───────────────────────────────────────────────────
  function showTab(tab) {
    panelDevices.hidden = tab !== "devices";
    panelRules.hidden = tab !== "rules";
    tabButtons.forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.tab === tab);
    });
  }

  // ── Status ─────────────────────────────────────────────────
  async function loadStatus() {
    try {
      const { response, data } = await fetchJson("/api/automazione/status");
      if (!response.ok || !data.ok) return;
      renderStatus(data);
    } catch {
      // ignore — non-critical poll
    }
  }

  function renderStatus({ enabled, active, rule_count, device_count }) {
    const text = active
      ? `Automazione: attiva · ${rule_count} regole · ${device_count} device`
      : enabled
        ? "Automazione: abilitata (engine non attivo)"
        : "Automazione: disabilitata";
    setPillState(statusPill, text, active ? "ok" : "");
    toggleCheckbox.checked = enabled;
  }

  // ── Toggle enable/disable ──────────────────────────────────
  async function setEnabled(enabled) {
    toggleCheckbox.disabled = true;
    setFeedback(enabled ? "Abilitazione..." : "Disabilitazione...");
    try {
      const { response, data } = await fetchJson("/api/automazione/toggle", {
        method: "PATCH",
        headers: JSON_HEADERS,
        body: JSON.stringify({ enabled }),
      });
      if (!response.ok || !data.ok) {
        setFeedback(data.error || "Errore toggle", true);
        toggleCheckbox.checked = !enabled;
        return;
      }
      renderStatus(data);
      setFeedback(enabled ? "Automazione abilitata" : "Automazione disabilitata");
    } catch {
      setFeedback("Errore di rete", true);
      toggleCheckbox.checked = !enabled;
    } finally {
      toggleCheckbox.disabled = false;
    }
  }

  // ── Devices ────────────────────────────────────────────────
  async function loadDevices() {
    try {
      const { response, data } = await fetchJson("/api/automazione/devices");
      if (!response.ok || !data.ok) return;
      devices = data.devices || [];
      renderDeviceList(devices);
    } catch {
      // ignore
    }
  }

  function renderDeviceList(list) {
    deviceList.innerHTML = "";
    const empty = document.getElementById("device-empty");
    if (empty) empty.style.display = list.length === 0 ? "" : "none";

    list.forEach((dev) => {
      const card = document.createElement("div");
      card.className = "camera-profile-card";
      card.innerHTML = `
        <div class="camera-profile-info">
          <strong>${escHtml(dev.name)}</strong>
          <span class="camera-profile-meta">
            ${escHtml(dev.driver)} · ${escHtml(dev.ip || "—")} · DP ${dev.switch_dp ?? 1}
          </span>
          <span class="camera-profile-meta">
            ID: ${escHtml(dev.device_id || "—")} · v${dev.version ?? 3.3}
            · local_key: ${dev.local_key === "***" ? "✓ configurata" : "—"}
          </span>
        </div>
        <div class="camera-profile-actions">
          <button class="ghost-button compact-button btn-edit-device" data-name="${escHtml(dev.name)}">Modifica</button>
          <button class="ghost-button compact-button btn-del-device" data-name="${escHtml(dev.name)}" style="color:#ff89ad">Elimina</button>
        </div>
      `;
      deviceList.appendChild(card);
    });

    deviceList.querySelectorAll(".btn-edit-device").forEach((btn) => {
      btn.addEventListener("click", () => {
        const dev = devices.find((d) => d.name === btn.dataset.name);
        if (dev) openDeviceModal(dev);
      });
    });

    deviceList.querySelectorAll(".btn-del-device").forEach((btn) => {
      btn.addEventListener("click", () => deleteDevice(btn.dataset.name));
    });
  }

  function openDeviceModal(device = null) {
    editingDevice = device;
    deviceDialogTitle.textContent = device ? "Modifica dispositivo" : "Nuovo dispositivo";
    document.getElementById("dev-name").value = device?.name ?? "";
    document.getElementById("dev-name").disabled = !!device; // name is the key — no rename
    document.getElementById("dev-driver").value = device?.driver ?? "tuya_lan";
    document.getElementById("dev-device-id").value = device?.device_id ?? "";
    document.getElementById("dev-ip").value = device?.ip ?? "";
    document.getElementById("dev-local-key").value = ""; // write-only
    document.getElementById("dev-version").value = device?.version ?? 3.3;
    document.getElementById("dev-switch-dp").value = device?.switch_dp ?? 1;
    document.getElementById("dev-access-secret").value = ""; // write-only
    setDeviceFeedback("-");
    deviceDialog.showModal();
  }

  async function saveDevice() {
    const name = editingDevice
      ? editingDevice.name
      : document.getElementById("dev-name").value.trim();

    const payload = {
      name,
      driver: document.getElementById("dev-driver").value,
      device_id: document.getElementById("dev-device-id").value.trim(),
      ip: document.getElementById("dev-ip").value.trim(),
      local_key: document.getElementById("dev-local-key").value,
      version: parseFloat(document.getElementById("dev-version").value) || 3.3,
      switch_dp: parseInt(document.getElementById("dev-switch-dp").value, 10) || 1,
      access_secret: document.getElementById("dev-access-secret").value,
    };

    saveDeviceBtn.disabled = true;
    setDeviceFeedback("Salvataggio...");
    try {
      const { response, data } = await fetchJson("/api/automazione/devices", {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify(payload),
      });
      if (!response.ok || !data.ok) {
        setDeviceFeedback(data.error || "Errore salvataggio", true);
        return;
      }
      deviceDialog.close();
      await loadDevices();
      await loadStatus();
      setFeedback("Dispositivo salvato");
    } catch {
      setDeviceFeedback("Errore di rete", true);
    } finally {
      saveDeviceBtn.disabled = false;
    }
  }

  async function deleteDevice(name) {
    if (!confirm(`Eliminare il dispositivo "${name}"?\nL'operazione è definitiva.`)) return;
    try {
      const { response, data } = await fetchJson(
        `/api/automazione/devices/${encodeURIComponent(name)}`,
        { method: "DELETE" },
      );
      if (!response.ok || !data.ok) {
        setFeedback(data.error || "Errore eliminazione", true);
        return;
      }
      await loadDevices();
      await loadStatus();
      setFeedback(`Dispositivo "${name}" eliminato`);
    } catch {
      setFeedback("Errore di rete", true);
    }
  }

  // ── Rules ──────────────────────────────────────────────────
  async function loadRules() {
    try {
      const { response, data } = await fetchJson("/api/automazione/rules");
      if (!response.ok || !data.ok) return;
      renderRuleList(data.rules || []);
    } catch {
      // ignore
    }
  }

  const ACTION_LABEL = { turn_on: "accendi", turn_off: "spegni" };

  function renderRuleList(list) {
    ruleList.innerHTML = "";
    const empty = document.getElementById("rule-empty");
    if (empty) empty.style.display = list.length === 0 ? "" : "none";

    list.forEach((rule) => {
      const actions = (rule.do || [])
        .map((a) => {
          const label = ACTION_LABEL[a.action] || a.action;
          return `${escHtml(a.device)} → ${label}${a.for ? ` per ${escHtml(a.for)}` : ""}`;
        })
        .join(" · ");
      const window = rule.between ? ` · ${rule.between[0]}→${rule.between[1]}` : "";
      const cooldown = rule.cooldown ? ` · cooldown ${escHtml(String(rule.cooldown))}` : "";

      const card = document.createElement("div");
      card.className = "camera-profile-card";
      card.innerHTML = `
        <div class="camera-profile-info">
          <strong>${escHtml(rule.name)}</strong>
          <span class="camera-profile-meta">
            on: <em>${escHtml(rule.on)}</em>${window}${cooldown}
          </span>
          <span class="camera-profile-meta">${actions || "—"}</span>
        </div>
        <div class="camera-profile-actions">
          <button class="ghost-button compact-button btn-edit-rule" data-name="${escHtml(rule.name)}">Modifica</button>
          <button class="ghost-button compact-button btn-del-rule" data-name="${escHtml(rule.name)}" style="color:#ff89ad">Elimina</button>
        </div>
      `;
      ruleList.appendChild(card);
    });

    // cache for edit modal
    ruleList._rules = list;

    ruleList.querySelectorAll(".btn-edit-rule").forEach((btn) => {
      btn.addEventListener("click", () => {
        const rule = (ruleList._rules || []).find((r) => r.name === btn.dataset.name);
        if (rule) openRuleModal(rule);
      });
    });

    ruleList.querySelectorAll(".btn-del-rule").forEach((btn) => {
      btn.addEventListener("click", () => deleteRule(btn.dataset.name));
    });
  }

  function openRuleModal(rule = null) {
    editingRule = rule;
    ruleDialogTitle.textContent = rule ? "Modifica regola" : "Nuova regola";
    document.getElementById("rule-name").value = rule?.name ?? "";
    document.getElementById("rule-name").disabled = !!rule; // no rename
    document.getElementById("rule-on").value = rule?.on ?? "person_detected";
    document.getElementById("rule-cooldown").value = rule?.cooldown ?? "";
    document.getElementById("rule-from").value = rule?.between?.[0] ?? "";
    document.getElementById("rule-to").value = rule?.between?.[1] ?? "";
    document.getElementById("rule-source").value = rule?.source ?? "";

    ruleActionsList.innerHTML = "";
    const actions = rule?.do?.length ? rule.do : [null];
    actions.forEach((a) => addActionRow(a));

    setRuleFeedback("");
    ruleDialog.showModal();
  }

  function addActionRow(action = null) {
    const row = document.createElement("div");
    row.className = "auto-action-row";

    const deviceOptions = devices
      .map((d) => `<option value="${escHtml(d.name)}"${action?.device === d.name ? " selected" : ""}>${escHtml(d.name)}</option>`)
      .join("");

    row.innerHTML = `
      <label>
        Dispositivo
        <select class="ar-device">
          <option value="">— seleziona —</option>
          ${deviceOptions}
        </select>
      </label>
      <label title="Azione da eseguire sul dispositivo quando la regola scatta.">
        Azione
        <select class="ar-action">
          <option value="turn_on"${action?.action === "turn_on" || !action?.action ? " selected" : ""}>Accendi (turn_on)</option>
          <option value="turn_off"${action?.action === "turn_off" ? " selected" : ""}>Spegni (turn_off)</option>
        </select>
      </label>
      <label title="Durata dopo cui il dispositivo si spegne automaticamente. Es: 30s, 5m, 1h. Lascia vuoto per nessun auto-spegnimento.">
        Auto-spegnimento
        <input class="ar-for" type="text" placeholder="es. 30s, 5m" value="${escHtml(action?.for ?? "")}" />
      </label>
      <button class="auto-action-del" type="button" title="Rimuovi azione">✕</button>
    `;

    row.querySelector(".auto-action-del").addEventListener("click", () => row.remove());
    ruleActionsList.appendChild(row);
  }

  async function saveRule() {
    const name = editingRule
      ? editingRule.name
      : document.getElementById("rule-name").value.trim();

    const doActions = Array.from(ruleActionsList.querySelectorAll(".auto-action-row"))
      .map((row) => {
        const action = { device: row.querySelector(".ar-device").value, action: row.querySelector(".ar-action").value };
        const forVal = row.querySelector(".ar-for").value.trim();
        if (forVal) action.for = forVal;
        return action;
      })
      .filter((a) => a.device);

    const payload = {
      name,
      on: document.getElementById("rule-on").value,
      cooldown: document.getElementById("rule-cooldown").value.trim() || undefined,
      between_from: document.getElementById("rule-from").value.trim() || undefined,
      between_to: document.getElementById("rule-to").value.trim() || undefined,
      source: document.getElementById("rule-source").value.trim() || undefined,
      do: doActions,
    };

    saveRuleBtn.disabled = true;
    setRuleFeedback("Salvataggio...");
    try {
      const { response, data } = await fetchJson("/api/automazione/rules", {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify(payload),
      });
      if (!response.ok || !data.ok) {
        setRuleFeedback(data.error || "Errore salvataggio", true);
        return;
      }
      ruleDialog.close();
      await loadRules();
      await loadStatus();
      setFeedback("Regola salvata");
    } catch {
      setRuleFeedback("Errore di rete", true);
    } finally {
      saveRuleBtn.disabled = false;
    }
  }

  async function deleteRule(name) {
    if (!confirm(`Eliminare la regola "${name}"?\nL'operazione è definitiva.`)) return;
    try {
      const { response, data } = await fetchJson(
        `/api/automazione/rules/${encodeURIComponent(name)}`,
        { method: "DELETE" },
      );
      if (!response.ok || !data.ok) {
        setFeedback(data.error || "Errore eliminazione", true);
        return;
      }
      await loadRules();
      await loadStatus();
      setFeedback(`Regola "${name}" eliminata`);
    } catch {
      setFeedback("Errore di rete", true);
    }
  }

  // ── Utility ────────────────────────────────────────────────
  function escHtml(str) {
    return String(str ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ── Public API ─────────────────────────────────────────────
  function init() {
    toggleCheckbox.addEventListener("change", (e) => setEnabled(e.target.checked));

    tabButtons.forEach((btn) => {
      btn.addEventListener("click", () => showTab(btn.dataset.tab));
    });

    addDeviceBtn.addEventListener("click", () => openDeviceModal(null));
    saveDeviceBtn.addEventListener("click", saveDevice);
    closeDeviceBtns.forEach((btn) => btn.addEventListener("click", () => deviceDialog.close()));

    addRuleBtn.addEventListener("click", () => openRuleModal(null));
    addActionBtn.addEventListener("click", () => addActionRow(null));
    saveRuleBtn.addEventListener("click", saveRule);
    closeRuleBtns.forEach((btn) => btn.addEventListener("click", () => ruleDialog.close()));

    showTab("devices");
    loadStatus();
    loadDevices();
    loadRules();
  }

  return { init };
}
