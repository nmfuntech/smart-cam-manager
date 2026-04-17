import { createLiveController } from "./live-ui.js";
import { createMotionController } from "./motion-ui.js";
import { createPtzController } from "./ptz-ui.js";

const live = createLiveController({
  overlay: document.getElementById("viewer-overlay"),
  cameraFrame: document.getElementById("camera-frame"),
  streamPill: document.getElementById("stream-pill"),
});

const motion = createMotionController({
  motionPill: document.getElementById("motion-pill"),
  motionState: document.getElementById("motion-state"),
  motionLastEvent: document.getElementById("motion-last-event"),
  motionCurrentArea: document.getElementById("motion-current-area"),
  motionLastTriggerArea: document.getElementById("motion-last-trigger-area"),
  motionThreshold: document.getElementById("motion-threshold"),
  motionCapturePath: document.getElementById("motion-capture-path"),
  motionCaptureImage: document.getElementById("motion-capture-image"),
  motionCaptureEmpty: document.getElementById("motion-capture-empty"),
  captureList: document.getElementById("capture-list"),
  captureSummary: document.getElementById("capture-summary"),
  captureToggle: document.getElementById("capture-toggle"),
  runtimeSave: document.getElementById("runtime-save"),
  runtimeFeedback: document.getElementById("runtime-feedback"),
  cfgMotionEnabled: document.getElementById("cfg-motion-enabled"),
  cfgMotionThreshold: document.getElementById("cfg-motion-threshold"),
  cfgMotionThresholdHint: document.getElementById("cfg-motion-threshold-hint"),
  cfgMotionMinArea: document.getElementById("cfg-motion-min-area"),
  cfgMotionMinAreaHint: document.getElementById("cfg-motion-min-area-hint"),
});

const ptz = createPtzController({
  ptzMessage: document.getElementById("ptz-message"),
  ptzPill: document.getElementById("ptz-pill"),
  ptzStatusLabel: document.getElementById("ptz-status-label"),
  ptzHostLabel: document.getElementById("ptz-host-label"),
  ptzButtons: document.querySelectorAll(".ptz-btn"),
});

const motionOverlayToggle = document.getElementById("motion-overlay-toggle");

function renderMotionOverlayState(enabled) {
  if (!motionOverlayToggle) {
    return;
  }
  const isOn = Boolean(enabled);
  motionOverlayToggle.textContent = isOn ? "MOTION ON" : "MOTION OFF";
  motionOverlayToggle.classList.toggle("is-on", isOn);
  motionOverlayToggle.classList.toggle("is-off", !isOn);
  motionOverlayToggle.setAttribute("aria-pressed", String(isOn));
}

function bindMotionOverlayToggle() {
  if (!motionOverlayToggle) {
    return;
  }
  motionOverlayToggle.addEventListener("click", () => {
    const current = motion.getMonitoringEnabled();
    if (typeof current !== "boolean") {
      return;
    }
    motion.saveMotionEnabled(!current).then((updated) => {
      if (typeof updated === "boolean") {
        renderMotionOverlayState(updated);
      }
    });
  });
}

motion.bind();
ptz.bind();
bindMotionOverlayToggle();

const POLL_MODE = {
  FAST: "fast",
  DEFAULT: "default",
  HIDDEN: "hidden",
};

const FAST_WINDOW_MS = 12000;
const MODE_CHECK_MS = 500;

let fastUntil = Date.now() + 4000;
let activeMode = null;
let pollingHandles = [];
let motionEnabled = true;

const POLL_INTERVALS = {
  [POLL_MODE.FAST]: {
    streamStatus: 2000,
    snapshot: 700,
    motionStatus: 1500,
    runtimeConfig: 8000,
    captureList: 3000,
    ptzStatus: 4000,
  },
  [POLL_MODE.DEFAULT]: {
    streamStatus: 4000,
    snapshot: 1500,
    motionStatus: 3500,
    runtimeConfig: 12000,
    captureList: 6000,
    ptzStatus: 8000,
  },
  [POLL_MODE.HIDDEN]: {
    streamStatus: 15000,
    snapshot: 0,
    motionStatus: 12000,
    runtimeConfig: 30000,
    captureList: 20000,
    ptzStatus: 20000,
  },
};

function getCurrentMode() {
  if (document.hidden) {
    return POLL_MODE.HIDDEN;
  }
  if (Date.now() < fastUntil) {
    return POLL_MODE.FAST;
  }
  return POLL_MODE.DEFAULT;
}

function getInterval(mode, key) {
  const modeIntervals = POLL_INTERVALS[mode] || POLL_INTERVALS[POLL_MODE.DEFAULT];
  const baseMs = modeIntervals[key];
  if (!Number.isFinite(baseMs)) {
    return 0;
  }
  if (baseMs <= 0) {
    return 0;
  }
  if (key === "snapshot") {
    const streamIntervalMs = live.getSnapshotIntervalMs();
    if (Number.isFinite(streamIntervalMs) && streamIntervalMs > 0) {
      return Math.max(baseMs, streamIntervalMs);
    }
  }
  if (motionEnabled) {
    return baseMs;
  }
  if (key === "motionStatus") {
    return Math.max(baseMs * 2, 7000);
  }
  if (key === "captureList") {
    return Math.max(baseMs * 2, 12000);
  }
  return baseMs;
}

function clearPollingHandles() {
  pollingHandles.forEach((handle) => clearInterval(handle));
  pollingHandles = [];
}

function scheduleTask(taskKey, callback, mode) {
  const intervalMs = getInterval(mode, taskKey);
  if (intervalMs <= 0) {
    return;
  }
  pollingHandles.push(setInterval(callback, intervalMs));
}

function syncMotionEnabledState(enabled) {
  if (typeof enabled !== "boolean" || enabled === motionEnabled) {
    return;
  }
  motionEnabled = enabled;
  if (activeMode) {
    applyPollingMode(activeMode);
  }
}

function applyPollingMode(mode) {
  clearPollingHandles();
  activeMode = mode;

  scheduleTask("streamStatus", () => live.refreshStatus(), mode);
  scheduleTask("snapshot", () => live.refreshSnapshot(), mode);
  scheduleTask("motionStatus", async () => {
    const enabled = await motion.refreshMotionStatus();
    syncMotionEnabledState(enabled);
    if (typeof enabled === "boolean") {
      renderMotionOverlayState(enabled);
    }
  }, mode);
  scheduleTask("runtimeConfig", () => motion.refreshRuntimeConfig(), mode);
  scheduleTask("captureList", () => motion.refreshCaptureList(), mode);
  scheduleTask("ptzStatus", () => ptz.refreshPtzStatus(), mode);
}

function refreshPollingMode() {
  const mode = getCurrentMode();
  if (mode !== activeMode) {
    applyPollingMode(mode);
  }
}

function markUserActive() {
  if (document.hidden) {
    return;
  }
  fastUntil = Date.now() + FAST_WINDOW_MS;
  refreshPollingMode();
}

async function bootstrap() {
  await live.refreshStatus();
  live.refreshSnapshot();
  const enabled = await motion.refreshMotionStatus();
  syncMotionEnabledState(enabled);
  if (typeof enabled === "boolean") {
    renderMotionOverlayState(enabled);
  }
  await motion.refreshRuntimeConfig();
  await motion.refreshCaptureList();
  await ptz.refreshPtzStatus();

  applyPollingMode(getCurrentMode());
  setInterval(() => refreshPollingMode(), MODE_CHECK_MS);
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    fastUntil = Date.now() + 4000;
  }
  refreshPollingMode();
});

["pointerdown", "keydown", "wheel", "touchstart"].forEach((eventName) => {
  window.addEventListener(eventName, markUserActive, { passive: true });
});

bootstrap();
