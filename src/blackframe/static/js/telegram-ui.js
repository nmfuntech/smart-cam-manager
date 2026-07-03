import { fetchJson } from "./api.js";

const JSON_HEADERS = { "Content-Type": "application/json" };

export function createTelegramController(elements) {
  const {
    openButton,
    dialog,
    closeButton,
    tokenInput,
    tokenHint,
    chatIdInput,
    discoverButton,
    chatList,
    preferVideo,
    enabled,
    testButton,
    saveButton,
    feedback,
    sidebarEnabledToggle,
    inviteCodeInput,
    inviteHint,
    inviteLinkBox,
    inviteLinkText,
    inviteCopyButton,
  } = elements;

  let hasSavedToken = false;

  function _updateInviteLink(code, botUsername) {
    if (code && botUsername) {
      const url = `https://t.me/${botUsername}?start=${code}`;
      if (inviteLinkText) inviteLinkText.textContent = url;
      if (inviteLinkBox) inviteLinkBox.hidden = false;
      if (inviteHint) inviteHint.textContent = "";
    } else if (code) {
      if (inviteLinkBox) inviteLinkBox.hidden = true;
      if (inviteHint) inviteHint.textContent = "Codice attivo. Il link sarà disponibile all'avvio del bot.";
    } else {
      if (inviteLinkBox) inviteLinkBox.hidden = true;
      if (inviteHint) inviteHint.textContent = "Senza codice solo tu puoi usare il bot.";
    }
  }

  function setFeedback(text, isError = false) {
    if (!feedback) {
      return;
    }
    feedback.textContent = text;
    feedback.style.color = isError ? "#ff89ad" : "";
  }

  // Token from the input if typed, otherwise empty (server keeps the saved one).
  function typedToken() {
    return (tokenInput?.value || "").trim();
  }

  function tokenAvailable() {
    return Boolean(typedToken() || hasSavedToken);
  }

  async function loadConfig() {
    try {
      const { data } = await fetchJson("/api/telegram_config");
      if (!data.ok) {
        return;
      }
      hasSavedToken = Boolean(data.has_token);
      if (tokenInput) {
        tokenInput.value = "";
      }
      if (tokenHint) {
        tokenHint.textContent = hasSavedToken
          ? "Token già salvato. Lascia vuoto per non modificarlo."
          : "Nessun token salvato.";
      }
      if (chatIdInput) {
        chatIdInput.value = data.chat_id || "";
      }
      if (preferVideo) {
        preferVideo.checked = Boolean(data.prefer_video);
      }
      if (enabled) {
        enabled.checked = Boolean(data.enabled);
      }
      if (inviteCodeInput) {
        inviteCodeInput.value = data.invite_code || "";
      }
      _updateInviteLink(data.invite_code || "", data.bot_username || "");
    } catch {
      setFeedback("Impossibile leggere la configurazione", true);
    }
  }

  function renderChats(chats) {
    if (!chatList) {
      return;
    }
    chatList.innerHTML = "";
    if (!chats || chats.length === 0) {
      chatList.hidden = true;
      return;
    }
    for (const chat of chats) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "tg-chat-row";
      button.textContent = `${chat.chat_id} · ${chat.label}`;
      button.addEventListener("click", () => {
        if (chatIdInput) {
          chatIdInput.value = String(chat.chat_id);
        }
        chatList.querySelectorAll(".tg-chat-row").forEach((row) => {
          row.classList.toggle("selected", row === button);
        });
      });
      chatList.appendChild(button);
    }
    chatList.hidden = false;
  }

  async function discover() {
    if (!tokenAvailable()) {
      setFeedback("Inserisci prima il token del bot", true);
      return;
    }
    discoverButton.disabled = true;
    setFeedback("Cerco le chat recenti del bot...");
    try {
      const { response, data } = await fetchJson("/api/telegram_discover", {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify({ bot_token: typedToken() }),
      });
      if (!response.ok || !data.ok) {
        setFeedback(data.error || "Ricerca chat fallita", true);
        renderChats([]);
        return;
      }
      if (!data.chats || data.chats.length === 0) {
        setFeedback("Nessuna chat trovata: scrivi al bot da Telegram e riprova", true);
        renderChats([]);
        return;
      }
      renderChats(data.chats);
      setFeedback(`Trovate ${data.chats.length} chat. Selezionane una.`);
    } catch {
      setFeedback("Errore di rete durante la ricerca", true);
    } finally {
      discoverButton.disabled = false;
    }
  }

  async function sendTest() {
    if (!tokenAvailable()) {
      setFeedback("Inserisci prima il token del bot", true);
      return;
    }
    const chatId = (chatIdInput?.value || "").trim();
    if (!chatId) {
      setFeedback("Inserisci o seleziona un chat ID", true);
      return;
    }
    testButton.disabled = true;
    setFeedback("Invio messaggio di prova...");
    try {
      const { response, data } = await fetchJson("/api/telegram_test", {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify({ bot_token: typedToken(), chat_id: chatId }),
      });
      if (!response.ok || !data.ok) {
        setFeedback(data.error || "Invio fallito", true);
        return;
      }
      setFeedback("✅ Messaggio inviato. Controlla Telegram.");
    } catch {
      setFeedback("Errore di rete durante l'invio", true);
    } finally {
      testButton.disabled = false;
    }
  }

  async function save() {
    const chatId = (chatIdInput?.value || "").trim();
    const wantEnabled = Boolean(enabled?.checked);
    if (wantEnabled && !tokenAvailable()) {
      setFeedback("Imposta il token del bot prima di abilitare", true);
      return;
    }
    if (wantEnabled && !chatId) {
      setFeedback("Imposta un chat ID prima di abilitare", true);
      return;
    }
    const payload = {
      chat_id: chatId,
      enabled: wantEnabled,
      prefer_video: Boolean(preferVideo?.checked),
      invite_code: (inviteCodeInput?.value || "").trim(),
    };
    const token = typedToken();
    if (token) {
      payload.bot_token = token;
    }
    saveButton.disabled = true;
    setFeedback("Salvataggio in corso...");
    try {
      const { response, data } = await fetchJson("/api/telegram_config", {
        method: "POST",
        headers: JSON_HEADERS,
        body: JSON.stringify(payload),
      });
      if (!response.ok || !data.ok) {
        setFeedback(data.error || "Salvataggio fallito", true);
        return;
      }
      setFeedback("Configurazione salvata su .env e applicata");
      if (sidebarEnabledToggle) {
        sidebarEnabledToggle.checked = wantEnabled;
      }
      await loadConfig();
    } catch {
      setFeedback("Errore di rete durante il salvataggio", true);
    } finally {
      saveButton.disabled = false;
    }
  }

  function open() {
    setFeedback("-");
    renderChats([]);
    loadConfig();
    if (dialog?.showModal) {
      dialog.showModal();
    } else if (dialog) {
      dialog.setAttribute("open", "");
    }
  }

  function close() {
    if (dialog?.close) {
      dialog.close();
    } else {
      dialog?.removeAttribute("open");
    }
  }

  function bind() {
    // Il controller funziona sia dentro un <dialog> (viewer) sia come
    // sezione inline (pagina Impostazioni): open/close esistono solo nel
    // primo caso.
    if (dialog && openButton) {
      openButton.addEventListener("click", open);
      closeButton?.addEventListener("click", close);
    }
    discoverButton?.addEventListener("click", discover);
    testButton?.addEventListener("click", sendTest);
    saveButton?.addEventListener("click", save);
    inviteCopyButton?.addEventListener("click", () => {
      const url = inviteLinkText?.textContent || "";
      if (!url) return;
      navigator.clipboard.writeText(url).then(() => {
        const orig = inviteCopyButton.textContent;
        inviteCopyButton.textContent = "Copiato!";
        setTimeout(() => { inviteCopyButton.textContent = orig; }, 1800);
      });
    });
  }

  return { bind, load: loadConfig };
}
