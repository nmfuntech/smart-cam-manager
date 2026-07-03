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
    runtimeFeedback,
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
  let monitoringEnabled = null;
  let clearingCaptures = false;
  let captureCategoryFilter = "all";
  let lastMotionListRefreshKey = "";
  let refreshCaptureListInFlight = null;
  let latestPreviewRequestKey = "";

  const collapsedCaptureLimit = 5;
  const expandedCaptureLimit = 60;
  const capturePanelHeight = 560;

  // Toggle rapidi rimasti nel viewer dopo lo spostamento della configurazione
  // completa in /impostazioni: salvataggio immediato a chiave singola.
  const quickToggles = {
    RECORD_ENABLED: {
      element: cfgRecordEnabled,
      onText: "Registrazione video abilitata",
      offText: "Registrazione video disabilitata",
    },
    NOTIFY_TELEGRAM_ENABLED: {
      element: cfgNotifyTelegramEnabled,
      onText: "Notifiche Telegram abilitate",
      offText: "Notifiche Telegram disabilitate",
    },
  };

  function setRuntimeFeedback(text, isError = false) {
    if (!runtimeFeedback) {
      return;
    }
    runtimeFeedback.textContent = text;
    runtimeFeedback.style.color = isError ? "#ff89ad" : "";
  }

  function applyQuickToggles(config) {
    for (const [key, spec] of Object.entries(quickToggles)) {
      if (spec.element && typeof config[key] === "boolean") {
        spec.element.checked = config[key];
      }
    }
  }

  async function refreshRuntimeConfig() {
    try {
      const { data } = await fetchJson("/runtime_config");
      applyQuickToggles(data.config || {});
      if (typeof data.config?.MOTION_ENABLED === "boolean") {
        monitoringEnabled = data.config.MOTION_ENABLED;
      }
    } catch {
      setRuntimeFeedback("Impossibile leggere runtime config", true);
    }
  }

  async function saveQuickToggle(key, value) {
    const spec = quickToggles[key];
    const element = spec?.element;
    if (element) {
      element.disabled = true;
    }
    setRuntimeFeedback("Aggiornamento in corso...");
    try {
      const { response, data } = await fetchJson("/api/runtime_config", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ updates: { [key]: Boolean(value) } }),
      });
      if (!response.ok) {
        if (element) {
          element.checked = !value;
        }
        setRuntimeFeedback(data.error || "Errore aggiornamento", true);
        return null;
      }
      applyQuickToggles(data.config || {});
      setRuntimeFeedback(value ? spec.onText : spec.offText);
      return Boolean(value);
    } catch {
      if (element) {
        element.checked = !value;
      }
      setRuntimeFeedback("Errore di rete durante aggiornamento", true);
      return null;
    } finally {
      if (element) {
        element.disabled = false;
      }
    }
  }

  async function saveMotionEnabled(value) {
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
      applyQuickToggles(data.config || {});
      if (typeof data.config?.MOTION_ENABLED === "boolean") {
        monitoringEnabled = data.config.MOTION_ENABLED;
      } else {
        monitoringEnabled = Boolean(value);
      }
      setRuntimeFeedback("Monitoraggio aggiornato");
      await refreshMotionStatus();
      return monitoringEnabled;
    } catch {
      setRuntimeFeedback("Errore di rete durante aggiornamento", true);
      return null;
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
        return false;
      }

      if (data.motion_detected) {
        setPillState(motionPill, "Motion: live", "active");
        motionState.textContent = "Movimento in corso";
        return true;
      }

      setPillState(motionPill, "Motion: armed", "ok");
      motionState.textContent = data.error || "Monitoraggio attivo";
      return true;
    } catch {
      monitoringEnabled = null;
      setPillState(motionPill, "Motion: errore", "error");
      motionState.textContent = "Impossibile leggere lo stato motion";
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
    for (const [key, spec] of Object.entries(quickToggles)) {
      if (spec.element) {
        spec.element.addEventListener("change", async () => {
          await saveQuickToggle(key, spec.element.checked);
        });
      }
    }
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
