import { fetchJson } from "./api.js";
import { setPillState } from "./ui.js";
import { createAdaptivePoller } from "./poller.js";

function tiles() {
  return Array.from(document.querySelectorAll(".camera-tile[data-profile-id]"));
}

async function refreshTile(tile) {
  const profileId = tile.dataset.profileId;
  const pill = tile.querySelector('[data-role="motion-pill"]');
  if (!profileId || !pill) {
    return;
  }
  try {
    const { response, data } = await fetchJson(
      `/cam/${encodeURIComponent(profileId)}/motion_status?ts=${Date.now()}`,
    );
    if (!response.ok) {
      setPillState(pill, "Motion: offline", "error");
      return;
    }
    if (!data.enabled) {
      setPillState(pill, "Motion: off", "");
      return;
    }
    if (data.motion_detected) {
      setPillState(pill, "Motion: rilevato", "active");
    } else {
      setPillState(pill, "Motion: attivo", "ok");
    }
  } catch {
    setPillState(pill, "Motion: errore", "error");
  }
}

function refreshSnapshot(tile) {
  const profileId = tile.dataset.profileId;
  const image = tile.querySelector('[data-role="tile-snapshot"]');
  if (!profileId || !image) {
    return;
  }
  image.src = `/cam/${encodeURIComponent(profileId)}/snapshot.jpg?ts=${Date.now()}`;
}

// Refresh SEQUENZIALE dei tile: con piu' camere un Promise.all concentra le
// richieste nello stesso istante — spalmarle costa nulla e alleggerisce il
// server single-process del mini PC.
async function refreshAllStatus() {
  for (const tile of tiles()) {
    await refreshTile(tile);
  }
}

function refreshAllSnapshots() {
  for (const tile of tiles()) {
    refreshSnapshot(tile);
  }
}

for (const tile of tiles()) {
  const image = tile.querySelector('[data-role="tile-snapshot"]');
  const pill = tile.querySelector('[data-role="motion-pill"]');
  image?.addEventListener("error", () => {
    if (pill) {
      setPillState(pill, "Camera: offline", "error");
    }
  });
}

const poller = createAdaptivePoller({
  tasks: {
    tileStatus: {
      run: refreshAllStatus,
      intervals: { fast: 4000, default: 5000, hidden: 25000 },
    },
    // In background niente snapshot: si aggiornano al ritorno sulla tab
    // (intervallo hidden assente = task sospeso).
    tileSnapshots: {
      run: refreshAllSnapshots,
      intervals: { fast: 4000, default: 5000, hidden: 0 },
    },
  },
});

refreshAllStatus();
poller.start();
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) {
    refreshAllSnapshots();
  }
});
