from flask import Flask


def create_app():
    app = Flask(__name__)
    app.config["TEMPLATES_AUTO_RELOAD"] = True

    from .routes import main_bp
    app.register_blueprint(main_bp)

    _feeds_started = {"done": False}

    @app.before_request
    def _start_feeds_once():
        if not _feeds_started["done"]:
            _feeds_started["done"] = True
            from .camera import start_all_feeds
            start_all_feeds()

    return app
