# Guida alla Configurazione: Accesso Remoto via WireGuard

Questa guida spiega come configurare e accedere all'applicazione **Blackframe (Cam Control)** da fuori rete locale in modo sicuro, utilizzando il protocollo WireGuard già presente sul tuo router.

## 1. Configurazione del Progetto (.env)

Per permettere al server di rispondere alle richieste provenienti dalla VPN o da altri dispositivi della rete, è necessario autorizzare l'ascolto su tutte le interfacce di rete.

Nel file `.env`, abbiamo aggiornato queste impostazioni:

```env
# Permette all'app di accettare connessioni da altri dispositivi (VPN/LAN)
APP_BIND_HOST=0.0.0.0
APP_PORT=8000
```

> [!IMPORTANT]
> Se `APP_BIND_HOST` è impostato su `127.0.0.1`, l'app sarà visibile **solo** dal computer su cui gira, bloccando ogni tentativo di accesso esterno anche se la VPN è attiva.

## 2. Configurazione WireGuard

### Sul Router
1. Assicurati che WireGuard sia attivo.
2. Nelle impostazioni del "Peer" (il tuo dispositivo), il campo **Allowed IPs** deve includere la sottorete del router, ad esempio: `192.168.1.0/24`. Questo permette al tuo dispositivo di "vedere" sia il computer che la camera.

### Sul Dispositivo (Mac/Smartphone)
1. Attiva la connessione WireGuard.
2. Un **handshake** recente e lo scambio di dati confermano che il tunnel sta funzionando.

## 3. Come Accedere (Indirizzi IP)

Dato che sei in VPN, puoi usare gli stessi indirizzi che useresti stando a casa:

| Scenario | Indirizzo da usare | Cosa stai raggiungendo |
| :--- | :--- | :--- |
| **Sullo stesso computer** | `http://localhost:8000` | L'app in esecuzione qui. |
| **Da altro disp. in VPN** | `http://192.168.27.66:8000` | **Il server (questo PC)** tramite VPN. |
| **Gestione Router** | `http://192.168.1.254` | Il router che è rimasto a casa. |
| **Accesso Camera** | `http://192.168.1.120` | La telecamera che è rimasta a casa. |

> [!TIP]
> L'indirizzo **192.168.27.66** è l'identità fissa di questo computer nella tua VPN. Usalo sempre per collegarti da remoto (es. dal tuo smartphone) quando WireGuard è attivo.

## 4. Troubleshooting: "Non vedo il video"

Se riesci a fare il login ma lo stream video è nero quando sei fuori casa:
* **Latenza/Banda**: Lo streaming video richiede banda stabile. Se sei in 4G con poco segnale, lo stream potrebbe fallire.
* **Firewall Locale**: Verifica che il firewall del computer (es. macOS Firewall) permetta connessioni in entrata sulla porta 8000.
* **Riavvio**: Ogni volta che modifichi il file `.env`, ricordati di riavviare l'app con `make run`.

---
*Configurazione verificata con successo il 18 Aprile 2026.*
