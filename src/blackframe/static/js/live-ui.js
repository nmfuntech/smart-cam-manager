import { fetchJson } from "./api.js";
import { setPillState } from "./ui.js";

export function createLiveController(elements) {
  const { overlay, cameraFrame, streamPill } = elements;
  // "" for the active camera, "/cam/<id>" when viewing a monitored secondary camera.
  const feedBase = document.body.dataset.feedBase || "";
  const OVERLAY_DEBOUNCE_MS = 500;
  let pendingOverlayTimer = null;
  let latestStatus = null;
  let feedInitialized = false;

  function clearPendingOverlayTimer() {
    if (!pendingOverlayTimer) {
      return;
    }
    clearTimeout(pendingOverlayTimer);
    pendingOverlayTimer = null;
  }

  function getPresentation(status) {
    if (status.connected) {
      return {
        pillText: "Stream: online",
        pillState: "ok",
        overlayText: "",
        showOverlay: false,
      };
    }

    if (status.connection_state === "connecting") {
      return {
        pillText: "Stream: connessione...",
        pillState: "active",
        overlayText: status.error || "Connessione stream in corso...",
        showOverlay: true,
      };
    }

    if (status.connection_state === "degraded") {
      return {
        pillText: "Stream: recupero",
        pillState: "active",
        overlayText: status.error || "Stream degradato, recupero in corso...",
        showOverlay: true,
      };
    }

    return {
      pillText: "Stream: offline",
      pillState: "error",
      overlayText: status.error || "Nessun frame disponibile",
      showOverlay: true,
    };
  }

  function renderDisconnected(presentation) {
    setPillState(streamPill, presentation.pillText, presentation.pillState);

    if (!presentation.showOverlay) {
      overlay.classList.add("hidden");
      overlay.textContent = "";
      return;
    }

    if (!overlay.classList.contains("hidden")) {
      overlay.textContent = presentation.overlayText;
      return;
    }

    clearPendingOverlayTimer();
    pendingOverlayTimer = setTimeout(() => {
      overlay.classList.remove("hidden");
      overlay.textContent = presentation.overlayText;
      pendingOverlayTimer = null;
    }, OVERLAY_DEBOUNCE_MS);
  }

  async function refreshStatus() {
    try {
      const { data } = await fetchJson(`${feedBase}/stream_status`);
      latestStatus = data;
      const presentation = getPresentation(data);

      if (!presentation.showOverlay) {
        clearPendingOverlayTimer();
        overlay.classList.add("hidden");
        overlay.textContent = "";
      } else {
        renderDisconnected(presentation);
      }
      setPillState(streamPill, presentation.pillText, presentation.pillState);
      return data;
    } catch {
      latestStatus = null;
      clearPendingOverlayTimer();
      overlay.classList.remove("hidden");
      overlay.textContent = "Impossibile leggere lo stato dello stream";
      setPillState(streamPill, "Stream: errore", "error");
      return null;
    }
  }

  function ensureVideoFeed() {
    if (feedInitialized || !cameraFrame) {
      return;
    }
    cameraFrame.src = `${feedBase}/video_feed`;
    feedInitialized = true;
  }

  return {
    ensureVideoFeed,
    refreshStatus,
    getStreamIntervalMs: () => latestStatus?.snapshot_interval_ms ?? null,
  };
}
