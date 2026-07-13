from flask import Flask, redirect, render_template, url_for
import os


def create_app(test_config=None):
    app = Flask(__name__)
    app.config.from_mapping(SECRET_KEY='ola')

    os.makedirs(app.instance_path, exist_ok=True)

    from . import db
    db.init_app(app)

    from . import auth
    app.register_blueprint(auth.bp)

    from . import logistics
    app.register_blueprint(logistics.bp)

    @app.route('/')
    def hello_world():
        return redirect(url_for('index'))

    @app.route('/index')
    def index():
        counts = logistics._fetch_counts()
        return render_template('index.html', counts=counts)

    return app
