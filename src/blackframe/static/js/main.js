import { createLiveController } from "./live-ui.js";
import { createMotionController } from "./motion-ui.js";
import { createPtzController } from "./ptz-ui.js";
import { createCameraConfigController } from "./camera-ui.js";
import { createAdaptivePoller } from "./poller.js";

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
  runtimeFeedback: document.getElementById("runtime-feedback"),
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

// A monitored secondary camera renders a live-only viewer: no PTZ, settings,
// events gallery, so their controllers are not wired up. La configurazione
// Telegram vive in /impostazioni.
const isActiveView = document.body.dataset.activeView !== "false";

if (isActiveView) {
  motion.bind();
  ptz.bind();
  cameras.bind();
  bindMotionOverlayToggle();
}

let motionEnabled = true;

async function pollMotionStatus() {
  const enabled = await motion.refreshMotionStatus();
  syncMotionEnabledState(enabled);
  if (typeof enabled === "boolean") {
    renderMotionOverlayState(enabled);
  }
}

const pollerTasks = {
  streamStatus: {
    run: () => live.refreshStatus(),
    intervals: { fast: 2000, default: 4000, hidden: 20000 },
  },
  motionStatus: {
    run: pollMotionStatus,
    intervals: { fast: 2000, default: 5000, hidden: 15000 },
  },
};

if (isActiveView) {
  pollerTasks.runtimeConfig = {
    run: () => motion.refreshRuntimeConfig(),
    intervals: { fast: 8000, default: 20000, hidden: 30000 },
  };
  pollerTasks.captureList = {
    run: () => motion.refreshCaptureList(),
    intervals: { fast: 5000, default: 10000, hidden: 30000 },
  };
  pollerTasks.ptzStatus = {
    run: () => ptz.refreshPtzStatus(),
    intervals: { fast: 6000, default: 12000, hidden: 30000 },
  };
}

const poller = createAdaptivePoller({
  tasks: pollerTasks,
  // Con il motion disabilitato lo stato e la lista eventi cambiano di rado:
  // gli intervalli raddoppiano.
  getIntervalOverride: (key, baseMs) => {
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
  },
});

function syncMotionEnabledState(enabled) {
  if (typeof enabled !== "boolean" || enabled === motionEnabled) {
    return;
  }
  motionEnabled = enabled;
  poller.reapply();
}

async function bootstrap() {
  live.ensureVideoFeed();
  await live.refreshStatus();
  await pollMotionStatus();
  if (isActiveView) {
    await motion.refreshRuntimeConfig();
    await motion.refreshCaptureList();
    await ptz.refreshPtzStatus();
    await cameras.refresh();
  }
  poller.start();
}

bootstrap();
