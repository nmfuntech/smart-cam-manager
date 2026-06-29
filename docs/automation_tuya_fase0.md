# Fase 0 — Setup Tuya e PoC (da fare manualmente)

Questa è la parte che devi fare tu in autonomia: serve a ottenere, **per ogni
device** (le 2 lampade Alantop e le prese Nooie), i quattro dati che il driver
locale usa per controllarlo senza cloud:

| Dato | A cosa serve | Dove lo prendi |
|---|---|---|
| `device_id` | identifica il device | wizard tinytuya |
| `local_key` | chiave per il controllo locale (segreta) | wizard tinytuya |
| `ip` | indirizzo del device in LAN | scan tinytuya |
| `version` | versione protocollo Tuya (3.1/3.3/3.4/3.5) | scan tinytuya |

Alla fine li inserisci nel registry cifrato del progetto (Step 7, import
automatico da `devices.json`) e, se vuoi, li collaudi uno per uno con lo script PoC.

> **Perché serve un account cloud Tuya solo per il controllo locale?** La
> `local_key` viene generata da Tuya e non è leggibile dal device. L'unico modo
> per estrarla è chiederla **una volta** alle API cloud Tuya tramite il wizard.
> Dopo, il controllo è 100% locale: il cloud non serve più (a meno di ri-pairing).

---

## Prerequisiti

- Le lampade/prese sono già configurate e funzionanti nell'app **Smart Life**.
- Il PC su cui giri il wizard/scan è sulla **stessa LAN/subnet** dei device
  (stesso Wi-Fi/rete; niente isolamento "guest" o VLAN separate).
- `tinytuya` installato (già in `pyproject.toml`): `poetry install`.

> **Comandi copiabili:** tutti i comandi `poetry run …` sotto sono su **una riga
> sola** (niente `\` a fine riga: in PowerShell dà errore). Funzionano uguale su
> Windows, macOS e Linux. Solo copia file e cancellazione wizard differiscono per
> shell — vedi Step 7a e la nota sui file in chiaro.

---

## Step 1 — Crea un progetto cloud su Tuya IoT Platform

1. Vai su <https://iot.tuya.com> e registra un account (gratuito).
2. **Cloud → Development → Create Cloud Project**.
   - **Industry / Development Method**: lascia i default (Smart Home).
   - **Data Center**: scegli quello della tua regione. Per l'Italia →
     **Central Europe Data Center**. ⚠️ Deve combaciare con la regione del tuo
     account Smart Life, altrimenti il wizard non vedrà i device.
3. Apri il progetto creato. Nella scheda **Overview** annota:
   - **Access ID / Client ID**
   - **Access Secret / Client Secret**

## Step 2 — Abilita le API necessarie

Nel progetto, scheda **Service API → Go to Authorize**, e assicurati che siano
sottoscritte (basta il trial gratuito):

- **IoT Core**
- **Authorization**
- **Smart Home Scene Linkage**

Senza queste, il wizard fallisce con errori di permesso.

## Step 3 — Collega l'account dell'app Smart Life

1. Nel progetto: **Devices → Link App Account → Add App Account**.
2. Compare un **QR code**. Apri l'app **Smart Life** sul telefono →
   **Profilo (Io)** → icona **scansione** in alto a destra → inquadra il QR.
3. Ora la scheda **Devices → All Devices** elenca le tue lampade/prese. Annota un
   `device_id` qualsiasi (ti serve come "seme" per il wizard).

## Step 4 — Estrai device_id e local_key (wizard)

Dal terminale, nella cartella del progetto:

```bash
poetry run python -m tinytuya wizard
```

Rispondi alle domande:
- **API Key** → l'Access ID dello Step 1
- **API Secret** → l'Access Secret dello Step 1
- **Any Device ID** → il `device_id` annotato allo Step 3
- **Region** → `eu` (Central Europe), oppure `us` / `cn` / `in` secondo il tuo data center

Il wizard scarica **tutti** i device collegati e scrive nella cartella corrente:
- `devices.json` / `tuyadevices.json` → contengono `id`, **`key`** (= `local_key`)
  e nome di ogni device.

> ⚠️ `devices.json` contiene le `local_key` in chiaro: **non committarlo**.
> Tienilo locale e cancellalo dopo aver popolato il registry cifrato (Step 7).

## Step 5 — Trova IP e versione di protocollo (scan)

```bash
poetry run python -m tinytuya scan
```

Lo scanner ascolta i broadcast UDP dei device in LAN e stampa, per ciascuno,
**IP** e **version** (es. `3.3`, `3.4`, `3.5`). Abbinali per nome/`device_id` ai
dati del wizard. Se lo scan combina i dati con `devices.json`, vedrai anche
`local_key` già accoppiata: comodo.

Se un device **non compare**: vedi Troubleshooting.

## Step 6 — Collauda con lo script PoC

Verifica che i dati controllino davvero la lampada (accende/spegne):

```bash
poetry run python scripts/tuya_poc.py --device-id <ID> --ip <IP> --local-key '<KEY>' --version <VER> --cycle
```

> ⚠️ **DP dell'interruttore (`--switch-dp`).** Le **prese** Tuya accendono/spengono
> sul DP `1` (default, niente flag). Le **lampade RGBCW Alantop** usano il DP `20`
> (`switch_led`): aggiungi `--switch-dp 20`, altrimenti il comando "riesce" ma la
> luce non cambia. Per capire il DP di un device, guarda lo `Status` dello scan:
> la chiave booleana che vale `True/False` (es. `'20': False`) è l'interruttore.
>
> ```bash
> # lampada RGBCW
> poetry run python scripts/tuya_poc.py --device-id <ID> --ip <IP> --local-key '<KEY>' --version <VER> --switch-dp 20 --cycle
> ```
>
> Nota sul quoting: metti la `local_key` tra **apici singoli** `'...'`; se contiene
> un apice singolo usa i **doppi apici** `"..."`. Le local_key Tuya sono 16
> caratteri e possono includere simboli (`. ' ^` ecc.): copiala esatta.

Se la lampada si accende e dopo ~2s si spegne → ✅ i dati sono corretti.
Lo script usa lo stesso driver dell'automazione (`TuyaLanDevice`), quindi questo
test convalida proprio il percorso che useremo.

## Step 7 — Inserisci i device nel registry cifrato del progetto

Dopo wizard + scan, `devices.json` contiene già `id`, `key`, `ip` e `version` per
ogni device raggiungibile in LAN. Lo script di import li copia nel registry cifrato
(`data/tuya_devices.json`); le `local_key` restano cifrate at-rest (prefisso `enc::`).

### 7a — Mappa i nomi Smart Life → nomi logici

I nomi nell'app (es. `Presa - Caffè`) vanno tradotti in identificatori per
`rules.yaml` (es. `presa_caffe`: solo minuscole, cifre, underscore).

Copia l'esempio e adattalo:

```bash
# macOS / Linux
cp config/automation/tuya_device_names.example.yaml config/automation/tuya_device_names.yaml
```

```powershell
# Windows (PowerShell) — oppure: cp … (cp è alias di Copy-Item)
Copy-Item config/automation/tuya_device_names.example.yaml config/automation/tuya_device_names.yaml
```

Modifica `config/automation/tuya_device_names.yaml` con i tuoi device. Per vedere i
nomi esatti in Smart Life:

```bash
poetry run python -m tinytuya list
```

### 7b — Import automatico (consigliato)

Anteprima (non scrive nulla):

```bash
poetry run python scripts/tuya_import_registry.py --map config/automation/tuya_device_names.yaml --dry-run
```

Import reale (upsert: aggiorna device già presenti, aggiunge i nuovi):

```bash
poetry run python scripts/tuya_import_registry.py --map config/automation/tuya_device_names.yaml
```

Oppure **scan + import** in un solo comando (utile quando cambiano IP o aggiungi
device):

```bash
poetry run python scripts/tuya_import_registry.py --scan --map config/automation/tuya_device_names.yaml
```

Lo script:
- legge `devices.json` (e `snapshot.json` per dedurre `switch_dp`: `20` lampade
  RGBCW, `1` prese);
- **salta** i device senza IP (offline / non scansionati);
- salva cifrato via `DeviceRegistry`.

Import parziale (solo alcuni device):

```bash
poetry run python scripts/tuya_import_registry.py --only presa_auto --only presa_dj --map config/automation/tuya_device_names.yaml
```

Verifica:

```bash
poetry run python -c "from dotenv import load_dotenv; load_dotenv(); from blackframe.automation.registry import DeviceRegistry; print(DeviceRegistry().list_devices())"
```

(le `local_key` appaiono come `***`: è corretto, sono cifrate su disco).

> ⚠️ Carica sempre `.env` (o riavvia l'app) prima di toccare il registry: la
> chiave di cifratura deve combaciare con quella usata al salvataggio.

Infine **cancella** i file in chiaro generati dal wizard:

```bash
# macOS / Linux
rm -f devices.json tuyadevices.json snapshot.json
```

```powershell
# Windows (PowerShell)
Remove-Item devices.json, tuyadevices.json, snapshot.json -ErrorAction SilentlyContinue
```

### 7c — Import manuale (alternativa)

Per un singolo device (dopo PoC), preferisci l'import automatico con `--only`.
In alternativa, da terminale:

```bash
poetry run python -c "from dotenv import load_dotenv; load_dotenv(); from blackframe.automation.registry import DeviceRegistry as R; print(R().save_device({'name':'luce_ingresso','driver':'tuya_lan','device_id':'<ID>','ip':'<IP>','local_key':'<KEY>','version':3.3,'switch_dp':20}))"
```

(`switch_dp`: `20` lampade RGBCW, `1` prese.)

---

## Troubleshooting

- **Il device non compare nello scan**: PC e device su reti/VLAN diverse, oppure
  Wi-Fi con "AP/client isolation" attivo. Mettili sulla stessa rete. Alcune prese
  vanno su Wi-Fi 2.4 GHz: assicurati che il PC raggiunga quel segmento.
- **PoC dà errore di connessione/decrypt**: `--version` sbagliata (riprova col
  valore esatto dello scan) o `local_key` non aggiornata (ri-esegui il wizard).
- **`local_key` cambiata all'improvviso**: ruota ogni volta che togli/ri-accoppi
  il device o ri-linki l'app account. Ri-esegui wizard + scan, poi rilancia
  `scripts/tuya_import_registry.py` (upsert automatico).
- **Registry vuoto o "non decifrabile"**: hai lanciato comandi senza caricare
  `.env` (chiave di cifratura diversa). Usa `load_dotenv()` negli script o
  riavvia l'app; in caso estremo ripopola con import da `devices.json`.
- **Errore di permesso nel wizard**: mancano le API dello Step 2, o il Data
  Center non combacia con la regione dell'account Smart Life.
- **IP che cambia nel tempo**: assegna ai device un **IP statico** o una
  *reservation* DHCP nel router, così il registry resta valido.

---

## Cosa consegnare alla Fase 3

Quando hai popolato `data/tuya_devices.json` con tutti i device e ognuno passa il
PoC, la Fase 0 è completa. Da lì l'automazione (dispatcher + hook sugli eventi)
risolverà i device per nome logico, senza che tu debba più toccare `local_key`.
