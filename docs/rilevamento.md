# Rilevamento persone e animali

Questa pagina spiega come BLACKFRAME decide se un evento riguarda una persona,
un animale o solo un movimento generico, perché il modello predefinito è
YOLOX-Tiny e come tarare il sistema sulla propria casa.

## Come funziona la catena

1. **Movimento (MOG2).** Il flusso video ridotto (`MOTION_SCALE_WIDTH`) passa
   in un sottrattore di sfondo. Le aree che cambiano diventano "primo piano".
   Le **zone di esclusione** disegnate dall'utente vengono tolte a questo punto.
2. **Conferma.** Servono alcuni frame consecutivi con movimento
   (`MOTION_TRIGGER_FRAMES`) per aprire un evento; si chiude dopo un periodo di
   quiete (`MOTION_CLEAR_FRAMES`, `MOTION_COOLDOWN_SEC`).
3. **Riconoscimento.** Alla chiusura, fino a `CLASSIFICATION_CLIP_MAX_FRAMES`
   frame della clip passano nel detector. Vince il rilevamento più sicuro; se
   una persona è presente, l'evento è "persona" anche se il cane ha un
   punteggio più alto (la sicurezza viene prima).
4. **Avviso.** Le categorie in `CLASSIFICATION_DETECT_PERSON/PET` e la
   modalità (armato, casa, disarmato) decidono se parte la notifica Telegram.

## Perché YOLOX-Tiny

Il confronto è stato fatto il 31/10/2026 su 45 copertine di eventi reali di
questa installazione (corridoio, giorno e notte a infrarossi, con un levriero
in casa), etichettate a mano: 27 con persona (12 insieme al cane), 11 con il
solo cane, 7 vuote (luce o ombre).

| Modello | Soglia | Persona P / R | Animale P / R | Etichetta corretta | Latenza N95 |
|---|---|---|---|---|---|
| MobileNet-SSD v2 (vecchio) | 0,50 (produzione) | ~0,97 / ~0,60 | 1,00 / 0,52 | ~65% | 121 ms |
| MobileNet-SSD v2 | 0,35 | 0,96 / 0,89 | 1,00 / 0,65 | 87% | 121 ms |
| YOLOX-Nano | 0,35 | 1,00 / 0,89 | 1,00 / 0,61 | 87% | 145 ms |
| **YOLOX-Tiny** | **0,35** | **1,00 / 1,00** | 1,00 / 0,57 | **93%** | 256 ms |

P = precisione (quanti allarmi erano giusti), R = recall (quanti casi reali
sono stati trovati). Latenza misurata sul mini PC Intel N95 con 4 thread.

Cosa conta per un sistema di sicurezza:

- **Persone:** YOLOX-Tiny non ne ha persa nessuna e non ha mai scambiato il
  cane per una persona. Il vecchio SSD etichettava persone a metà
  inquadratura come "cat".
- **Animali:** il punto debole è il cane nelle immagini notturne a
  infrarossi, dove i punteggi scendono tra 0,15 e 0,35. Per questo la soglia
  animali è separata e più bassa (0,15), senza falsi positivi nel test.
  Se il cane è il vostro, un cane non riconosciuto finisce in "movimento" e,
  con il riconoscimento attivo, non genera avvisi: lo stesso effetto di
  ignorare gli animali.
- **Costo:** 256 ms per frame vanno bene perché il detector gira solo alla
  chiusura di un evento, in un thread separato, con uscita anticipata appena
  un soggetto supera `CLASSIFICATION_EARLY_EXIT_CONF`.

Valutati e scartati: ONNX Runtime (solo il 30% più veloce, una dipendenza in
più), YOLOv8/YOLO11 (licenza AGPL), OpenVINO (molto più veloce su CPU Intel ma
una dipendenza da decine di MB: possibile evoluzione futura).

## Configurazione

```env
CLASSIFICATION_ENABLED=true
CLASSIFICATION_BACKEND=yolox
CLASSIFICATION_YOLOX_MODEL_PATH=models/yolox_tiny.onnx
CLASSIFICATION_PERSON_MIN_CONFIDENCE=0.35
CLASSIFICATION_PET_MIN_CONFIDENCE=0.15
```

Il modello si scarica con `make fetch-model` (su Windows lo fa da solo
`update.ps1` durante il deploy). `make fetch-model` accetta `--model nano` per
la variante più leggera e `--model ssd` per il vecchio modello.

## Tarare il sistema sulla propria casa

1. **Disegna le zone di esclusione** (Impostazioni → Zone): finestre dove
   entra il sole, specchi, TV, tende. È la leva più efficace contro gli eventi
   "vuoti".
2. **Il vostro animale fa scattare troppi eventi?** Usa la modalità **casa**
   quando siete in casa (registra ma non avvisa) oppure spegni gli avvisi per
   gli animali (`/detect_pet_off`).
3. **Segnala i falsi allarmi** con il bottone ❌ sotto l'avviso: l'evento
   viene marcato in `meta.json` e il riepilogo giornaliero li conta.
4. **Misura sui tuoi eventi.** Copia alcune copertine
   (`captures/motion/<camera>/<evento>/cover.jpg`) in una cartella, scrivi un
   `labels.csv` con colonne `file,person,pet` e lancia:

   ```bash
   poetry run python scripts/benchmark_detectors.py --images cartella \
       --labels labels.csv --thresholds 0.25,0.35,0.45 --show-errors
   ```

## Prestazioni sul mini PC

- Il loop di movimento salta i frame già analizzati (stesso numero di
  sequenza) e non copia il frame se non è cambiato: niente CPU sprecata
  quando lo stream è più lento del loop.
- Con `TAPO_STREAM_PATH=stream2` (720p) e rilevamento spento il servizio usa
  circa il 2,5% della CPU dell'N95.
- `OPENCV_NUM_THREADS` vuoto lascia a OpenCV tutti i core: con 1 o 2 thread il
  detector impiega il doppio.
