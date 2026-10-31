# Agente in linguaggio naturale

L'agente trasforma un messaggio libero ("sono rientrato", "il cane è uscito?",
"più a sx") in uno dei comandi del registro. Sul mini PC (Intel N95, 8 GB) ogni
chiamata a un LLM costa da 1,5 s in su, quindi la pipeline prova prima le strade
economiche e usa il modello generativo solo quando serve.

## Pipeline

1. **Fast-path** (`agent/fastpath.py`): regole sulle frasi più frequenti. Zero
   modelli, risposta immediata.
2. **Classificatore semantico** (`agent/semantic.py`): un modello di embedding
   servito da Ollama (`AGENT_EMBED_MODEL`, default `embeddinggemma`) confronta il
   messaggio con le frasi di esempio in `agent/intent_examples.json`.
   - L'embedding sceglie l'**argomento** (movimento, notifiche, PTZ, modalità...).
     Le regole lessicali scelgono la **variante**: acceso/spento, direzione,
     armato/casa/disarmato. Gli embedding capiscono bene l'argomento ma male
     le negazioni: senza questo secondo stadio "smetti di rilevare" finiva su
     `motion_on`.
   - Se è sicuro (somiglianza ≥ `AGENT_SEMANTIC_MIN_SCORE` e distacco dal
     secondo argomento ≥ `AGENT_SEMANTIC_MARGIN`) risponde da solo, in circa
     15 ms sul Mac.
   - Se il messaggio somiglia alla classe `nessuno` (saluti, meteo, richieste
     fuori ambito) lo rifiuta senza chiamare l'LLM.
   - Se non è sicuro, passa all'LLM una **rosa di candidati**: i comandi dei 3
     argomenti più vicini.
3. **LLM** (`agent/intent.py`, `AGENT_OLLAMA_MODEL`, default consigliato
   `qwen2.5:1.5b`). Il prompt di sistema è sempre identico, così Ollama
   riusa la cache del prefisso. La rosa di candidati restringe solo lo schema
   JSON della risposta. Se il modello sbaglia acceso/spento o la direzione e le
   regole lessicali danno una risposta netta, vince la regola
   (`_correct_variant`).
4. **Validazione e conferma**: ogni proposta passa dalla whitelist del
   registro e da `validate_arg`. I comandi che cambiano lo stato chiedono
   sempre conferma.

Due regole valgono in tutta la pipeline:
- una **domanda** ("il cane è uscito?") può portare solo a comandi di lettura;
- le frasi con parole distruttive (cancella, elimina, formatta) non passano
  mai dal classificatore.
- se la classe `nessuno` arriva quasi in testa (distacco sotto
  `AGENT_SEMANTIC_NONE_MARGIN`, default 0,03), la frase è ambigua: all'LLM
  restano solo comandi di lettura e, se non ce ne sono, la richiesta viene
  rifiutata. Serve perché lo stesso `qwen2.5:1.5b` sulla CPU del mini PC
  decideva in modo diverso dal Mac ("resetta tutto il sistema" → `disarma`).

## Risultati (31/10/2026)

Set di test indipendente `scripts/agent_eval_cases.json`: 136 frasi scritte in
stile chat, diverse sia dalle regole del fast-path sia dagli esempi del
classificatore. Modello LLM `qwen2.5:1.5b`.

| Configurazione | Comandi giusti | Fuori ambito accettati | Latenza tipica |
|---|---|---|---|
| Prima (fast-path + filtro parole chiave + LLM) | 36,8% | 0% | — |
| + classificatore `embeddinggemma` | 76,1% | 0% | 15 ms |
| + rosa di candidati per l'LLM | 88,9% | 0% | 17 ms |
| + correzione lessicale dell'LLM | **93,2%** | **0%** | 17 ms |

Il vecchio set (`agent_benchmark_cases.json`, 65 frasi) resta al 100%.

Il 36,8% di partenza non dipendeva dall'LLM: il filtro a parole chiave scartava
prima dell'LLM tutte le frasi senza termini come "camera" o "notifiche" (per
esempio "tutto ok?" o "sono rientrato"). Con il classificatore il filtro scatta
solo quando anche gli embedding dicono che il messaggio è fuori tema.

Modelli di embedding confrontati, solo classificatore, a precisione ≥ 97%:

| Modello | Dimensione | Copertura | Note |
|---|---|---|---|
| **embeddinggemma** + prefisso | 622 MB | 65,8% | scelto: migliore pipeline completa, 0 falsi positivi |
| qwen3-embedding:0.6b | 640 MB | 69,2% | pipeline completa 92,3% ma una frase fuori ambito accettata, più lento |
| granite-embedding:278m | 560 MB | 59,0% | |
| paraphrase-multilingual | 563 MB | 46,2% | |

Le soglie e il risolutore sono stati tarati guardando questo set, quindi il
numero reale sarà un po' più basso. Per misurarlo serve il log dei messaggi
veri (sotto).

LLM confrontati da soli, senza fast-path, sulle 65 frasi del vecchio set, sul
mini PC:

| Modello | Comandi giusti | Latenza tipica |
|---|---|---|
| **qwen2.5:1.5b** | 75% | 1,6 s |
| granite4:1b | 72% | 3,2 s |
| qwen3.5:2b | 70% | 18 s |
| qwen3:0.6b | 68% | 1,3 s |
| qwen3.5:0.8b | 63% | 8,4 s |
| qwen2.5:0.5b | 61% | 1,0 s |
| gemma3:1b | 39% | 1,5 s |
| functiongemma:270m | 39% | 1,6 s |

## Messaggi reali

Con `AGENT_MESSAGE_LOG=true` (default) ogni messaggio libero finisce in
`data/agent_messages.jsonl` con il comando proposto, il livello che l'ha deciso
(`fastpath`, `semantic`, `llm`, `gate`...) e l'esito: Conferma o Annulla. Un
Annulla è un ottimo indizio di interpretazione sbagliata.

Per trasformarlo in un set di test:

```bash
python scripts/export_agent_messages.py --out data/agent_real_cases.json \
    --devices lampada_ingresso,presa_salotto --rules notte
# rivedere a mano expect_command/expect_arg, poi:
poetry run python scripts/benchmark_agent.py --models qwen2.5:1.5b --cases data/agent_real_cases.json
```

## Come migliorare

- **Frase capita male in modo ricorrente:** aggiungere 2-3 esempi simili in
  `intent_examples.json`. Non copiare le frasi del set di test, altrimenti il
  test smette di misurare. La cache dei vettori si rigenera da sola.
- **Classificatore troppo timido o troppo audace:** rilanciare
  `scripts/benchmark_semantic.py` e aggiornare le due soglie.
- **Nuovo comando nel registro:** aggiungere i suoi esempi. Se ha una variante
  acceso/spento, aggiungerlo anche a `_ON_OFF_TOPICS`.
- **Fine-tuning di un LLM piccolo (LoRA su qwen2.5:0.5b):** per ora non serve.
  L'LLM decide circa un terzo dei messaggi e sceglie tra pochi candidati. Ha
  senso solo se il set di messaggi reali scende sotto il 90%.

## Strumenti

```bash
# classificatore: copertura/precisione per modello e soglie consigliate
poetry run python scripts/benchmark_semantic.py --models embeddinggemma,granite-embedding:278m
# pipeline completa
poetry run python scripts/benchmark_agent.py --models qwen2.5:1.5b --cases scripts/agent_eval_cases.json
```

Variabili: vedi la sezione agente di `.env.example`.
