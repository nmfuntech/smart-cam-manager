import { fetchJson, postJson } from "./api.js";
import { setPillState } from "./ui.js";

export function createPtzController(elements) {
  const {
    ptzMessage,
    ptzPill,
    ptzStatusLabel,
    ptzHostLabel,
    ptzButtons,
  } = elements;

  async function refreshPtzStatus() {
    try {
      const { data } = await fetchJson("/ptz_status");

      if (data.available) {
        setPillState(ptzPill, "PTZ: ready", "ok");
        ptzStatusLabel.textContent = "Operativo";
        ptzHostLabel.textContent = `${data.host}:${data.port}`;
        ptzMessage.textContent = "Controllo remoto disponibile";
        return;
      }

      setPillState(ptzPill, "PTZ: offline", "error");
      ptzStatusLabel.textContent = "Non disponibile";
      ptzHostLabel.textContent = `${data.host}:${data.port}`;
      ptzMessage.textContent = data.error || "Controllo PTZ non disponibile";
    } catch {
      setPillState(ptzPill, "PTZ: errore", "error");
      ptzStatusLabel.textContent = "Errore";
      ptzHostLabel.textContent = "-";
      ptzMessage.textContent = "Impossibile leggere lo stato PTZ";
    }
  }

  async function sendPtzRequest(path) {
    try {
      const { response, data } = await postJson(path);

      if (!response.ok || !data.ok) {
        ptzMessage.textContent = data.error || "Comando PTZ non riuscito";
        await refreshPtzStatus();
        return;
      }

      ptzMessage.textContent = "Comando PTZ inviato";
      await refreshPtzStatus();
    } catch {
      ptzMessage.textContent = "Errore di rete durante il comando PTZ";
    }
  }

  function bind() {
    ptzButtons.forEach((button) => {
      button.addEventListener("click", async () => {
        const action = button.dataset.action;
        const direction = button.dataset.direction;

        if (action === "home") {
          await sendPtzRequest("/api/ptz/home");
          return;
        }

        if (action === "stop") {
          await sendPtzRequest("/api/ptz/stop");
          return;
        }

        if (direction) {
          await sendPtzRequest(`/api/ptz/${direction}`);
        }
      });
    });
  }

  return {
    bind,
    refreshPtzStatus,
  };
}
