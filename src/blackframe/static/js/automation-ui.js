import { fetchBlobUrl, fetchJson } from "./api.js";
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
    // rename
    renameDialog,
    renameFeedback,
    renameSaveBtn,
    closeRenameBtns,
    // wizard
    wizardBtn,
    wizardDialog,
    wizardFeedback,
    wizardPreview,
    wizardScanBtn,
    wizardUploadBtn,
    wizardCommitBtn,
    wizardDevicesFile,
    wizardSnapshotFile,
    closeWizardBtns,
    // import/export
    exportBtn,
    importBtn,
    importFile,
  } = elements;

  // ── State ──────────────────────────────────────────────────
  let devices = [];
  let editingDevice = null; // null = new device
  let editingRule = null; // null = new rule
  let renamingDevice = null;
  let wizardScanReady = false; // true dopo scan LAN con device importabili

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

  function setRenameFeedback(text, isError = false) {
    renameFeedback.textContent = text;
    renameFeedback.style.color = isError ? "#ff89ad" : "";
  }

  function setWizardFeedback(text, isError = false) {
    wizardFeedback.textContent = text;
    wizardFeedback.style.color = isError ? "#ff89ad" : "";
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
          <button class="ghost-button compact-button btn-test-device" data-name="${escHtml(dev.name)}" data-action="turn_on" title="Accendi per verificare">⏻ On</button>
          <button class="ghost-button compact-button btn-test-device" data-name="${escHtml(dev.name)}" data-action="turn_off" title="Spegni per verificare">⭘ Off</button>
          <button class="ghost-button compact-button btn-rename-device" data-name="${escHtml(dev.name)}">Rinomina</button>
          <button class="ghost-button compact-button btn-edit-device" data-name="${escHtml(dev.name)}">Modifica</button>
          <button class="ghost-button compact-button btn-del-device" data-name="${escHtml(dev.name)}" style="color:#ff89ad">Elimina</button>
        </div>
      `;
      deviceList.appendChild(card);
    });

    deviceList.querySelectorAll(".btn-test-device").forEach((btn) => {
      btn.addEventListener("click", () => testDevice(btn.dataset.name, btn.dataset.action));
    });

    deviceList.querySelectorAll(".btn-rename-device").forEach((btn) => {
      btn.addEventListener("click", () => {
        const dev = devices.find((d) => d.name === btn.dataset.name);
        if (dev) openRenameModal(dev);
      });
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

  // ── Device test + rename ───────────────────────────────────
  async function testDevice(name, action) {
    const verb = action === "turn_off" ? "spegnimento" : "accensione";
    setFeedback(`Test ${verb} "${name}"...`);
    try {
      const { response, data } = await fetchJson(
        `/api/automazione/devices/${encodeURIComponent(name)}/test`,
        { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ action }) },
      );
      if (!response.ok || !data.ok) {
        setFeedback(data.error || "Test fallito", true);
        return;
      }
      setFeedback(`"${name}": ${action === "turn_off" ? "spento" : "acceso"} ✓`);
    } catch {
      setFeedback("Errore di rete", true);
    }
  }

  function openRenameModal(device) {
    renamingDevice = device;
    document.getElementById("rename-old").value = device.name;
    document.getElementById("rename-new").value = "";
    setRenameFeedback("");
    renameDialog.showModal();
  }

  async function renameDevice() {
    if (!renamingDevice) return;
    const newName = document.getElementById("rename-new").value.trim();
    renameSaveBtn.disabled = true;
    setRenameFeedback("Rinomina...");
    try {
      const { response, data } = await fetchJson(
        `/api/automazione/devices/${encodeURIComponent(renamingDevice.name)}/rename`,
        { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ new_name: newName }) },
      );
      if (!response.ok || !data.ok) {
        setRenameFeedback(data.error || "Errore rinomina", true);
        return;
      }
      renameDialog.close();
      await loadDevices();
      await loadRules();
      setFeedback(`Dispositivo rinominato in "${newName}"`);
    } catch {
      setRenameFeedback("Errore di rete", true);
    } finally {
      renameSaveBtn.disabled = false;
    }
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
    setDeviceFeedback("");
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
      const enabled = rule.enabled !== false;

      const card = document.createElement("div");
      card.className = "camera-profile-card";
      card.innerHTML = `
        <div class="camera-profile-info">
          <strong>${escHtml(rule.name)} ${enabled ? "" : "<span style='opacity:.5;font-weight:400'>(disabilitata)</span>"}</strong>
          <span class="camera-profile-meta">
            on: <em>${escHtml(rule.on)}</em>${window}${cooldown}
          </span>
          <span class="camera-profile-meta">${actions || "—"}</span>
        </div>
        <div class="camera-profile-actions">
          <button class="ghost-button compact-button btn-preview-rule" data-name="${escHtml(rule.name)}" title="Mostra le azioni senza eseguirle">Anteprima</button>
          <button class="ghost-button compact-button btn-run-rule" data-name="${escHtml(rule.name)}" title="Esegui ora le azioni della regola">Esegui</button>
          <button class="ghost-button compact-button btn-toggle-rule" data-name="${escHtml(rule.name)}" data-enabled="${enabled}">${enabled ? "Disabilita" : "Abilita"}</button>
          <button class="ghost-button compact-button btn-edit-rule" data-name="${escHtml(rule.name)}">Modifica</button>
          <button class="ghost-button compact-button btn-del-rule" data-name="${escHtml(rule.name)}" style="color:#ff89ad">Elimina</button>
        </div>
      `;
      ruleList.appendChild(card);
    });

    // cache for edit modal
    ruleList._rules = list;

    ruleList.querySelectorAll(".btn-preview-rule").forEach((btn) => {
      btn.addEventListener("click", () => testRule(btn.dataset.name, false));
    });

    ruleList.querySelectorAll(".btn-run-rule").forEach((btn) => {
      btn.addEventListener("click", () => testRule(btn.dataset.name, true));
    });

    ruleList.querySelectorAll(".btn-toggle-rule").forEach((btn) => {
      btn.addEventListener("click", () =>
        toggleRule(btn.dataset.name, btn.dataset.enabled !== "true"),
      );
    });

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

  // ── Rule test + enable toggle ──────────────────────────────
  async function testRule(name, execute) {
    setFeedback(execute ? `Esecuzione "${name}"...` : `Anteprima "${name}"...`);
    try {
      const { response, data } = await fetchJson(
        `/api/automazione/rules/${encodeURIComponent(name)}/test`,
        { method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ execute }) },
      );
      if (!response.ok || !data.ok) {
        setFeedback(data.error || "Test regola fallito", true);
        return;
      }
      const summary = (data.actions || [])
        .map((a) => `${a.device}→${ACTION_LABEL[a.action] || a.action}`)
        .join(", ");
      setFeedback(
        execute
          ? `"${name}" eseguita: ${summary || "nessuna azione"} ✓`
          : `Anteprima "${name}": ${summary || "nessuna azione"}`,
      );
    } catch {
      setFeedback("Errore di rete", true);
    }
  }

  async function toggleRule(name, enabled) {
    try {
      const { response, data } = await fetchJson(
        `/api/automazione/rules/${encodeURIComponent(name)}/enabled`,
        { method: "PATCH", headers: JSON_HEADERS, body: JSON.stringify({ enabled }) },
      );
      if (!response.ok || !data.ok) {
        setFeedback(data.error || "Errore aggiornamento", true);
        return;
      }
      await loadRules();
      await loadStatus();
      setFeedback(`Regola "${name}" ${enabled ? "abilitata" : "disabilitata"}`);
    } catch {
      setFeedback("Errore di rete", true);
    }
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

  // ── Wizard (scan LAN + import file tinytuya) ───────────────
  function openWizard() {
    wizardPreview.innerHTML = "";
    wizardCommitBtn.hidden = true;
    wizardScanReady = false;
    if (wizardDevicesFile) wizardDevicesFile.value = "";
    if (wizardSnapshotFile) wizardSnapshotFile.value = "";
    setWizardFeedback("");
    wizardDialog.showModal();
  }

  function renderWizardPreview(found, skipped) {
    wizardPreview.innerHTML = "";
    (found || []).forEach((dev) => {
      const row = document.createElement("div");
      row.className = "camera-profile-card";
      row.innerHTML = `
        <div class="camera-profile-info">
          <strong>${escHtml(dev.name)}</strong>
          <span class="camera-profile-meta">${escHtml(dev.ip || "—")} · DP ${dev.switch_dp ?? 1} · key ${dev.local_key === "***" ? "✓" : "—"}</span>
        </div>`;
      wizardPreview.appendChild(row);
    });
    (skipped || []).forEach((line) => {
      const p = document.createElement("p");
      p.className = "tg-hint";
      p.textContent = `⚠ ${line}`;
      wizardPreview.appendChild(p);
    });
  }

  async function scanLan() {
    setWizardFeedback("Scansione rete (può richiedere ~10s)...");
    wizardCommitBtn.hidden = true;
    wizardScanReady = false;
    try {
      const { response, data } = await fetchJson("/api/automazione/devices/scan", {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify({ commit: false }),
      });
      if (!response.ok || !data.ok) {
        setWizardFeedback(data.error || "Scan fallito", true);
        return;
      }
      renderWizardPreview(data.devices, data.skipped);
      const ready = (data.devices || []).length;
      wizardScanReady = ready > 0;
      wizardCommitBtn.hidden = ready === 0;
      if (ready > 0) {
        setWizardFeedback(
          `${data.found} trovati, ${ready} pronti. Clicca «Salva selezionati» per importarli.`,
        );
      } else if (data.found > 0) {
        setWizardFeedback(
          `${data.found} trovati ma senza chiave locale: carica devices.json dal wizard tinytuya.`,
          true,
        );
      } else {
        setWizardFeedback("Nessun dispositivo Tuya trovato sulla rete del server.", true);
      }
    } catch {
      setWizardFeedback("Errore di rete", true);
    }
  }

  function wizardFormData(commit) {
    const fd = new FormData();
    if (wizardDevicesFile?.files?.[0]) fd.append("devices", wizardDevicesFile.files[0]);
    if (wizardSnapshotFile?.files?.[0]) fd.append("snapshot", wizardSnapshotFile.files[0]);
    if (commit) fd.append("commit", "1");
    return fd;
  }

  async function analyzeFiles() {
    if (!wizardDevicesFile?.files?.[0]) {
      setWizardFeedback("Seleziona un file devices.json", true);
      return;
    }
    setWizardFeedback("Analisi file...");
    try {
      const { response, data } = await fetchJson("/api/automazione/devices/import-tuya", {
        method: "POST",
        body: wizardFormData(false),
      });
      if (!response.ok || !data.ok) {
        setWizardFeedback(data.error || "Analisi fallita", true);
        return;
      }
      renderWizardPreview(data.devices, data.skipped);
      wizardScanReady = false;
      wizardCommitBtn.hidden = (data.devices || []).length === 0;
      setWizardFeedback(`${(data.devices || []).length} dispositivi pronti per il salvataggio.`);
    } catch {
      setWizardFeedback("Errore di rete", true);
    }
  }

  async function commitWizard() {
    const hasFile = Boolean(wizardDevicesFile?.files?.[0]);
    if (!hasFile && !wizardScanReady) {
      setWizardFeedback("Esegui una scansione o carica devices.json", true);
      return;
    }
    setWizardFeedback(hasFile ? "Salvataggio da file..." : "Salvataggio da scansione (~10s)...");
    wizardCommitBtn.disabled = true;
    try {
      let response;
      let data;
      if (hasFile) {
        ({ response, data } = await fetchJson("/api/automazione/devices/import-tuya", {
          method: "POST",
          body: wizardFormData(true),
        }));
      } else {
        ({ response, data } = await fetchJson("/api/automazione/devices/scan", {
          method: "POST",
          headers: JSON_HEADERS,
          body: JSON.stringify({ commit: true }),
        }));
      }
      if (!response.ok || !data.ok) {
        setWizardFeedback(data.error || "Salvataggio fallito", true);
        return;
      }
      wizardDialog.close();
      await loadDevices();
      await loadStatus();
      setFeedback(`${(data.devices || []).length} dispositivi importati`);
    } catch {
      setWizardFeedback("Errore di rete", true);
    } finally {
      wizardCommitBtn.disabled = false;
    }
  }

  // ── Import / Export config ─────────────────────────────────
  async function exportConfig() {
    setFeedback("Esportazione...");
    try {
      const url = await fetchBlobUrl("/api/automazione/export");
      const a = document.createElement("a");
      a.href = url;
      a.download = "blackframe-automazione.json";
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      setFeedback("Configurazione esportata");
    } catch {
      setFeedback("Esportazione fallita", true);
    }
  }

  async function importConfig(file) {
    if (!file) return;
    setFeedback("Importazione...");
    try {
      const text = await file.text();
      let bundle;
      try {
        bundle = JSON.parse(text);
      } catch {
        setFeedback("File JSON non valido", true);
        return;
      }
      const { response, data } = await fetchJson("/api/automazione/import", {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify(bundle),
      });
      if (!response.ok || !data.ok) {
        setFeedback(data.error || "Importazione fallita", true);
        return;
      }
      await loadDevices();
      await loadRules();
      await loadStatus();
      const errs = (data.errors || []).length;
      setFeedback(
        `Importati ${data.devices_imported} dispositivi, ${data.rules_imported} regole` +
          (errs ? ` · ${errs} errori (vedi console)` : ""),
      );
      if (errs) console.warn("Import errori:", data.errors);
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

    // rename
    renameSaveBtn.addEventListener("click", renameDevice);
    closeRenameBtns.forEach((btn) => btn.addEventListener("click", () => renameDialog.close()));

    // wizard
    wizardBtn.addEventListener("click", openWizard);
    wizardScanBtn.addEventListener("click", scanLan);
    wizardUploadBtn.addEventListener("click", analyzeFiles);
    wizardCommitBtn.addEventListener("click", commitWizard);
    closeWizardBtns.forEach((btn) => btn.addEventListener("click", () => wizardDialog.close()));

    // import / export
    exportBtn.addEventListener("click", exportConfig);
    importBtn.addEventListener("click", () => importFile.click());
    importFile.addEventListener("change", (e) => {
      const file = e.target.files?.[0];
      importConfig(file);
      e.target.value = "";
    });

    showTab("devices");
    loadStatus();
    loadDevices();
    loadRules();
  }

  return { init };
}
