"""Risposte in italiano naturale per le domande sui comandi di lettura.

Quando l'utente fa una *domanda* ("le luci sono accese?") e l'agente la
risolve con un comando readonly (es. ``devices``), l'output grezzo del
comando è un elenco tecnico: una seconda chiamata LLM lo trasforma in una
risposta breve in italiano, ancorata SOLO ai dati del comando.

Costa una seconda generazione, quindi:

- scatta solo se l'input *sembra una domanda* (euristica locale, zero LLM):
  gli imperativi tipo "stato" non pagano nulla;
- i dati passati al modello sono troncati (``AGENT_ANSWER_MAX_RESULT_CHARS``)
  per contenere il prefill su CPU;
- fail-open: su qualunque errore si torna ``None`` e il canale mostra
  l'output grezzo come prima.
"""

from __future__ import annotations

import logging
import os
import re

from . import ollama_client

logger = logging.getLogger(__name__)

_INTERROGATIVE_STARTS = {
    "come",
    "cosa",
    "che",
    "chi",
    "quando",
    "dove",
    "perche",
    "perché",
    "quanti",
    "quante",
    "quanto",
    "quanta",
    "quale",
    "quali",
    "mi",
    "c'è",
    "c'e'",
    "ci",
}

# "le luci sono accese", "il movimento è attivo", "sta registrando" — domande
# di stato anche senza punto interrogativo o parola interrogativa iniziale.
_STATE_RE = re.compile(
    r"\b(?:e|è|sono|sta|stanno)\s+"
    r"(?:acces\w*|spent\w*|attiv\w*|disattiv\w*|silenziat\w*|registrando|funzionando)",
    re.IGNORECASE,
)

_SYSTEM_PROMPT = (
    "Sei l'assistente di BLACKFRAME, un sistema di videosorveglianza "
    "domestica. Rispondi alla domanda dell'utente in una o due frasi in "
    "italiano, usando SOLO i dati forniti. Se i dati non bastano per "
    "rispondere, di' che non lo sai. Non inventare informazioni."
)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def looks_like_question(text: str) -> bool:
    """Euristica locale (niente LLM): il testo sembra una domanda di stato?"""
    value = (text or "").strip().lower()
    if not value:
        return False
    if "?" in value:
        return True
    first = value.split(None, 1)[0]
    if first in _INTERROGATIVE_STARTS:
        return True
    return bool(_STATE_RE.search(value))


def compose_answer(question: str, command: str, result_text: str) -> str | None:
    """Seconda chiamata LLM: dai dati del comando a una risposta naturale.

    Ritorna ``None`` su qualunque errore o risposta vuota: il chiamante deve
    ripiegare sull'output grezzo del comando.
    """
    max_chars = _env_int("AGENT_ANSWER_MAX_RESULT_CHARS", 700)
    base_url = os.getenv("AGENT_OLLAMA_URL", "http://127.0.0.1:11434").strip()
    model = os.getenv("AGENT_OLLAMA_MODEL", "qwen2.5:0.5b").strip()
    timeout = _env_float("AGENT_TIMEOUT_SEC", 8.0)
    keep_alive = os.getenv("AGENT_OLLAMA_KEEP_ALIVE", "30m").strip()

    user_text = f"Domanda: {question}\nDati ({command}):\n{result_text[:max_chars]}"
    answer = ollama_client.chat_text(
        base_url,
        model,
        _SYSTEM_PROMPT,
        user_text,
        timeout=timeout,
        keep_alive=keep_alive or None,
        options={
            "temperature": _env_float("AGENT_OLLAMA_TEMPERATURE", 0.0),
            "num_ctx": _env_int("AGENT_OLLAMA_NUM_CTX", 1536),
            "num_predict": 120,
        },
    )
    if not answer:
        logger.info("Composizione risposta naturale fallita per %s, uso output grezzo", command)
        return None
    return answer
