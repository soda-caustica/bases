from flask import *
import os

def create_app(test_config = None):
    app = Flask(__name__)
    app.config.from_mapping(SECRET_KEY='ola')
    
    os.makedirs(app.instance_path,exist_ok=True)
    @app.route('/')
    def hello_world():
        return redirect(url_for('auth.login')); 


    from . import db
    db.init_app(app)

    from . import auth
    app.register_blueprint(auth.bp)
    return app
