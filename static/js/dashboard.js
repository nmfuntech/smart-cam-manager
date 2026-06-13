import { fetchJson } from "./api.js";
import { setPillState } from "./ui.js";

// Poll per-camera motion status and reflect it on each tile's pill.
const POLL_INTERVAL_MS = 4000;

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

async function refreshAll() {
  await Promise.all(tiles().map(refreshTile));
}

refreshAll();
setInterval(refreshAll, POLL_INTERVAL_MS);
