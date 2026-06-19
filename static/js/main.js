import { createLiveController } from "./live-ui.js";
import { createMotionController } from "./motion-ui.js";
import { createPtzController } from "./ptz-ui.js";
import { createCameraConfigController } from "./camera-ui.js";
import { createTelegramController } from "./telegram-ui.js";

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
  motionPreviewBadge: document.getElementById("motion-preview-badge"),
  motionCaptureEmpty: document.getElementById("motion-capture-empty"),
  captureList: document.getElementById("capture-list"),
  captureSummary: document.getElementById("capture-summary"),
  captureFilter: document.getElementById("capture-filter"),
  captureToggle: document.getElementById("capture-toggle"),
  captureClear: document.getElementById("capture-clear"),
  captureOpenFolder: document.getElementById("capture-open-folder"),
  runtimeSave: document.getElementById("runtime-save"),
  runtimeSaveClassification: document.getElementById("runtime-save-classification"),
  runtimeFeedback: document.getElementById("runtime-feedback"),
  runtimeFeedbackClassification: document.getElementById("runtime-feedback-classification"),
  cfgMotionEnabled: document.getElementById("cfg-motion-enabled"),
  cfgMotionThreshold: document.getElementById("cfg-motion-threshold"),
  cfgMotionThresholdHint: document.getElementById("cfg-motion-threshold-hint"),
  cfgMotionThresholdValue: document.getElementById("cfg-motion-threshold-value"),
  cfgMotionMinArea: document.getElementById("cfg-motion-min-area"),
  cfgMotionMinAreaHint: document.getElementById("cfg-motion-min-area-hint"),
  cfgMotionMinAreaValue: document.getElementById("cfg-motion-min-area-value"),
  cfgClassificationEnabled: document.getElementById("cfg-classification-enabled"),
  cfgClassificationDetectPerson: document.getElementById("cfg-classification-detect-person"),
  cfgClassificationDetectPet: document.getElementById("cfg-classification-detect-pet"),
  cfgClassificationBackend: document.getElementById("cfg-classification-backend"),
  cfgClassificationMinConfidence: document.getElementById("cfg-classification-min-confidence"),
  cfgClassificationMinConfidenceValue: document.getElementById("cfg-classification-min-confidence-value"),
  cfgClassificationSamplePolicy: document.getElementById("cfg-classification-sample-policy"),
  cfgRecordEnabled: document.getElementById("cfg-record-enabled"),
  cfgNotifyTelegramEnabled: document.getElementById("cfg-notify-telegram-enabled"),
  motionCaptureVideo: document.getElementById("motion-capture-video"),
});

const ptz = createPtzController({
  ptzMessage: document.getElementById("ptz-message"),
  ptzPill: document.getElementById("ptz-pill"),
  ptzStatusLabel: document.getElementById("ptz-status-label"),
  ptzHostLabel: document.getElementById("ptz-host-label"),
  ptzButtons: document.querySelectorAll(".ptz-btn"),
});

const cameras = createCameraConfigController({
  wifiPill: document.getElementById("wifi-pill"),
  feedback: document.getElementById("camera-config-feedback"),
  activeSummary: document.getElementById("camera-active-summary"),
  profileList: document.getElementById("camera-profile-list"),
  form: document.getElementById("camera-form"),
  onApplied: async () => {
    live.ensureVideoFeed();
    await live.refreshStatus();
    await ptz.refreshPtzStatus();
  },
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

const telegram = createTelegramController({
  openButton: document.getElementById("telegram-config-open"),
  dialog: document.getElementById("telegram-dialog"),
  closeButton: document.getElementById("telegram-close"),
  tokenInput: document.getElementById("tg-bot-token"),
  tokenHint: document.getElementById("tg-token-hint"),
  chatIdInput: document.getElementById("tg-chat-id"),
  discoverButton: document.getElementById("tg-discover"),
  chatList: document.getElementById("tg-chat-list"),
  preferVideo: document.getElementById("tg-prefer-video"),
  enabled: document.getElementById("tg-enabled"),
  testButton: document.getElementById("tg-test"),
  saveButton: document.getElementById("tg-save"),
  feedback: document.getElementById("tg-feedback"),
  sidebarEnabledToggle: document.getElementById("cfg-notify-telegram-enabled"),
  inviteCodeInput: document.getElementById("tg-invite-code"),
  inviteHint: document.getElementById("tg-invite-hint"),
  inviteLinkBox: document.getElementById("tg-invite-link-box"),
  inviteLinkText: document.getElementById("tg-invite-link-text"),
  inviteCopyButton: document.getElementById("tg-invite-copy"),
});

// A monitored secondary camera renders a live-only viewer: no PTZ, settings,
// events gallery or Telegram dialog, so their controllers are not wired up.
const isActiveView = document.body.dataset.activeView !== "false";

if (isActiveView) {
  motion.bind();
  ptz.bind();
  cameras.bind();
  telegram.bind();
  bindMotionOverlayToggle();
}

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
    motionStatus: 2000,
    runtimeConfig: 8000,
    captureList: 5000,
    ptzStatus: 6000,
  },
  [POLL_MODE.DEFAULT]: {
    streamStatus: 4000,
    motionStatus: 5000,
    runtimeConfig: 20000,
    captureList: 10000,
    ptzStatus: 12000,
  },
  [POLL_MODE.HIDDEN]: {
    streamStatus: 20000,
    motionStatus: 15000,
    runtimeConfig: 30000,
    captureList: 30000,
    ptzStatus: 30000,
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

let authLost = false;

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
  if (authLost) {
    clearPollingHandles();
    return;
  }
  clearPollingHandles();
  activeMode = mode;

  scheduleTask("streamStatus", () => live.refreshStatus(), mode);
  scheduleTask("motionStatus", async () => {
    const enabled = await motion.refreshMotionStatus();
    syncMotionEnabledState(enabled);
    if (typeof enabled === "boolean") {
      renderMotionOverlayState(enabled);
    }
  }, mode);
  if (isActiveView) {
    scheduleTask("runtimeConfig", () => motion.refreshRuntimeConfig(), mode);
    scheduleTask("captureList", () => motion.refreshCaptureList(), mode);
    scheduleTask("ptzStatus", () => ptz.refreshPtzStatus(), mode);
  }
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
  if (authLost) {
    return;
  }
  live.ensureVideoFeed();
  await live.refreshStatus();
  const enabled = await motion.refreshMotionStatus();
  syncMotionEnabledState(enabled);
  if (typeof enabled === "boolean") {
    renderMotionOverlayState(enabled);
  }
  if (isActiveView) {
    await motion.refreshRuntimeConfig();
    await motion.refreshCaptureList();
    await ptz.refreshPtzStatus();
    await cameras.refresh();
  }

  applyPollingMode(getCurrentMode());
  setInterval(() => refreshPollingMode(), MODE_CHECK_MS);
}

window.addEventListener("blackframe:auth-required", () => {
  authLost = true;
  clearPollingHandles();
});

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
