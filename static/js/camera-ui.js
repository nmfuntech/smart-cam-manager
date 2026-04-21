import { fetchJson } from "./api.js";
import { setPillState } from "./ui.js";

export function createCameraConfigController(elements) {
  const {
    wifiPill,
    feedback,
    activeSummary,
    profileList,
    form,
    onApplied,
    openViewerOnSave = false,
  } = elements;

  let submitting = false;

  function getSubmitButton() {
    return form?.querySelector('button[type="submit"]') || null;
  }

  function setFeedback(text, isError = false) {
    if (!feedback) {
      return;
    }
    feedback.textContent = text;
    feedback.style.color = isError ? "#ff89ad" : "";
  }

  function setSubmittingState(isSubmitting) {
    const submitButton = getSubmitButton();
    if (!submitButton) {
      return;
    }
    submitButton.disabled = isSubmitting;
    submitButton.textContent = isSubmitting ? "Salvataggio..." : "Salva camera";
  }

  function readFieldLabel(field) {
    const label = field?.closest("label");
    const labelText = label?.querySelector("span")?.textContent?.trim();
    return labelText || field?.name || "Campo";
  }

  function showFieldError(field) {
    if (!field) {
      return;
    }
    const label = readFieldLabel(field);
    const message = field.validationMessage || "Valore non valido";
    setFeedback(`${label}: ${message}`, true);
  }

  function renderWifi(wifi) {
    if (!wifiPill) {
      return;
    }
    if (wifi?.connected && wifi?.ssid) {
      setPillState(wifiPill, `Wi-Fi: ${wifi.ssid}`, "ok");
      return;
    }
    setPillState(wifiPill, "Wi-Fi: assente", "error");
  }

  function renderActiveSummary(activeProfileId, profiles) {
    if (!activeSummary) {
      return;
    }
    const active = profiles.find((profile) => profile.id === activeProfileId);
    activeSummary.textContent = active
      ? `Profilo attivo: ${active.name} · ${active.host}`
      : "Profilo attivo: nessuno";
  }

  function createProfileCard(profile, currentWifi) {
    const card = document.createElement("article");
    card.className = `camera-profile-card${profile.active ? " is-active" : ""}`;

    const top = document.createElement("div");
    top.className = "camera-profile-top";

    const name = document.createElement("strong");
    name.className = "camera-profile-name";
    name.textContent = profile.name;
    top.appendChild(name);

    const badgeWrap = document.createElement("div");
    badgeWrap.className = "camera-profile-actions";

    if (profile.active) {
      const activeBadge = document.createElement("span");
      activeBadge.className = "camera-badge active";
      activeBadge.textContent = "Attiva";
      badgeWrap.appendChild(activeBadge);
    }

    if (
      currentWifi?.ssid &&
      profile.wifi_ssid &&
      currentWifi.ssid.toLowerCase() === profile.wifi_ssid.toLowerCase()
    ) {
      const wifiBadge = document.createElement("span");
      wifiBadge.className = "camera-badge match";
      wifiBadge.textContent = "Wi-Fi compatibile";
      badgeWrap.appendChild(wifiBadge);
    }

    top.appendChild(badgeWrap);
    card.appendChild(top);

    const meta = document.createElement("div");
    meta.className = "camera-profile-meta";
    meta.textContent = [
      profile.host,
      `RTSP ${profile.rtsp_port}`,
      `ONVIF ${profile.onvif_port}`,
      profile.wifi_ssid ? `SSID ${profile.wifi_ssid}` : null,
    ]
      .filter(Boolean)
      .join(" · ");
    card.appendChild(meta);

    const actions = document.createElement("div");
    actions.className = "camera-profile-actions";

    const viewLink = document.createElement("a");
    viewLink.className = "ghost-button compact-button camera-profile-button";
    viewLink.href = profile.viewer_url || `/camera/${profile.id}`;
    viewLink.textContent = profile.active ? "Apri viewer attuale" : "Apri viewer";
    if (!profile.active) {
      viewLink.addEventListener("click", async (event) => {
        event.preventDefault();
        const opened = await activateProfile(profile.id, { openViewer: true });
        if (!opened) {
          viewLink.blur();
        }
      });
    }
    actions.appendChild(viewLink);

    const button = document.createElement("button");
    button.type = "button";
    button.className = "ghost-button compact-button camera-profile-button";
    button.textContent = profile.active ? "In uso" : "Attiva senza aprire";
    button.disabled = profile.active || submitting;
    button.addEventListener("click", async () => {
      await activateProfile(profile.id);
    });
    actions.appendChild(button);

    card.appendChild(actions);

    return card;
  }

  function renderProfiles(payload) {
    const profiles = payload?.profiles || [];
    profileList.innerHTML = "";
    renderActiveSummary(payload?.active_profile_id, profiles);
    if (!profiles.length) {
      const empty = document.createElement("div");
      empty.className = "camera-empty";
      empty.textContent = "Nessun profilo salvato";
      profileList.appendChild(empty);
      return;
    }
    profiles.forEach((profile) => {
      profileList.appendChild(createProfileCard(profile, payload.current_wifi));
    });
  }

  function readFormPayload() {
    const formData = new FormData(form);
    const username = String(formData.get("username") || "").trim();
    const password = String(formData.get("password") || "").trim();
    const onvifUsername = String(formData.get("onvif_username") || "").trim();
    const onvifPassword = String(formData.get("onvif_password") || "").trim();
    return {
      name: String(formData.get("name") || "").trim(),
      wifi_ssid: String(formData.get("wifi_ssid") || "").trim(),
      host: String(formData.get("host") || "").trim(),
      rtsp_port: Number(formData.get("rtsp_port") || 554),
      stream_path: String(formData.get("stream_path") || "stream1").trim(),
      username,
      password,
      onvif_port: Number(formData.get("onvif_port") || 2020),
      onvif_username: onvifUsername || username,
      onvif_password: onvifPassword || password,
      move_speed: Number(formData.get("move_speed") || 0.6),
      move_timeout: Number(formData.get("move_timeout") || 0.35),
      notes: String(formData.get("notes") || "").trim(),
      activate: true,
    };
  }

  async function refresh() {
    try {
      const { data } = await fetchJson("/api/cameras");
      renderWifi(data.current_wifi);
      renderProfiles(data);
      if (data.current_wifi?.ssid && !form.elements.wifi_ssid.value) {
        form.elements.wifi_ssid.value = data.current_wifi.ssid;
      }
      setFeedback("Profili camera caricati");
      return data;
    } catch {
      renderWifi(null);
      setFeedback("Impossibile leggere profili camera", true);
      return null;
    }
  }

  async function activateProfile(profileId, options = {}) {
    const { openViewer = false } = options;
    submitting = true;
    setFeedback(openViewer ? "Apertura viewer..." : "Attivazione camera...");
    try {
      const { response, data } = await fetchJson(`/api/cameras/${profileId}/activate`, {
        method: "POST",
      });
      if (!response.ok || !data.ok) {
        setFeedback(data.error || "Attivazione camera fallita", true);
        return false;
      }
      renderWifi(data.current_wifi);
      renderProfiles(data);
      setFeedback(openViewer ? "Camera attivata. Apertura viewer..." : "Camera attivata. Stream in riconnessione...");
      if (openViewer && data.profile?.viewer_url) {
        window.location.href = data.profile.viewer_url;
        return true;
      }
      if (typeof onApplied === "function") {
        onApplied();
      }
      return true;
    } catch {
      setFeedback("Errore rete durante attivazione camera", true);
      return false;
    } finally {
      submitting = false;
    }
  }

  function bind() {
    if (!form) {
      return;
    }
    form.noValidate = true;
    form.addEventListener(
      "invalid",
      (event) => {
        showFieldError(event.target);
      },
      true
    );
    form.addEventListener("input", () => {
      if (feedback?.style.color) {
        setFeedback("-");
      }
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      if (!form.checkValidity()) {
        const invalidField = form.querySelector(":invalid");
        showFieldError(invalidField);
        form.reportValidity();
        invalidField?.focus();
        return;
      }
      submitting = true;
      setSubmittingState(true);
      setFeedback("Salvataggio camera...");
      try {
        const { response, data } = await fetchJson("/api/cameras", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(readFormPayload()),
        });
        if (!response.ok || !data.ok) {
          setFeedback(data.error || "Salvataggio camera fallito", true);
          return;
        }
        renderWifi(data.current_wifi);
        renderProfiles(data);
        setFeedback("Camera salvata e attivata");
        if (openViewerOnSave && data.profile?.viewer_url) {
          window.location.href = data.profile.viewer_url;
          return;
        }
        if (typeof onApplied === "function") {
          onApplied();
        }
      } catch {
        setFeedback("Errore rete durante salvataggio camera", true);
      } finally {
        submitting = false;
        setSubmittingState(false);
      }
    });
  }

  return {
    bind,
    refresh,
    activateProfile,
  };
}
