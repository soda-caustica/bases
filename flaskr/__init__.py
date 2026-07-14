from flask import Flask, redirect, url_for
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

    @app.context_processor
    def inject_active_view():
        from flask import request
        endpoint_map = {
            'logistics.dashboard': 'dashboard',
            'logistics.list_clients': 'clients',
            'logistics.list_orders': 'orders',
            'logistics.create_order': 'orders',
            'logistics.order_detail': 'orders',
            'logistics.list_payments': 'payments',
            'logistics.pay_order': 'payments',
            'logistics.list_receipts': 'receipts',
            'logistics.create_receipt': 'receipts',
            'logistics.receipt_detail': 'receipts',
            'logistics.list_vehicles': 'vehicles',
            'logistics.list_gruas': 'gruas',
            'logistics.list_employees': 'employees',
            'logistics.list_sucursales': 'sucursales',
            'logistics.sucursal_detail': 'sucursales',
        }
        return {'active_view': endpoint_map.get(request.endpoint)}

    @app.route('/')

    def hello_world():
        return redirect(url_for('logistics.dashboard'))

    @app.route('/index')
    def index():
        return redirect(url_for('logistics.dashboard'))

    return app
