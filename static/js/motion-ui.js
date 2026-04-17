import { fetchJson } from "./api.js";
import { setPillState } from "./ui.js";

export function createMotionController(elements) {
  const {
    motionPill,
    motionState,
    motionLastEvent,
    motionCurrentArea,
    motionLastTriggerArea,
    motionThreshold,
    motionCapturePath,
    motionCaptureImage,
    motionCaptureEmpty,
    captureList,
    captureSummary,
    captureToggle,
    runtimeSave,
    runtimeFeedback,
    cfgMotionEnabled,
    cfgMotionThreshold,
    cfgMotionThresholdHint,
    cfgMotionMinArea,
    cfgMotionMinAreaHint,
  } = elements;

  let selectedCaptureId = null;
  let latestCaptureId = null;
  let lastCaptureSignature = "";
  let previewFrames = [];
  let previewFrameIndex = 0;
  let previewTimer = null;
  let archiveExpanded = false;
  let runtimeLoaded = false;
  let monitoringEnabled = null;
  let runtimeDirty = false;

  const collapsedCaptureLimit = 5;
  const expandedCaptureLimit = 60;
  const capturePanelHeight = 560;
  const runtimeFieldMap = {
    MOTION_ENABLED: { element: cfgMotionEnabled, type: "bool" },
    MOTION_THRESHOLD: { element: cfgMotionThreshold, type: "int" },
    MOTION_MIN_AREA: { element: cfgMotionMinArea, type: "int" },
  };

  function thresholdToUiSensitivity(thresholdValue) {
    const numeric = Number(thresholdValue);
    if (!Number.isFinite(numeric)) {
      return 128;
    }
    return Math.max(1, Math.min(255, 256 - Math.round(numeric)));
  }

  function uiSensitivityToThreshold(uiValue) {
    const numeric = Number(uiValue);
    if (!Number.isFinite(numeric)) {
      return null;
    }
    return Math.max(1, Math.min(255, 256 - Math.round(numeric)));
  }

  function minAreaToUiSensitivity(minAreaValue) {
    const numeric = Number(minAreaValue);
    if (!Number.isFinite(numeric)) {
      return 15000;
    }
    return Math.max(1, Math.min(30000, 30001 - Math.round(numeric)));
  }

  function uiSensitivityToMinArea(uiValue) {
    const numeric = Number(uiValue);
    if (!Number.isFinite(numeric)) {
      return null;
    }
    return Math.max(1, Math.min(30000, 30001 - Math.round(numeric)));
  }

  function setRuntimeFeedback(text, isError = false) {
    runtimeFeedback.textContent = text;
    runtimeFeedback.style.color = isError ? "#ff89ad" : "";
  }

  function applyRuntimeConfigToForm(config) {
    for (const [key, spec] of Object.entries(runtimeFieldMap)) {
      const value = config[key];
      if (value === undefined || value === null || !spec.element) {
        continue;
      }
      if (spec.type === "bool") {
        spec.element.checked = Boolean(value);
      } else {
        if (key === "MOTION_THRESHOLD") {
          spec.element.value = thresholdToUiSensitivity(value);
        } else if (key === "MOTION_MIN_AREA") {
          spec.element.value = minAreaToUiSensitivity(value);
        } else {
          spec.element.value = value;
        }
      }
    }
    updateSliderHints();
  }

  function markRuntimeDirty() {
    runtimeDirty = true;
  }

  function setMotionControlsEnabled(enabled) {
    const active = Boolean(enabled);
    if (cfgMotionThreshold) {
      cfgMotionThreshold.disabled = !active;
    }
    if (cfgMotionMinArea) {
      cfgMotionMinArea.disabled = !active;
    }
    if (runtimeSave) {
      runtimeSave.disabled = !active;
    }
  }

  function getThresholdHint(value) {
    if (value <= 85) {
      return "Poco sensibile: rileva solo variazioni marcate";
    }
    if (value <= 170) {
      return "Sensibilita media: buon equilibrio";
    }
    return "Molto sensibile: rileva anche micro-variazioni";
  }

  function getMinAreaHint(value) {
    if (value <= 10000) {
      return "Poco sensibile: ignora movimenti piccoli";
    }
    if (value <= 22000) {
      return "Sensibilita media: filtra disturbi leggeri";
    }
    return "Molto sensibile: rileva anche movimenti piccoli/lontani";
  }

  function updateSliderHints() {
    if (cfgMotionThreshold && cfgMotionThresholdHint) {
      const thresholdValue = parseInt(cfgMotionThreshold.value, 10);
      if (Number.isFinite(thresholdValue)) {
        cfgMotionThresholdHint.textContent = getThresholdHint(thresholdValue);
      }
    }
    if (cfgMotionMinArea && cfgMotionMinAreaHint) {
      const minAreaValue = parseInt(cfgMotionMinArea.value, 10);
      if (Number.isFinite(minAreaValue)) {
        cfgMotionMinAreaHint.textContent = getMinAreaHint(minAreaValue);
      }
    }
  }

  function collectRuntimeUpdates() {
    const updates = {};
    for (const [key, spec] of Object.entries(runtimeFieldMap)) {
      if (!spec.element) {
        continue;
      }
      if (spec.type === "bool") {
        updates[key] = spec.element.checked;
      } else if (spec.type === "int") {
        const parsed = parseInt(spec.element.value, 10);
        if (!Number.isFinite(parsed)) {
          continue;
        }
        if (key === "MOTION_THRESHOLD") {
          const threshold = uiSensitivityToThreshold(parsed);
          if (!Number.isFinite(threshold)) {
            continue;
          }
          updates[key] = threshold;
        } else if (key === "MOTION_MIN_AREA") {
          const minArea = uiSensitivityToMinArea(parsed);
          if (!Number.isFinite(minArea)) {
            continue;
          }
          updates[key] = minArea;
        } else {
          updates[key] = parsed;
        }
      } else if (spec.type === "float") {
        const parsed = parseFloat(spec.element.value);
        if (!Number.isFinite(parsed)) {
          continue;
        }
        updates[key] = parsed;
      } else {
        const text = spec.element.value.trim();
        if (!text) {
          continue;
        }
        updates[key] = text;
      }
    }
    return updates;
  }

  async function refreshRuntimeConfig() {
    if (!runtimeSave || !runtimeFeedback) {
      return;
    }
    try {
      const { data } = await fetchJson("/runtime_config");
      if (!runtimeDirty) {
        applyRuntimeConfigToForm(data.config || {});
      }
      if (typeof data.config?.MOTION_ENABLED === "boolean") {
        monitoringEnabled = data.config.MOTION_ENABLED;
        setMotionControlsEnabled(monitoringEnabled);
      }
      if (!runtimeLoaded) {
        setRuntimeFeedback("Config runtime caricata");
      }
      runtimeLoaded = true;
    } catch {
      setRuntimeFeedback("Impossibile leggere runtime config", true);
    }
  }

  async function saveRuntimeConfig() {
    const updates = collectRuntimeUpdates();
    if (Object.keys(updates).length === 0) {
      setRuntimeFeedback("Nessun parametro valido da salvare", true);
      return;
    }
    runtimeSave.disabled = true;
    setRuntimeFeedback("Salvataggio in corso...");
    try {
      const { response, data } = await fetchJson("/api/runtime_config", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ updates }),
      });
      if (!response.ok) {
        setRuntimeFeedback(data.error || "Errore salvataggio runtime config", true);
        return;
      }
      applyRuntimeConfigToForm(data.config || {});
      runtimeDirty = false;
      setRuntimeFeedback("Config salvata su .env e applicata");
      await refreshMotionStatus();
    } catch {
      setRuntimeFeedback("Errore di rete durante salvataggio", true);
    } finally {
      runtimeSave.disabled = false;
    }
  }

  async function saveMotionEnabled(value) {
    runtimeSave.disabled = true;
    setRuntimeFeedback("Aggiornamento monitoraggio...");
    try {
      const { response, data } = await fetchJson("/api/runtime_config", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ updates: { MOTION_ENABLED: Boolean(value) } }),
      });
      if (!response.ok) {
        setRuntimeFeedback(data.error || "Errore aggiornamento monitoraggio", true);
        return;
      }
      applyRuntimeConfigToForm(data.config || {});
      runtimeDirty = false;
      if (typeof data.config?.MOTION_ENABLED === "boolean") {
        monitoringEnabled = data.config.MOTION_ENABLED;
      } else {
        monitoringEnabled = Boolean(value);
      }
      setMotionControlsEnabled(monitoringEnabled);
      setRuntimeFeedback("Monitoraggio aggiornato");
      await refreshMotionStatus();
      return monitoringEnabled;
    } catch {
      setRuntimeFeedback("Errore di rete durante aggiornamento", true);
      return null;
    } finally {
      runtimeSave.disabled = false;
    }
  }

  function stopPreviewAnimation() {
    if (previewTimer) {
      clearInterval(previewTimer);
      previewTimer = null;
    }
  }

  function startPreviewAnimation(frames) {
    stopPreviewAnimation();
    previewFrames = frames || [];
    previewFrameIndex = 0;

    if (previewFrames.length === 0) {
      return;
    }

    motionCaptureImage.src = previewFrames[0];

    if (previewFrames.length === 1) {
      return;
    }

    previewTimer = setInterval(() => {
      previewFrameIndex = (previewFrameIndex + 1) % previewFrames.length;
      motionCaptureImage.src = previewFrames[previewFrameIndex];
    }, 220);
  }

  function formatCaptureLabel(capture) {
    return `${capture.label} · ${capture.frame_count} frame`;
  }

  function showCapture(capture) {
    selectedCaptureId = capture.id;
    motionCaptureImage.style.display = "block";
    motionCaptureEmpty.style.display = "none";
    motionCapturePath.textContent = formatCaptureLabel(capture);
    startPreviewAnimation(capture.frames?.length ? capture.frames : [capture.url]);
    document.querySelectorAll(".capture-row").forEach((row) => {
      row.classList.toggle("selected", row.dataset.captureId === capture.id);
    });
  }

  async function openCapture(capture) {
    try {
      const { response, data } = await fetchJson(`/motion_event/${capture.id}`);
      if (response.ok) {
        showCapture(data);
        return;
      }
    } catch {
    }

    showCapture(capture);
  }

  function buildCaptureRow(capture) {
    const button = document.createElement("button");
    button.className = "capture-row";
    button.type = "button";
    button.title = capture.label;
    button.dataset.captureId = capture.id;

    const image = document.createElement("img");
    image.src = capture.url;
    image.alt = capture.label;
    button.appendChild(image);

    const meta = document.createElement("div");
    meta.className = "capture-row-meta";

    const title = document.createElement("span");
    title.className = "capture-row-title";
    title.textContent = capture.label;
    meta.appendChild(title);

    const detail = document.createElement("span");
    detail.className = "capture-row-label";
    detail.textContent = `${capture.frame_count} frame`;
    meta.appendChild(detail);

    button.appendChild(meta);

    button.addEventListener("click", async () => {
      await openCapture(capture);
    });

    return button;
  }

  async function refreshCaptureList() {
    const limit = archiveExpanded ? expandedCaptureLimit : collapsedCaptureLimit;

    try {
      const { data } = await fetchJson(`/motion_captures?limit=${limit}`);
      const captures = data.captures || [];
      const total = data.total || captures.length;
      const signature = `${archiveExpanded ? "full" : "compact"}::${captures
        .map((capture) => capture.id)
        .join("|")}::${total}`;

      captureSummary.textContent = archiveExpanded
        ? `Archivio completo · ${total} eventi · scorri elenco`
        : `Ultimi ${Math.min(collapsedCaptureLimit, total)} eventi`;
      captureToggle.style.display = total > collapsedCaptureLimit ? "inline-flex" : "none";
      captureToggle.textContent = archiveExpanded ? "Mostra meno" : "Mostra altri";
      captureList.style.maxHeight = `${capturePanelHeight}px`;

      if (signature === lastCaptureSignature && captures.length > 0) {
        if (!selectedCaptureId && captures[0].id !== latestCaptureId) {
          latestCaptureId = captures[0].id;
          await openCapture(captures[0]);
        }
        return;
      }

      lastCaptureSignature = signature;
      captureList.innerHTML = "";
      captureList.scrollTop = 0;

      if (captures.length === 0) {
        if (!selectedCaptureId) {
          motionCaptureEmpty.style.display = "grid";
        }
        captureList.innerHTML = '<p class="capture-list-empty">Nessun evento salvato ancora</p>';
        return;
      }

      motionCaptureEmpty.style.display = "none";

      for (const [index, capture] of captures.entries()) {
        const button = buildCaptureRow(capture);

        if (index === 0) {
          latestCaptureId = capture.id;
        }

        if (index === 0 && !selectedCaptureId) {
          await openCapture(capture);
        }

        if (capture.id === selectedCaptureId) {
          button.classList.add("selected");
        }

        captureList.appendChild(button);
      }
    } catch {
      captureList.innerHTML =
        '<p class="capture-list-empty">Impossibile leggere l\'archivio eventi</p>';
    }
  }

  async function refreshMotionStatus() {
    try {
      const { data } = await fetchJson("/motion_status");
      monitoringEnabled = Boolean(data.enabled);

      motionThreshold.textContent = data.min_area ?? "-";
      motionCurrentArea.textContent = data.current_area ?? 0;
      motionLastTriggerArea.textContent = data.last_trigger_area ?? 0;
      motionLastEvent.textContent = data.last_motion_at || "-";

      if (data.last_event_id) {
        motionCapturePath.textContent = data.last_event_id;
      }

      if (data.last_capture_path) {
        const capture = {
          id: data.last_event_id || "latest",
          path: data.last_capture_path,
          url: data.last_preview_url || "/latest_motion.jpg",
          label: data.last_event_id || "Evento recente",
          frame_count: "?",
          frames: [data.last_preview_url || "/latest_motion.jpg"],
        };
        const shouldFollowLatest =
          !selectedCaptureId || selectedCaptureId === latestCaptureId;
        latestCaptureId = capture.id;
        if (shouldFollowLatest) {
          motionCaptureImage.style.display = "block";
          motionCaptureEmpty.style.display = "none";
          showCapture(capture);
        }
      } else if (!selectedCaptureId) {
        motionCaptureImage.removeAttribute("src");
        motionCaptureImage.style.display = "none";
        motionCaptureEmpty.style.display = "grid";
      }

      if (!data.enabled) {
        setPillState(motionPill, "Motion: off", "error");
        motionState.textContent = data.error || "Disabilitato";
        setMotionControlsEnabled(false);
        return false;
      }

      if (data.motion_detected) {
        setPillState(motionPill, "Motion: live", "active");
        motionState.textContent = "Movimento in corso";
        setMotionControlsEnabled(true);
        return true;
      }

      setPillState(motionPill, "Motion: armed", "ok");
      motionState.textContent = data.error || "Monitoraggio attivo";
      setMotionControlsEnabled(true);
      return true;
    } catch {
      monitoringEnabled = null;
      setPillState(motionPill, "Motion: errore", "error");
      motionState.textContent = "Impossibile leggere lo stato motion";
      setMotionControlsEnabled(false);
      return null;
    }
  }

  function bind() {
    captureToggle.addEventListener("click", async () => {
      archiveExpanded = !archiveExpanded;
      lastCaptureSignature = "";
      captureList.scrollTop = 0;
      await refreshCaptureList();
    });
    if (runtimeSave) {
      runtimeSave.addEventListener("click", async () => {
        await saveRuntimeConfig();
      });
    }
    if (cfgMotionEnabled) {
      cfgMotionEnabled.addEventListener("change", async () => {
        await saveMotionEnabled(cfgMotionEnabled.checked);
      });
    }
    if (cfgMotionThreshold) {
      cfgMotionThreshold.addEventListener("input", markRuntimeDirty);
      cfgMotionThreshold.addEventListener("input", updateSliderHints);
      cfgMotionThreshold.addEventListener("change", updateSliderHints);
    }
    if (cfgMotionMinArea) {
      cfgMotionMinArea.addEventListener("input", markRuntimeDirty);
      cfgMotionMinArea.addEventListener("input", updateSliderHints);
      cfgMotionMinArea.addEventListener("change", updateSliderHints);
    }
    updateSliderHints();
  }

  return {
    bind,
    refreshCaptureList,
    refreshMotionStatus,
    refreshRuntimeConfig,
    saveMotionEnabled,
    getMonitoringEnabled: () => monitoringEnabled,
  };
}
