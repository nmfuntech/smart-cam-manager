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
    motionPreviewBadge,
    motionCaptureEmpty,
    captureList,
    captureSummary,
    captureFilter,
    captureToggle,
    captureClear,
    captureOpenFolder,
    runtimeSave,
    runtimeSaveClassification,
    runtimeFeedback,
    runtimeFeedbackClassification,
    cfgMotionEnabled,
    cfgMotionThreshold,
    cfgMotionThresholdHint,
    cfgMotionThresholdValue,
    cfgMotionMinArea,
    cfgMotionMinAreaHint,
    cfgMotionMinAreaValue,
    cfgClassificationEnabled,
    cfgClassificationDetectPerson,
    cfgClassificationDetectPet,
    cfgClassificationBackend,
    cfgClassificationMinConfidence,
    cfgClassificationMinConfidenceValue,
    cfgClassificationSamplePolicy,
    cfgRecordEnabled,
    cfgNotifyTelegramEnabled,
    motionCaptureVideo,
  } = elements;

  // "" for the active camera, "/cam/<id>" for a monitored secondary camera.
  // On a secondary (live-only) view the gallery/config DOM is not rendered, so the
  // status poll targets the per-camera endpoint and skips gallery side-effects.
  const feedBase = document.body.dataset.feedBase || "";
  const isActiveView = document.body.dataset.activeView !== "false";

  let selectedCaptureId = null;
  let selectedCapturePinned = false;
  let latestCaptureId = null;
  let lastCaptureSignature = "";
  let previewFrames = [];
  let previewFrameIndex = 0;
  let previewTimer = null;
  let previewLoadedFrames = [];
  let previewAnimationToken = 0;
  let archiveExpanded = false;
  let runtimeLoaded = false;
  let monitoringEnabled = null;
  let runtimeDirty = false;
  let clearingCaptures = false;
  let captureCategoryFilter = "all";
  let lastMotionListRefreshKey = "";
  let refreshCaptureListInFlight = null;
  let latestPreviewRequestKey = "";

  const collapsedCaptureLimit = 5;
  const expandedCaptureLimit = 60;
  const capturePanelHeight = 560;
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
  const runtimeFieldMap = {
    MOTION_ENABLED: { element: cfgMotionEnabled, type: "bool" },
    MOTION_THRESHOLD: { element: cfgMotionThreshold, type: "int" },
    MOTION_MIN_AREA: { element: cfgMotionMinArea, type: "int" },
    CLASSIFICATION_ENABLED: { element: cfgClassificationEnabled, type: "bool" },
    CLASSIFICATION_DETECT_PERSON: { element: cfgClassificationDetectPerson, type: "bool" },
    CLASSIFICATION_DETECT_PET: { element: cfgClassificationDetectPet, type: "bool" },
    CLASSIFICATION_BACKEND: { element: cfgClassificationBackend, type: "str" },
    CLASSIFICATION_MIN_CONFIDENCE: { element: cfgClassificationMinConfidence, type: "float" },
    CLASSIFICATION_SAMPLE_POLICY: { element: cfgClassificationSamplePolicy, type: "str" },
    RECORD_ENABLED: { element: cfgRecordEnabled, type: "bool" },
    NOTIFY_TELEGRAM_ENABLED: { element: cfgNotifyTelegramEnabled, type: "bool" },
  };

  function nearestStepByValue(steps, rawValue) {
    const numeric = Number(rawValue);
    if (!Number.isFinite(numeric)) {
      return steps[1] || steps[0];
    }
    return steps.reduce((best, step) => {
      if (!best) {
        return step;
      }
      return Math.abs(step.value - numeric) < Math.abs(best.value - numeric) ? step : best;
    }, null);
  }

  function stepByLevel(steps, rawLevel) {
    const numeric = Number(rawLevel);
    if (!Number.isFinite(numeric)) {
      return steps[1] || steps[0];
    }
    return steps.find((step) => step.level === Math.round(numeric)) || steps[1] || steps[0];
  }

  function thresholdToUiSensitivity(thresholdValue) {
    return nearestStepByValue(thresholdSteps, thresholdValue).level;
  }

  function uiSensitivityToThreshold(uiValue) {
    return stepByLevel(thresholdSteps, uiValue).value;
  }

  function minAreaToUiSensitivity(minAreaValue) {
    return nearestStepByValue(minAreaSteps, minAreaValue).level;
  }

  function uiSensitivityToMinArea(uiValue) {
    return stepByLevel(minAreaSteps, uiValue).value;
  }

  function getThresholdStep(uiValue) {
    return stepByLevel(thresholdSteps, uiValue);
  }

  function getMinAreaStep(uiValue) {
    return stepByLevel(minAreaSteps, uiValue);
  }

  function setRuntimeFeedback(text, isError = false) {
    const color = isError ? "#ff89ad" : "";
    for (const el of [runtimeFeedback, runtimeFeedbackClassification]) {
      if (el) {
        el.textContent = text;
        el.style.color = color;
      }
    }
  }

  function applyRuntimeConfigToForm(config) {
    if (cfgMotionThreshold) {
      cfgMotionThreshold.min = "1";
      cfgMotionThreshold.max = "3";
      cfgMotionThreshold.step = "1";
    }
    if (cfgMotionMinArea) {
      cfgMotionMinArea.min = "1";
      cfgMotionMinArea.max = "3";
      cfgMotionMinArea.step = "1";
    }
    if (cfgClassificationBackend && !cfgClassificationBackend.value) {
      cfgClassificationBackend.value = "local";
    }
    if (cfgClassificationSamplePolicy && !cfgClassificationSamplePolicy.value) {
      cfgClassificationSamplePolicy.value = "event_cover";
    }
    if (cfgClassificationMinConfidence && !cfgClassificationMinConfidence.value) {
      cfgClassificationMinConfidence.value = "0.55";
    }

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
    updateClassificationControls();
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
    // Classification master toggle depends on motion being active.
    if (cfgClassificationEnabled) {
      cfgClassificationEnabled.disabled = !active;
    }
    if (runtimeSave) {
      runtimeSave.disabled = !active;
    }
    if (runtimeSaveClassification) {
      runtimeSaveClassification.disabled = !active;
    }
    // Classification sub-options cascade off both motion AND the classification toggle.
    updateClassificationControls(active);
  }

  function updateClassificationControls(motionActive = Boolean(monitoringEnabled)) {
    // Hierarchy: motion ON -> classification ON -> choose person/pet. A sub-option is
    // editable only when both motion and classification are active.
    const classOn =
      Boolean(motionActive) && Boolean(cfgClassificationEnabled && cfgClassificationEnabled.checked);
    for (const el of [
      cfgClassificationDetectPerson,
      cfgClassificationDetectPet,
      cfgClassificationBackend,
      cfgClassificationMinConfidence,
      cfgClassificationSamplePolicy,
    ]) {
      if (el) {
        el.disabled = !classOn;
      }
    }
  }

  function snapRangeToStep(element) {
    if (!element) {
      return;
    }
    const numeric = parseInt(element.value, 10);
    if (!Number.isFinite(numeric)) {
      return;
    }
    element.value = String(Math.max(1, Math.min(3, Math.round(numeric))));
  }

  function updateSliderHints() {
    if (cfgMotionThreshold && cfgMotionThresholdHint) {
      const thresholdStep = getThresholdStep(cfgMotionThreshold.value);
      if (thresholdStep) {
        cfgMotionThresholdHint.textContent = thresholdStep.hint;
        if (cfgMotionThresholdValue) {
          cfgMotionThresholdValue.textContent =
            `Scenario ${thresholdStep.level}/3 · ${thresholdStep.title} · Valore reale ${thresholdStep.value}`;
        }
      }
    }
    if (cfgMotionMinArea && cfgMotionMinAreaHint) {
      const minAreaStep = getMinAreaStep(cfgMotionMinArea.value);
      if (minAreaStep) {
        cfgMotionMinAreaHint.textContent = minAreaStep.hint;
        if (cfgMotionMinAreaValue) {
          cfgMotionMinAreaValue.textContent =
            `Scenario ${minAreaStep.level}/3 · ${minAreaStep.title} · Valore reale ${minAreaStep.value} px`;
        }
      }
    }
    if (cfgClassificationMinConfidenceValue && cfgClassificationMinConfidence) {
      const confidence = Number.parseFloat(cfgClassificationMinConfidence.value);
      const normalized = Number.isFinite(confidence) ? confidence : 0.55;
      cfgClassificationMinConfidenceValue.textContent =
        `Soglia minima ${normalized.toFixed(2)}`;
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
    if (runtimeSaveClassification) {
      runtimeSaveClassification.disabled = true;
    }
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
      if (runtimeSaveClassification) {
        runtimeSaveClassification.disabled = false;
      }
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
      clearTimeout(previewTimer);
      previewTimer = null;
    }
    previewLoadedFrames = [];
    previewAnimationToken += 1;
  }

  function setPreviewBadge(text) {
    if (!motionPreviewBadge) {
      return;
    }
    motionPreviewBadge.textContent = text;
  }

  function hidePreviewVideo() {
    if (!motionCaptureVideo) {
      return;
    }
    motionCaptureVideo.pause?.();
    motionCaptureVideo.removeAttribute("src");
    motionCaptureVideo.load?.();
    motionCaptureVideo.hidden = true;
    // CSS sets `.motion-capture-frame video { display: none }`; an inline style is
    // required to actually hide/show it (the `hidden` attribute alone is overridden).
    motionCaptureVideo.style.display = "none";
  }

  function showEmptyPreview(message = "Seleziona un evento") {
    selectedCaptureId = null;
    selectedCapturePinned = false;
    stopPreviewAnimation();
    hidePreviewVideo();
    motionCapturePath.textContent = message;
    motionCaptureImage.removeAttribute("src");
    motionCaptureImage.style.display = "none";
    motionCaptureEmpty.textContent = message;
    motionCaptureEmpty.style.display = "grid";
    setPreviewBadge("Nessun evento selezionato");
  }

  function buildPreviewUrl(url) {
    const separator = url.includes("?") ? "&" : "?";
    return `${url}${separator}preview_ts=${Date.now()}`;
  }

  function preloadPreviewFrames(frames, animationToken) {
    return Promise.all(
      frames.map(
        (frameUrl) =>
          new Promise((resolve) => {
            const image = new Image();
            image.decoding = "async";
            const resolvedUrl = buildPreviewUrl(frameUrl);
            image.onload = () => resolve(resolvedUrl);
            image.onerror = () => resolve(resolvedUrl);
            image.src = resolvedUrl;
          }),
      ),
    ).then((loadedFrames) => {
      if (animationToken !== previewAnimationToken) {
        return [];
      }
      return loadedFrames;
    });
  }

  function renderPreviewFrame(frameUrl) {
    if (!frameUrl) {
      return;
    }
    motionCaptureImage.src = frameUrl;
    motionCaptureImage.style.opacity = "1";
  }

  function startPreviewAnimation(frames) {
    stopPreviewAnimation();
    previewFrames = frames || [];
    previewFrameIndex = 0;
    const animationToken = previewAnimationToken;

    if (previewFrames.length === 0) {
      setPreviewBadge("Nessun frame");
      return;
    }

    setPreviewBadge(previewFrames.length === 1 ? "Frame singolo" : `${previewFrames.length} frame`);
    motionCaptureImage.style.opacity = "0";

    preloadPreviewFrames(previewFrames, animationToken).then((loadedFrames) => {
      if (animationToken !== previewAnimationToken || loadedFrames.length === 0) {
        return;
      }

      previewLoadedFrames = loadedFrames;
      renderPreviewFrame(previewLoadedFrames[0]);

      if (previewLoadedFrames.length === 1) {
        return;
      }

      const frameDurationMs = 180;
      const tick = () => {
        if (animationToken !== previewAnimationToken || previewLoadedFrames.length <= 1) {
          return;
        }
        previewFrameIndex = (previewFrameIndex + 1) % previewLoadedFrames.length;
        renderPreviewFrame(previewLoadedFrames[previewFrameIndex]);
        previewTimer = setTimeout(tick, frameDurationMs);
      };

      previewTimer = setTimeout(tick, frameDurationMs);
    });
  }

  function formatCaptureLabel(capture) {
    const classLabel = capture?.classification?.class_label;
    const classBadge = classLabel && classLabel !== "unknown"
      ? ` · ${classLabel}`
      : "";
    return `${capture.label} · ${capture.frame_count} frame${classBadge}`;
  }

  function showCapture(capture, options = {}) {
    const animate =
      options.animate
      ?? (
        !capture.isLivePreview
        && Array.isArray(capture.frames)
        && capture.frames.length > 1
      );
    const pinSelection = options.pinSelection ?? false;
    selectedCaptureId = capture.id;
    selectedCapturePinned = pinSelection;
    motionCaptureEmpty.style.display = "none";
    motionCapturePath.textContent = formatCaptureLabel(capture);

    if (motionCaptureVideo && capture.video_url) {
      // Prefer the recorded MP4 clip when available.
      stopPreviewAnimation();
      motionCaptureImage.style.display = "none";
      motionCaptureImage.removeAttribute("src");
      motionCaptureVideo.hidden = false;
      motionCaptureVideo.style.display = "block";
      // Avoid reloading the same clip on every live poll (it would restart playback).
      if (!motionCaptureVideo.currentSrc.endsWith(capture.video_url)) {
        motionCaptureVideo.src = capture.video_url;
        motionCaptureVideo.load?.();
      }
      setPreviewBadge("Video");
    } else {
      hidePreviewVideo();
      motionCaptureImage.style.display = "block";
      const playbackFrames = animate ? capture.frames : [capture.url];
      startPreviewAnimation(playbackFrames);
    }

    document.querySelectorAll(".capture-row").forEach((row) => {
      row.classList.toggle("selected", row.dataset.captureId === capture.id);
    });
  }

  async function openCapture(capture) {
    try {
      const { response, data } = await fetchJson(`/motion_event/${capture.id}`);
      if (response.ok) {
        showCapture(data, { animate: true, pinSelection: true });
        return;
      }
    } catch {
    }

    showCapture(capture, { animate: false, pinSelection: true });
  }

  async function openLatestCapture(eventId, fallbackCapture, refreshKey) {
    latestPreviewRequestKey = refreshKey;
    try {
      const { response, data } = await fetchJson(`/motion_event/${eventId}?ts=${Date.now()}`);
      if (latestPreviewRequestKey !== refreshKey) {
        return;
      }
      if (response.ok && data?.id === eventId) {
        showCapture(data, { animate: false, pinSelection: false });
        return;
      }
    } catch {
    }

    if (latestPreviewRequestKey === refreshKey) {
      showCapture(fallbackCapture, { animate: false, pinSelection: false });
    }
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
    const classLabel = capture?.classification?.class_label;
    if (classLabel && classLabel !== "unknown") {
      detail.textContent = `${capture.frame_count} frame · ${classLabel}`;
    } else {
      detail.textContent = `${capture.frame_count} frame`;
    }
    meta.appendChild(detail);

    button.appendChild(meta);

    button.addEventListener("click", async () => {
      await openCapture(capture);
    });

    return button;
  }

  function captureEventCategory(capture) {
    // Prefer the explicit category from the API; fall back to motion.
    return capture?.category || capture?.classification?.detected_label || "movimento";
  }

  function captureFilterLabel() {
    const labels = {
      persona: "Persone",
      animale_domestico: "Animali",
      movimento: "Solo movimento",
    };
    return labels[captureCategoryFilter] || "Tutti";
  }

  async function refreshCaptureList() {
    if (refreshCaptureListInFlight) {
      return refreshCaptureListInFlight;
    }

    refreshCaptureListInFlight = (async () => {
    const limit = archiveExpanded ? expandedCaptureLimit : collapsedCaptureLimit;

    try {
      const { data } = await fetchJson(`/motion_captures?limit=${limit}&ts=${Date.now()}`);
      const allCaptures = data.captures || [];
      const total = data.total || allCaptures.length;
      const captures =
        captureCategoryFilter === "all"
          ? allCaptures
          : allCaptures.filter((capture) => captureEventCategory(capture) === captureCategoryFilter);
      const signature = `${archiveExpanded ? "full" : "compact"}::${captureCategoryFilter}::${captures
        .map((capture) => capture.id)
        .join("|")}::${total}`;

      const filterSuffix =
        captureCategoryFilter === "all" ? "" : ` · filtro: ${captureFilterLabel()}`;
      captureSummary.textContent = archiveExpanded
        ? `Archivio completo · ${total} eventi · scorri elenco${filterSuffix}`
        : `Ultimi ${Math.min(collapsedCaptureLimit, total)} eventi${filterSuffix}`;
      captureToggle.style.display = total > collapsedCaptureLimit ? "inline-flex" : "none";
      captureToggle.textContent = archiveExpanded ? "Mostra meno" : "Mostra altri";
      captureList.style.maxHeight = `${capturePanelHeight}px`;
      if (captureClear) {
        captureClear.disabled = clearingCaptures || total === 0;
      }

      if (signature === lastCaptureSignature && captures.length > 0) {
        return;
      }

      lastCaptureSignature = signature;
      captureList.innerHTML = "";
      captureList.scrollTop = 0;

      if (captures.length === 0) {
        latestCaptureId = null;
        showEmptyPreview("Nessun evento");
        captureList.innerHTML =
          captureCategoryFilter === "all"
            ? '<p class="capture-list-empty">Nessun evento salvato ancora</p>'
            : `<p class="capture-list-empty">Nessun evento per il filtro "${captureFilterLabel()}"</p>`;
        return;
      }

      if (!selectedCaptureId) {
        showEmptyPreview("Seleziona un evento");
      }

      for (const [index, capture] of captures.entries()) {
        const button = buildCaptureRow(capture);

        if (index === 0) {
          latestCaptureId = capture.id;
        }

        if (capture.id === selectedCaptureId) {
          button.classList.add("selected");
        }

        captureList.appendChild(button);
      }
    } catch {
      captureList.innerHTML =
        '<p class="capture-list-empty">Impossibile leggere l\'archivio eventi</p>';
    } finally {
      refreshCaptureListInFlight = null;
    }
    })();

    return refreshCaptureListInFlight;
  }

  async function clearAllCaptures() {
    if (!captureClear || clearingCaptures || !window.confirm("Cancellare tutti gli eventi salvati?")) {
      return;
    }

    clearingCaptures = true;
    captureClear.disabled = true;
    setRuntimeFeedback("Cancellazione archivio in corso...");

    try {
      const { response, data } = await fetchJson("/api/motion_captures", {
        method: "DELETE",
      });
      if (!response.ok) {
        setRuntimeFeedback(data.error || "Errore cancellazione archivio", true);
        return;
      }

      lastCaptureSignature = "";
      latestCaptureId = null;
      showEmptyPreview("Nessun evento");
      captureList.innerHTML = '<p class="capture-list-empty">Nessun evento salvato ancora</p>';
      captureSummary.textContent = "Archivio svuotato";
      setRuntimeFeedback(`Archivio cancellato (${data.removed ?? 0} elementi)`);
      await refreshMotionStatus();
      await refreshCaptureList();
    } catch {
      setRuntimeFeedback("Errore di rete durante cancellazione archivio", true);
    } finally {
      clearingCaptures = false;
      if (captureClear) {
        captureClear.disabled = false;
      }
    }
  }

  async function openCapturesFolder() {
    if (!captureOpenFolder) {
      return;
    }
    captureOpenFolder.disabled = true;
    try {
      const { response, data } = await fetchJson("/api/open_captures_folder", {
        method: "POST",
      });
      if (!response.ok) {
        setRuntimeFeedback(data.error || "Impossibile aprire la cartella", true);
        return;
      }
      setRuntimeFeedback("Cartella clip aperta nel file manager");
    } catch {
      setRuntimeFeedback("Errore di rete durante apertura cartella", true);
    } finally {
      captureOpenFolder.disabled = false;
    }
  }

  async function refreshMotionStatus() {
    try {
      const { data } = await fetchJson(`${feedBase}/motion_status?ts=${Date.now()}`);
      monitoringEnabled = Boolean(data.enabled);

      motionThreshold.textContent = data.threshold ?? "-";
      motionCurrentArea.textContent = data.current_area ?? 0;
      motionLastTriggerArea.textContent = data.last_trigger_area ?? 0;
      motionLastEvent.textContent = data.last_motion_at || "-";

      if (isActiveView && data.last_capture_path) {
        const refreshKey = `${data.last_event_id || ""}::${data.last_capture_path || ""}`;
        if (refreshKey && refreshKey !== lastMotionListRefreshKey) {
          lastMotionListRefreshKey = refreshKey;
          await refreshCaptureList();
        }
      } else {
        lastMotionListRefreshKey = "";
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
    if (cfgMotionThreshold) {
      cfgMotionThreshold.min = "1";
      cfgMotionThreshold.max = "3";
      cfgMotionThreshold.step = "1";
    }
    if (cfgMotionMinArea) {
      cfgMotionMinArea.min = "1";
      cfgMotionMinArea.max = "3";
      cfgMotionMinArea.step = "1";
    }
    if (cfgClassificationMinConfidence) {
      cfgClassificationMinConfidence.min = "0.3";
      cfgClassificationMinConfidence.max = "0.99";
      cfgClassificationMinConfidence.step = "0.01";
    }

    captureToggle.addEventListener("click", async () => {
      archiveExpanded = !archiveExpanded;
      lastCaptureSignature = "";
      captureList.scrollTop = 0;
      await refreshCaptureList();
    });
    if (captureFilter) {
      captureFilter.addEventListener("change", async () => {
        captureCategoryFilter = captureFilter.value || "all";
        lastCaptureSignature = "";
        captureList.scrollTop = 0;
        await refreshCaptureList();
      });
    }
    if (captureClear) {
      captureClear.addEventListener("click", async () => {
        await clearAllCaptures();
      });
    }
    if (captureOpenFolder) {
      captureOpenFolder.addEventListener("click", async () => {
        await openCapturesFolder();
      });
    }
    if (runtimeSave) {
      runtimeSave.addEventListener("click", async () => {
        await saveRuntimeConfig();
      });
    }
    if (runtimeSaveClassification) {
      runtimeSaveClassification.addEventListener("click", async () => {
        await saveRuntimeConfig();
      });
    }
    if (cfgMotionEnabled) {
      cfgMotionEnabled.addEventListener("change", async () => {
        await saveMotionEnabled(cfgMotionEnabled.checked);
      });
    }
    if (cfgMotionThreshold) {
      cfgMotionThreshold.addEventListener("input", () => {
        snapRangeToStep(cfgMotionThreshold);
        markRuntimeDirty();
        updateSliderHints();
      });
      cfgMotionThreshold.addEventListener("change", updateSliderHints);
    }
    if (cfgMotionMinArea) {
      cfgMotionMinArea.addEventListener("input", () => {
        snapRangeToStep(cfgMotionMinArea);
        markRuntimeDirty();
        updateSliderHints();
      });
      cfgMotionMinArea.addEventListener("change", updateSliderHints);
    }
    if (cfgClassificationEnabled) {
      cfgClassificationEnabled.addEventListener("change", () => {
        markRuntimeDirty();
        updateClassificationControls();
      });
    }
    if (cfgClassificationDetectPerson) {
      cfgClassificationDetectPerson.addEventListener("change", markRuntimeDirty);
    }
    if (cfgClassificationDetectPet) {
      cfgClassificationDetectPet.addEventListener("change", markRuntimeDirty);
    }
    if (cfgClassificationBackend) {
      cfgClassificationBackend.addEventListener("change", markRuntimeDirty);
    }
    if (cfgClassificationSamplePolicy) {
      cfgClassificationSamplePolicy.addEventListener("change", markRuntimeDirty);
    }
    if (cfgNotifyTelegramEnabled) {
      cfgNotifyTelegramEnabled.addEventListener("change", async () => {
        const value = cfgNotifyTelegramEnabled.checked;
        cfgNotifyTelegramEnabled.disabled = true;
        setRuntimeFeedback("Aggiornamento notifiche Telegram...");
        try {
          const { response, data } = await fetchJson("/api/runtime_config", {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ updates: { NOTIFY_TELEGRAM_ENABLED: value } }),
          });
          if (!response.ok) {
            cfgNotifyTelegramEnabled.checked = !value;
            setRuntimeFeedback(data.error || "Errore aggiornamento notifiche", true);
            return;
          }
          applyRuntimeConfigToForm(data.config || {});
          runtimeDirty = false;
          setRuntimeFeedback(value ? "Notifiche Telegram abilitate" : "Notifiche Telegram disabilitate");
        } catch {
          cfgNotifyTelegramEnabled.checked = !value;
          setRuntimeFeedback("Errore di rete durante aggiornamento notifiche", true);
        } finally {
          cfgNotifyTelegramEnabled.disabled = false;
        }
      });
    }
    if (cfgClassificationMinConfidence) {
      cfgClassificationMinConfidence.addEventListener("input", () => {
        markRuntimeDirty();
        updateSliderHints();
      });
      cfgClassificationMinConfidence.addEventListener("change", updateSliderHints);
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
