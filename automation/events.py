"""Contesto evento passato dal core all'automazione.

``EventContext`` è l'unico payload che il core (``MotionDetector``) consegna al
layer di automazione. Disaccoppia il rule engine dai dettagli interni della
pipeline video: contiene solo ciò che serve a far matchare una regola.
"""

from dataclasses import dataclass

# Mappa la categoria interna (italiana, come nei suffissi dir / meta.json) verso
# il nome-evento usato nelle regole. Aggiungere una categoria qui NON richiede di
# toccare engine o driver.
CATEGORY_EVENT_MAP = {
    "persona": "person_detected",
    "animale_domestico": "animal_detected",
    "movimento": "motion_detected",
}

# Evento di fallback quando la categoria è sconosciuta/non classificata.
DEFAULT_EVENT_NAME = "motion_detected"


@dataclass(frozen=True)
class EventContext:
    """Snapshot immutabile di un evento cam chiuso.

    Args:
        event_id: nome dir evento (post-rename), es. ``motion_event_..._\\_persona``.
        category: categoria interna (``persona`` / ``animale_domestico`` / ``movimento``).
        source: id del profilo camera sorgente (``profile_id``); ``None`` = sconosciuta.
        timestamp: epoch dell'evento (``time.time()``); ``None`` se non disponibile.
        video_path: percorso della clip ``event.mp4`` se presente.
    """

    event_id: str
    category: str
    source: str | None = None
    timestamp: float | None = None
    video_path: str | None = None

    @property
    def event_name(self) -> str:
        """Nome-evento normalizzato usato dalle regole (es. ``person_detected``)."""
        return CATEGORY_EVENT_MAP.get(self.category, DEFAULT_EVENT_NAME)
