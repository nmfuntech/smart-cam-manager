"""Semantic intent classifier: sentence embeddings + nearest examples.

Choosing one of ~40 commands is a classification problem, and on the mini PC
a generative LLM pays ~1.5 s (and much more on a cache miss) to solve it. An
embedding model served by the same Ollama instance turns the message into a
vector in a few tens of milliseconds; the command whose example phrases
(``intent_examples.json``) are closest wins.

The classifier only answers when it is confident:

- the best command must score at least ``AGENT_SEMANTIC_MIN_SCORE`` (cosine
  similarity with its closest example);
- it must beat the runner-up command by ``AGENT_SEMANTIC_MARGIN``;
- commands that need an argument (device, rule, minutes...) must get it from
  the text with the deterministic extractors below.

Otherwise it returns ``None`` and the LLM decides as before. The ``nessuno``
class holds out-of-scope phrases: a confident match there rejects the message
without any LLM call. Every proposal still goes through
``intent._validate_response`` (whitelist + ``validate_arg``) and through the
confirmation gate for state-changing commands.

Example vectors are computed once per (model, examples) pair and cached in
``data/agent_embed_cache.npz``; until they are ready the classifier is
simply skipped.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from blackframe.commands.naming import normalize_identifier
from blackframe.envutil import env_bool, env_float, env_int, env_str

from . import ollama_client
from .residency import effective_keep_alive

logger = logging.getLogger(__name__)

NO_COMMAND = "nessuno"
_EXAMPLES_PATH = Path(__file__).with_name("intent_examples.json")

# Commands whose argument is a name, resolved from the text.
_NAME_COMMANDS = {
    "device_on": "device",
    "device_off": "device",
    "entity_status": "entity",
    "rule_run": "rule",
    "rule_on": "rule",
    "rule_off": "rule",
}
# Words that never identify a device/rule on their own.
_NAME_STOPWORDS = frozenset(
    {
        "il", "lo", "la", "i", "gli", "le", "l", "un", "una", "uno", "di", "del", "della",
        "dei", "delle", "dell", "a", "al", "alla", "ai", "all", "in", "nel", "nella", "da",
        "dal", "dalla", "su", "per", "con", "e", "accendi", "spegni", "accendimi", "spegnimi",
        "attiva", "disattiva", "abilita", "disabilita", "regola", "automazione", "on", "off",
        "esegui", "avvia", "lancia", "fai", "partire", "riattiva", "adesso", "ora", "subito",
        "dai", "togli", "corrente", "e'", "accesa", "acceso", "spenta", "spento", "stato",
        "com", "messa", "messo", "mi", "luce", "luci",
    }
)  # fmt: skip


@dataclass
class SemanticMatch:
    command: str
    arg: str | None
    score: float
    margin: float


@dataclass
class SemanticResult:
    match: SemanticMatch | None
    # Commands the LLM should choose from when there is no confident match.
    shortlist: frozenset[str] | None


def _load_examples() -> dict[str, list[str]]:
    data = json.loads(_EXAMPLES_PATH.read_text(encoding="utf-8"))
    return {command: [str(p) for p in phrases] for command, phrases in data.items() if phrases}


def _cache_path() -> Path:
    return Path(env_str("AGENT_EMBED_CACHE_PATH", "data/agent_embed_cache.npz"))


def _normalize_rows(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


# --- argument extractors -----------------------------------------------------


def parse_minutes(normalized: str) -> str | None:
    """Duration in minutes from a normalized message ("2_ore", "mezz_ora")."""
    tokens = normalized.split("_")
    number = next((t for t in tokens if t.isdigit()), None)
    if number is None:
        if {"mezz", "mezzora"} & set(tokens):
            return "30"
        if {"ora", "oretta"} & set(tokens) and {"un", "una", "l"} & set(tokens):
            return "60"
        return None
    value = float(number)
    if any(t.startswith("second") for t in tokens):
        value /= 60
    elif {"ora", "ore", "h"} & set(tokens):
        value *= 60
    return str(int(value)) if value.is_integer() else str(value)


_SENSITIVITY_WORDS = {
    "alta": {"alta", "alto", "massimo", "massima", "max", "aumenta", "alza", "piu"},
    "bassa": {"bassa", "basso", "minimo", "minima", "min", "riduci", "abbassa", "meno", "troppo"},
    "media": {"media", "medio", "normale", "standard"},
}


def parse_sensitivity(normalized: str) -> str | None:
    tokens = set(normalized.split("_"))
    hits = [level for level, words in _SENSITIVITY_WORDS.items() if tokens & words]
    return hits[0] if len(hits) == 1 else None


def resolve_mentioned_name(normalized: str, candidates: list[str]) -> str | None:
    """The single candidate name the message refers to, or None.

    A candidate matches when one of its meaningful tokens appears in the
    message; the one with the most matching tokens wins, ties are ambiguous.
    """
    tokens = {t for t in normalized.split("_") if t and t not in _NAME_STOPWORDS}
    if not tokens:
        return None
    scored = []
    for candidate in candidates:
        cand_tokens = {t for t in normalize_identifier(candidate).split("_") if t}
        overlap = len(tokens & cand_tokens)
        if overlap:
            scored.append((overlap, candidate))
    if not scored:
        return None
    scored.sort(reverse=True)
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][1]


def extract_arg(command: str, text: str, known_names: dict[str, list[str]]) -> tuple[bool, Any]:
    """Return (ok, arg). ok=False means a required argument is missing."""
    normalized = normalize_identifier(text)
    if command == "mute":
        return True, parse_minutes(normalized)
    if command == "sensitivity":
        level = parse_sensitivity(normalized)
        return level is not None, level
    kind = _NAME_COMMANDS.get(command)
    if kind is None:
        return True, None
    candidates = list(known_names.get(kind, []))
    if kind == "entity":
        candidates += known_names.get("device", [])
    # Devices are also entities: the same name twice must not look ambiguous.
    name = resolve_mentioned_name(normalized, list(dict.fromkeys(candidates)))
    return name is not None, name


# --- topics: the embedding picks the topic, word rules pick the command -------
#
# Embeddings capture the subject of a sentence well but negation poorly:
# "smetti di rilevare" sits next to "rileva il movimento". So on/off pairs,
# PTZ directions and the three security modes are grouped in one topic each,
# and a deterministic resolver picks the command inside the topic. When the
# resolver is unsure the classifier abstains and the LLM decides.

_OFF_WORDS = frozenset(
    {
        "spegni", "spegnimi", "spegnere", "spegnila", "spegnilo", "disattiva", "disattivare",
        "disabilita", "ferma", "fermare", "stop", "basta", "smetti", "smettila", "non",
        "niente", "off", "togli", "disinserisci", "pausa", "ignora", "sospendi", "senza",
        "stacca", "no",
    }
)  # fmt: skip
_ON_WORDS = frozenset(
    {
        "accendi", "accendimi", "accendere", "attiva", "attivare", "attivami", "riattiva",
        "abilita", "avvia", "on", "riaccendi", "voglio", "avvisami", "notificami",
        "segnalami", "registra", "salva", "inserisci", "riprendi", "torna", "ricomincia",
        "sempre", "rileva", "manda", "mandami", "dai", "parti", "partire",
    }
)  # fmt: skip
_ON_OFF_TOPICS = {
    "motion": ("motion_on", "motion_off"),
    "classification": ("classification_on", "classification_off"),
    "detect_person": ("detect_person_on", "detect_person_off"),
    "detect_pet": ("detect_pet_on", "detect_pet_off"),
    "notifications": ("notifications_on", "notifications_off"),
    "record": ("record_on", "record_off"),
    "continuous": ("continuous_on", "continuous_off"),
    "device": ("device_on", "device_off"),
    "rule_toggle": ("rule_on", "rule_off"),
}
_PTZ_WORDS = {
    "ptz_left": {"sinistra", "sx"},
    "ptz_right": {"destra", "dx"},
    "ptz_up": {"su", "alto", "sopra", "alza", "alzati"},
    "ptz_down": {"giu", "basso", "sotto", "abbassa", "abbassati"},
    "ptz_stop": {"stop", "ferma", "fermati", "blocca", "basta"},
    "ptz_home": {"home", "centro", "partenza", "iniziale", "dritta", "dritto", "default"},
}
_MODE_OUT_WORDS = frozenset({"esco", "uscendo", "usciamo", "fuori", "via", "vado", "assente"})
_MODE_HOME_WORDS = frozenset(
    {"casa", "rientrato", "rientrata", "rientrati", "tornato", "tornata", "tornati", "dentro"}
)

TOPIC_OF: dict[str, str] = {}
for _topic, _pair in _ON_OFF_TOPICS.items():
    for _command in _pair:
        TOPIC_OF[_command] = _topic
for _command in _PTZ_WORDS:
    TOPIC_OF[_command] = "ptz"
for _command in ("arma", "casa", "disarma"):
    TOPIC_OF[_command] = "mode_set"


def _topic(command: str) -> str:
    return TOPIC_OF.get(command, command)


def polarity(tokens: set[str]) -> str | None:
    """'on', 'off' or None. Negation wins: "non attivare" is off."""
    if tokens & _OFF_WORDS:
        return "off"
    if tokens & _ON_WORDS:
        return "on"
    return None


def resolve_in_topic(topic: str, text: str) -> str | None:
    tokens = set(normalize_identifier(text).split("_"))
    if topic in _ON_OFF_TOPICS:
        side = polarity(tokens)
        if side is None:
            return None
        on, off = _ON_OFF_TOPICS[topic]
        return on if side == "on" else off
    if topic == "ptz":
        hits = [command for command, words in _PTZ_WORDS.items() if tokens & words]
        return hits[0] if len(hits) == 1 else None
    if topic == "mode_set":
        # Explicit verbs first: "arma la casa" is armato, not casa.
        if tokens & {"disarma", "disarmato", "disarmata", "disinserisci"}:
            return "disarma"
        if tokens & {"arma", "armato", "armata", "inserisci"}:
            return "arma"
        if tokens & _MODE_OUT_WORDS:
            return "arma"
        if tokens & _MODE_HOME_WORDS:
            return "casa"
        side = polarity(tokens)
        if side == "off":
            return "disarma"
        if side == "on":
            return "arma"
        return None
    return topic


def rank_topics(
    command_scores: dict[str, float], allowed: frozenset[str] | set[str]
) -> list[tuple[str, float]]:
    """Best score per topic among allowed commands (plus ``nessuno``)."""
    topic_scores: dict[str, float] = {}
    for command, score in command_scores.items():
        if command != NO_COMMAND and command not in allowed:
            continue
        topic = _topic(command)
        if score > topic_scores.get(topic, -1.0):
            topic_scores[topic] = score
    return sorted(topic_scores.items(), key=lambda item: item[1], reverse=True)


def shortlist(
    command_scores: dict[str, float],
    allowed: frozenset[str] | set[str],
    *,
    topics: int = 3,
    min_score: float = 0.70,
    none_margin: float = 0.0,
) -> frozenset[str] | None:
    """Commands of the best topics, to narrow the LLM choice when the
    classifier abstains. None when the message looks out of scope.

    When the out-of-scope class scores almost as high as the best topic
    (within ``none_margin``), the message is ambiguous ("resetta tutto il
    sistema", "che giorno e' oggi"): only read-only commands are kept, so a
    small LLM cannot turn it into a state change. The result may then be
    empty, which means "reject".
    """
    ranked = rank_topics(command_scores, allowed)
    if not ranked or ranked[0][0] == NO_COMMAND or ranked[0][1] < min_score:
        return None
    chosen = {topic for topic, _ in ranked[:topics] if topic != NO_COMMAND}
    commands = frozenset(c for c in allowed if _topic(c) in chosen)
    none_score = command_scores.get(NO_COMMAND)
    if none_score is not None and none_score >= ranked[0][1] - none_margin:
        commands = frozenset(c for c in commands if _is_readonly(c))
    return commands


def _is_readonly(command: str) -> bool:
    from blackframe.commands import COMMAND_REGISTRY

    spec = COMMAND_REGISTRY.get(command)
    return spec is not None and spec.readonly


def decide(
    command_scores: dict[str, float],
    text: str,
    allowed: frozenset[str] | set[str],
    known_names: dict[str, list[str]],
    *,
    min_score: float,
    margin: float,
) -> SemanticMatch | None:
    """Pure decision step, shared by the classifier and the benchmark."""
    ranked = rank_topics(command_scores, allowed)
    if len(ranked) < 2:
        return None
    (topic, score), (_, runner_up) = ranked[0], ranked[1]
    gap = score - runner_up
    if score < min_score or gap < margin:
        return None
    if topic == NO_COMMAND:
        return SemanticMatch(NO_COMMAND, None, score, gap)
    command = resolve_in_topic(topic, text)
    if command is None or command not in allowed:
        return None
    ok, arg = extract_arg(command, text, known_names)
    if not ok:
        return None
    return SemanticMatch(command, arg, score, gap)


# --- classifier --------------------------------------------------------------


class SemanticClassifier:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model: str | None = None
        self._labels: list[str] = []
        self._matrix: np.ndarray | None = None
        self._building = False
        self._failed_model: str | None = None

    @staticmethod
    def enabled() -> bool:
        return env_bool("AGENT_SEMANTIC", True) and bool(env_str("AGENT_EMBED_MODEL", ""))

    @staticmethod
    def _base_url() -> str:
        return env_str("AGENT_OLLAMA_URL", "http://127.0.0.1:11434")

    def _embed(self, texts: list[str], timeout: float) -> np.ndarray | None:
        prefix = env_str("AGENT_EMBED_PREFIX", "")
        vectors = ollama_client.embed(
            self._base_url(),
            env_str("AGENT_EMBED_MODEL", ""),
            [prefix + t for t in texts],
            timeout=timeout,
            keep_alive=effective_keep_alive() or None,
        )
        if vectors is None or len(vectors) != len(texts):
            return None
        return _normalize_rows(np.asarray(vectors, dtype=np.float32))

    def _signature(self, model: str, examples: dict[str, list[str]]) -> str:
        payload = json.dumps([model, env_str("AGENT_EMBED_PREFIX", ""), examples], sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _load_cache(self, signature: str) -> tuple[list[str], np.ndarray] | None:
        try:
            with np.load(_cache_path(), allow_pickle=False) as data:
                if str(data["signature"]) != signature:
                    return None
                return [str(x) for x in data["labels"]], data["matrix"].astype(np.float32)
        except (OSError, KeyError, ValueError):
            return None

    def _save_cache(self, signature: str, labels: list[str], matrix: np.ndarray) -> None:
        path = _cache_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as handle:
                np.savez(handle, signature=np.array(signature), labels=np.array(labels),
                         matrix=matrix)  # fmt: skip
        except OSError:
            logger.warning("Cache embedding agente non salvata", exc_info=True)

    def build(self) -> bool:
        """Compute (or load) the example vectors. Safe to call repeatedly."""
        if not self.enabled():
            return False
        model = env_str("AGENT_EMBED_MODEL", "")
        with self._lock:
            if self._model == model and self._matrix is not None:
                return True
            if self._building:
                return False
            self._building = True
        try:
            examples = _load_examples()
            signature = self._signature(model, examples)
            cached = self._load_cache(signature)
            if cached is not None:
                labels, matrix = cached
            else:
                labels = [command for command, phrases in examples.items() for _ in phrases]
                phrases = [phrase for items in examples.values() for phrase in items]
                parts = []
                for start in range(0, len(phrases), 32):
                    part = self._embed(phrases[start : start + 32], timeout=120.0)
                    if part is None:
                        if self._failed_model != model:
                            logger.warning(
                                "Classificatore semantico non disponibile: embedding falliti "
                                "con il modello %s (scaricato in Ollama?)",
                                model,
                            )
                            self._failed_model = model
                        return False
                    parts.append(part)
                matrix = np.vstack(parts)
                self._save_cache(signature, labels, matrix)
                logger.info("Classificatore semantico pronto: %d esempi (%s)", len(labels), model)
            with self._lock:
                self._model, self._labels, self._matrix = model, labels, matrix
                self._failed_model = None
            return True
        finally:
            with self._lock:
                self._building = False

    def preload_async(self) -> None:
        with self._lock:
            busy = self._building
        if self.enabled() and not busy:
            threading.Thread(target=self.build, name="agent-semantic-build", daemon=True).start()

    def ready(self) -> bool:
        with self._lock:
            return self._matrix is not None and self._model == env_str("AGENT_EMBED_MODEL", "")

    def scores(self, text: str) -> list[tuple[str, float]] | None:
        """Best similarity per class, sorted descending (None if unavailable)."""
        if not self.ready():
            return None
        timeout = env_float("AGENT_SEMANTIC_TIMEOUT_SEC", 5.0, minimum=0.5)
        query = self._embed([text], timeout=timeout)
        if query is None:
            return None
        with self._lock:
            labels, matrix = self._labels, self._matrix
        sims = matrix @ query[0]
        best: dict[str, float] = {}
        for label, sim in zip(labels, sims.tolist()):
            if sim > best.get(label, -1.0):
                best[label] = sim
        return sorted(best.items(), key=lambda item: item[1], reverse=True)

    def classify(
        self,
        text: str,
        allowed: frozenset[str] | set[str],
        known_names: dict[str, list[str]],
    ) -> SemanticMatch | None:
        """A confident match among ``allowed`` (plus ``nessuno``), or None."""
        result = self.analyze(text, allowed, known_names)
        return None if result is None else result.match

    def analyze(
        self,
        text: str,
        allowed: frozenset[str] | set[str],
        known_names: dict[str, list[str]],
    ) -> SemanticResult | None:
        """Confident match and LLM shortlist from a single embedding call."""
        ranked = self.scores(text)
        if not ranked:
            return None
        command_scores = dict(ranked)
        match = decide(
            command_scores,
            text,
            allowed,
            known_names,
            min_score=env_float("AGENT_SEMANTIC_MIN_SCORE", 0.80),
            margin=env_float("AGENT_SEMANTIC_MARGIN", 0.04),
        )
        short = None
        if match is None:
            short = shortlist(
                command_scores,
                allowed,
                topics=env_int("AGENT_SEMANTIC_SHORTLIST_TOPICS", 3, minimum=1),
                min_score=env_float("AGENT_SEMANTIC_SHORTLIST_SCORE", 0.70),
                none_margin=env_float("AGENT_SEMANTIC_NONE_MARGIN", 0.03),
            )
        return SemanticResult(match, short)


_classifier = SemanticClassifier()


def get_classifier() -> SemanticClassifier:
    return _classifier
