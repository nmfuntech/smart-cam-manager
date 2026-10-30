"""``python -m blackframe`` dev server entrypoint."""

from scripts.runtime_paths import configure_runtime_environment

configure_runtime_environment()

from blackframe.logging_setup import configure_logging  # noqa: E402

configure_logging()

from blackframe.app import app, create_app  # noqa: E402

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
