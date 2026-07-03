from __future__ import annotations

from flask import Blueprint, current_app, jsonify, render_template, request, session

from blackframe.agent.service import ProposalResult
from blackframe.auth import AUTH_SESSION_KEY, rate_limit, require_auth, require_csrf

agent_bp = Blueprint("agent", __name__)

_RL = {"limit": 15, "window_seconds": 60, "api": True}


def get_services():
    return current_app.config["services"]


def _channel_key() -> str:
    # Un solo utente admin autenticato usa la UI web: la sessione basta a
    # isolare le proposte pending di una sessione dall'altra.
    return str(session.get(AUTH_SESSION_KEY) or "")


def _result_payload(proposal: ProposalResult) -> dict:
    if proposal.result is None and proposal.answer is None:
        return {}
    # snapshot/latest (foto) sono escluse dal catalogo lato web (vedi
    # agent.service.WEB_EXCLUDED_COMMANDS): qui arriva sempre solo testo.
    # La risposta naturale composta dall'LLM, se presente, ha precedenza
    # sull'output grezzo del comando.
    raw_text = proposal.result.text if proposal.result else None
    return {"result_text": proposal.answer or raw_text}


def _transcript():
    return get_services().agent_transcript


def _record_proposal(text: str, proposal: ProposalResult) -> None:
    """Registra il turno nel transcript (best-effort, mai bloccante).

    Le proposte pending si registrano come richiesta di conferma; l'esito
    arriva con il turno di confirm/cancel. I bottoni non vengono ricreati
    dalla history (il pending vive in-memory con TTL breve).
    """
    try:
        transcript = _transcript()
        transcript.append("user", text)
        if not proposal.ok:
            transcript.append("agent", proposal.error or "Non ho capito.", kind="error")
        elif proposal.executed:
            reply = proposal.answer or (proposal.result.text if proposal.result else "")
            transcript.append(
                "agent", reply or "Eseguito.", kind="message", command=proposal.command
            )
        else:
            transcript.append(
                "agent",
                f"Ho capito: {proposal.description} — confermi?",
                kind="confirm_request",
                command=proposal.command,
            )
    except Exception:
        current_app.logger.exception("Registrazione transcript agente fallita")


@agent_bp.get("/agente")
@require_auth()
def agente_page():
    return render_template("agente.html")


@agent_bp.get("/api/agente/status")
@require_auth(api=True)
def agente_status():
    agent = get_services().agent
    return jsonify({"ok": True, "enabled": agent is not None and agent.enabled})


@agent_bp.patch("/api/agente/toggle")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("agente", **_RL)
def agente_toggle():
    payload = request.get_json(silent=True) or {}
    if "enabled" not in payload:
        return jsonify({"ok": False, "error": "Campo 'enabled' mancante"}), 400
    enabled = bool(payload["enabled"])
    services = get_services()
    try:
        services.runtime_config.update({"AGENT_ENABLED": enabled})
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    services.reload_agent()
    agent = services.agent
    return jsonify({"ok": True, "enabled": agent is not None and agent.enabled})


@agent_bp.post("/api/agente/interpret")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("agente", **_RL)
def agente_interpret():
    payload = request.get_json(silent=True) or {}
    text = str(payload.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Messaggio vuoto"}), 400

    services = get_services()
    agent = services.agent
    if agent is None:
        return jsonify({"ok": False, "error": "Assistente non abilitato."}), 503

    proposal = agent.propose(text, "web", _channel_key())
    _record_proposal(text, proposal)
    if not proposal.ok:
        return jsonify({"ok": False, "error": proposal.error})
    return jsonify(
        {
            "ok": True,
            "executed": proposal.executed,
            "command": proposal.command,
            "description": proposal.description,
            "pending_id": proposal.pending_id,
            **_result_payload(proposal),
        }
    )


@agent_bp.post("/api/agente/confirm")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("agente", **_RL)
def agente_confirm():
    payload = request.get_json(silent=True) or {}
    pending_id = str(payload.get("pending_id") or "").strip()
    if not pending_id:
        return jsonify({"ok": False, "error": "pending_id mancante"}), 400

    services = get_services()
    agent = services.agent
    if agent is None:
        return jsonify({"ok": False, "error": "Assistente non abilitato."}), 503

    proposal = agent.confirm(pending_id, "web", _channel_key())
    try:
        if proposal.ok:
            outcome = proposal.result.text if proposal.result else "Eseguito."
            _transcript().append("agent", outcome, kind="executed", command=proposal.command)
        else:
            _transcript().append("agent", proposal.error or "Richiesta scaduta.", kind="error")
    except Exception:
        current_app.logger.exception("Registrazione transcript agente fallita")
    if not proposal.ok:
        return jsonify({"ok": False, "error": proposal.error})
    return jsonify(
        {
            "ok": True,
            "executed": proposal.executed,
            "command": proposal.command,
            "description": proposal.description,
            **_result_payload(proposal),
        }
    )


@agent_bp.post("/api/agente/cancel")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("agente", **_RL)
def agente_cancel():
    payload = request.get_json(silent=True) or {}
    pending_id = str(payload.get("pending_id") or "").strip()
    if not pending_id:
        return jsonify({"ok": False, "error": "pending_id mancante"}), 400

    services = get_services()
    agent = services.agent
    if agent is None:
        return jsonify({"ok": False, "error": "Assistente non abilitato."}), 503

    cancelled = agent.cancel(pending_id, "web", _channel_key())
    if cancelled:
        try:
            _transcript().append("agent", "Annullato.", kind="message")
        except Exception:
            current_app.logger.exception("Registrazione transcript agente fallita")
    return jsonify({"ok": True, "cancelled": cancelled})


@agent_bp.get("/api/agente/history")
@require_auth(api=True)
def agente_history():
    limit = request.args.get("limit", default=100, type=int)
    limit = max(1, min(limit, 500))
    return jsonify({"ok": True, "messages": _transcript().list(limit=limit)})


@agent_bp.delete("/api/agente/history")
@require_auth(api=True)
@require_csrf(api=True)
@rate_limit("agente", **_RL)
def agente_history_clear():
    _transcript().clear()
    return jsonify({"ok": True})
