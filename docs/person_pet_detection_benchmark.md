# Benchmark persona/pet: locale vs cloud

## Obiettivo

Confrontare tre backend classificazione su stessi eventi motion:

- `local` (OpenCV DNN + modello locale)
- `teachable_machine` (modello esportato TM)
- `cloud` (API esterna)

## Dataset valutazione

1. Usa eventi reali da `captures/motion`.
2. Campiona almeno:
   - 80 eventi con persona
   - 80 eventi con animale domestico
   - 80 eventi negativi (nessuno dei due)
3. Per ogni evento valuta `cover.jpg` e salva ground truth in CSV:
   - `event_id`
   - `ground_truth` (`persona`, `animale_domestico`, `none`)

## Metriche

- Accuratezza:
  - precision/recall/F1 per `persona`
  - precision/recall/F1 per `animale_domestico`
- Prestazioni:
  - latenza inferenza `p50`, `p95` (ms)
  - errore backend (`classification_status != ok`) %
- Impatto runtime:
  - variazione `MOTION_FRAME_INTERVAL` osservata
  - eventuale perdita trigger motion
- Costo:
  - solo backend cloud: costo/evento e costo/mese stimato

## Procedura

1. Abilita backend in `.env`:
   - `CLASSIFICATION_ENABLED=true`
   - `CLASSIFICATION_BACKEND=<local|teachable_machine|cloud>`
2. Esegui sessione test con stessa scena/luci.
3. Esporta eventi via API:
   - `GET /motion_captures?limit=200`
   - `GET /motion_event/<id>`
4. Raccogli campi:
   - `classification.class_label`
   - `classification.confidence`
   - `classification.inference_ms`
   - `classification.classification_status`
5. Confronta output con ground truth.

## Criteri go-live consigliati

- `F1 >= 0.85` su entrambe classi
- `p95 inference <= 200ms` su MacBook Air M1
- errori backend `< 5%`
- zero regressioni su salvataggio eventi motion

## Note Teachable Machine

- Usa export stabile (es. TFLite/TFJS) e congelalo per benchmark.
- Evita dataset sbilanciato per ridurre falsi `persona`.
- Riesegui benchmark dopo ogni retrain.
