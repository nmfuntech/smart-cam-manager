"""``python -m blackframe`` dev server entrypoint."""

from blackframe.app import app, create_app

__all__ = ["app", "create_app"]

if __name__ == "__main__":
    import os

    app.run(
        host=os.getenv("APP_BIND_HOST", "127.0.0.1"),
        port=int(os.getenv("APP_PORT", "8000")),
        debug=False,
        use_reloader=False,
        threaded=True,
    )
