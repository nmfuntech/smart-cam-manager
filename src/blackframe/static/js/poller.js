// Poller adattivo condiviso (estratto da main.js): tre modalità in base
// all'attività dell'utente e alla visibilità della tab.
//
// - fast: finestra breve dopo un input (pointer/tastiera/wheel/touch);
// - default: tab visibile ma utente fermo;
// - hidden: tab in background — su hardware esiguo è la modalità che conta,
//   ogni richiesta risparmiata è CPU per la pipeline camera.
//
// tasks: { chiave: { run: fn, intervals: { fast, default, hidden } } }.
// getIntervalOverride(chiave, baseMs, mode) opzionale permette al consumer
// di allungare dinamicamente un intervallo (es. motion disabilitato).
// Si ferma da solo su blackframe:auth-required.

export const POLL_MODE = {
  FAST: "fast",
  DEFAULT: "default",
  HIDDEN: "hidden",
};

export function createAdaptivePoller({
  tasks,
  fastWindowMs = 12000,
  modeCheckMs = 500,
  initialFastMs = 4000,
  getIntervalOverride = null,
}) {
  let fastUntil = Date.now() + initialFastMs;
  let activeMode = null;
  let handles = [];
  let stopped = false;

  function currentMode() {
    if (document.hidden) {
      return POLL_MODE.HIDDEN;
    }
    if (Date.now() < fastUntil) {
      return POLL_MODE.FAST;
    }
    return POLL_MODE.DEFAULT;
  }

  function intervalFor(key, mode) {
    const spec = tasks[key];
    const baseMs = spec?.intervals?.[mode] ?? spec?.intervals?.[POLL_MODE.DEFAULT];
    if (!Number.isFinite(baseMs) || baseMs <= 0) {
      return 0;
    }
    if (getIntervalOverride) {
      const overridden = getIntervalOverride(key, baseMs, mode);
      if (Number.isFinite(overridden) && overridden > 0) {
        return overridden;
      }
    }
    return baseMs;
  }

  function clearHandles() {
    handles.forEach((handle) => clearInterval(handle));
    handles = [];
  }

  function applyMode(mode) {
    clearHandles();
    if (stopped) {
      return;
    }
    activeMode = mode;
    for (const [key, spec] of Object.entries(tasks)) {
      const intervalMs = intervalFor(key, mode);
      if (intervalMs > 0) {
        handles.push(setInterval(spec.run, intervalMs));
      }
    }
  }

  function refreshMode() {
    const mode = currentMode();
    if (mode !== activeMode) {
      applyMode(mode);
    }
  }

  function markUserActive() {
    if (document.hidden || stopped) {
      return;
    }
    fastUntil = Date.now() + fastWindowMs;
    refreshMode();
  }

  function start() {
    applyMode(currentMode());
    handles.push(setInterval(refreshMode, modeCheckMs));
  }

  function stop() {
    stopped = true;
    clearHandles();
  }

  // Ricalcola subito gli intervalli (es. dopo un cambio di stato che
  // influenza getIntervalOverride).
  function reapply() {
    if (activeMode && !stopped) {
      applyMode(activeMode);
    }
  }

  window.addEventListener("blackframe:auth-required", stop);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      fastUntil = Date.now() + initialFastMs;
    }
    refreshMode();
  });
  ["pointerdown", "keydown", "wheel", "touchstart"].forEach((eventName) => {
    window.addEventListener(eventName, markUserActive, { passive: true });
  });

  return { start, stop, reapply, getMode: () => activeMode };
}
