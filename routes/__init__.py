from .motion import motion_bp
from .ptz import ptz_bp
from .video import video_bp


def register_blueprints(app):
    app.register_blueprint(video_bp)
    app.register_blueprint(motion_bp)
    app.register_blueprint(ptz_bp)
