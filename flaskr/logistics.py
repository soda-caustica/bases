from datetime import date

from flask import (
    Blueprint, abort, flash, g, redirect, render_template, request, url_for
)

from .auth import admin_required, login_required
from .db import get_db

bp = Blueprint('logistics', __name__)

ORDER_STATES = [
    ('pendiente', 'Pendiente'),
    ('en_proceso', 'En proceso'),
    ('completada', 'Completada'),
    ('cancelada', 'Cancelada'),
]
PAYMENT_METHODS = [
    ('efectivo', 'Efectivo'),
    ('tarjeta', 'Tarjeta'),
    ('transferencia', 'Transferencia'),
    ('otro', 'Otro'),
]


def _is_admin():
    return g.user is not None and g.user.get('role') == 'admin'


def _fetch_counts():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT count(*) FROM public.ordenderetiro")
    total = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM public.ordenderetiro WHERE estado = 'pendiente'")
    pending = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM public.ordenderetiro WHERE estado = 'en_proceso'")
    in_process = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM public.ordenderetiro WHERE estado = 'completada'")
    completed = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM public.chofer WHERE disponibilidad = true")
    available_drivers = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM public.cliente")
    total_clients = cur.fetchone()[0]
    cur.execute(
        "SELECT COALESCE(SUM(monto_total), 0) FROM public.factura "
        "WHERE date_trunc('month', fecha) = date_trunc('month', CURRENT_DATE)"
    )
    monthly_billing = cur.fetchone()[0]
    cur.execute(
        """
        SELECT count(*) FROM public.ordenderetiro o
        WHERE o.estado = 'completada'
          AND NOT EXISTS (SELECT 1 FROM public.factura f WHERE f.numero_de_orden = o.numero_de_orden)
        """
    )
    pending_invoices = cur.fetchone()[0]
    return {
        'total': total,
        'pending': pending,
        'in_process': in_process,
        'completed': completed,
        'available_drivers': available_drivers,
        'total_clients': total_clients,
        'monthly_billing': monthly_billing,
        'pending_invoices': pending_invoices,
    }


def _fetch_recent_orders(limit=5):
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT o.numero_de_orden, o.estado, o.monto_base,
               COALESCE(p.nombre, e.razon_social, c.email, c.rut) AS cliente_nombre
        FROM public.ordenderetiro o
        JOIN public.cliente c ON c.rut = o.rut_cliente
        LEFT JOIN public.persona p ON p.rut = c.rut
        LEFT JOIN public.empresa e ON e.rut = c.rut
        ORDER BY o.fecha DESC, o.numero_de_orden DESC
        LIMIT %s
        """,
        (limit,),
    )
    return cur.fetchall()


@bp.route('/dashboard')
@login_required
def dashboard():
    if _is_admin():
        counts = _fetch_counts()
        recent_orders = _fetch_recent_orders()
        return render_template('dashboard/admin.html', counts=counts, recent_orders=recent_orders)

    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT o.numero_de_orden, o.fecha, o.estado, o.monto_base,
               o.direccion_origen, o.direccion_destino
        FROM public.ordenderetiro o
        WHERE o.rut_cliente = %s
        ORDER BY o.fecha DESC, o.numero_de_orden DESC
        """,
        (g.user['rut'],),
    )
    my_orders = cur.fetchall()
    active_order = next((o for o in my_orders if o.estado in ('pendiente', 'en_proceso')), None)
    completed_count = sum(1 for o in my_orders if o.estado == 'completada')

    cur.execute(
        """
        SELECT f.numero_de_factura, f.fecha, f.monto_total, f.metodo_de_pago, f.numero_de_orden
        FROM public.factura f
        JOIN public.ordenderetiro o ON o.numero_de_orden = f.numero_de_orden
        WHERE o.rut_cliente = %s
        ORDER BY f.fecha DESC
        LIMIT 5
        """,
        (g.user['rut'],),
    )
    my_invoices = cur.fetchall()

    cur.execute(
        """
        SELECT count(*) FROM public.ordenderetiro o
        WHERE o.rut_cliente = %s AND o.estado = 'completada'
          AND NOT EXISTS (SELECT 1 FROM public.factura f WHERE f.numero_de_orden = o.numero_de_orden)
        """,
        (g.user['rut'],),
    )
    pending_invoices_count = cur.fetchone()[0]

    cur.execute(
        "SELECT COALESCE(SUM(f.monto_total), 0) FROM public.factura f "
        "JOIN public.ordenderetiro o ON o.numero_de_orden = f.numero_de_orden "
        "WHERE o.rut_cliente = %s",
        (g.user['rut'],),
    )
    total_paid = cur.fetchone()[0]

    return render_template(
        'dashboard/client.html',
        my_orders=my_orders,
        active_order=active_order,
        completed_count=completed_count,
        my_invoices=my_invoices,
        pending_invoices_count=pending_invoices_count,
        total_paid=total_paid,
        states=dict(ORDER_STATES),
    )


@bp.route('/orders')
@login_required
def list_orders():
    db = get_db()
    cur = db.cursor()
    if _is_admin():
        cur.execute(
            """
            SELECT o.numero_de_orden,
                   o.fecha,
                   o.estado,
                   o.monto_base,
                   o.direccion_origen,
                   o.direccion_destino,
                   o.rut_cliente,
                   COALESCE(p.nombre, e.razon_social, c.email, c.rut) AS cliente_nombre,
                   CASE WHEN f.numero_de_factura IS NULL THEN false ELSE true END AS tiene_factura
            FROM public.ordenderetiro o
            JOIN public.cliente c ON c.rut = o.rut_cliente
            LEFT JOIN public.persona p ON p.rut = c.rut
            LEFT JOIN public.empresa e ON e.rut = c.rut
            LEFT JOIN public.factura f ON f.numero_de_orden = o.numero_de_orden
            ORDER BY o.fecha DESC, o.numero_de_orden DESC
            """
        )
    else:
        cur.execute(
            """
            SELECT o.numero_de_orden,
                   o.fecha,
                   o.estado,
                   o.monto_base,
                   o.direccion_origen,
                   o.direccion_destino,
                   o.rut_cliente,
                   COALESCE(p.nombre, e.razon_social, c.email, c.rut) AS cliente_nombre,
                   CASE WHEN f.numero_de_factura IS NULL THEN false ELSE true END AS tiene_factura
            FROM public.ordenderetiro o
            JOIN public.cliente c ON c.rut = o.rut_cliente
            LEFT JOIN public.persona p ON p.rut = c.rut
            LEFT JOIN public.empresa e ON e.rut = c.rut
            LEFT JOIN public.factura f ON f.numero_de_orden = o.numero_de_orden
            WHERE o.rut_cliente = %s
            ORDER BY o.fecha DESC, o.numero_de_orden DESC
            """,
            (g.user['rut'],),
        )
    orders = cur.fetchall()
    return render_template('orders/list.html', orders=orders)


@bp.route('/orders/create', methods=('GET', 'POST'))
@login_required
def create_order():
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT c.rut,
               COALESCE(p.nombre, e.razon_social, c.email, c.rut) AS nombre
        FROM public.cliente c
        LEFT JOIN public.persona p ON p.rut = c.rut
        LEFT JOIN public.empresa e ON e.rut = c.rut
        ORDER BY nombre
        """
    )
    clients = cur.fetchall()
    cur.execute("SELECT id_comuna, nombre FROM public.comuna ORDER BY nombre")
    communes = cur.fetchall()

    error = None
    if request.method == 'POST':
        numero = request.form.get('numero_de_orden', '').strip()
        fecha = request.form.get('fecha', '').strip()
        monto = request.form.get('monto_base', '').strip()
        estado = request.form.get('estado', '').strip()
        rut_cliente = request.form.get('rut_cliente', '').strip() if _is_admin() else g.user['rut']
        direccion_origen = request.form.get('direccion_origen', '').strip()
        direccion_destino = request.form.get('direccion_destino', '').strip()
        id_comuna_origen = request.form.get('id_comuna_origen', '').strip()
        id_comuna_destino = request.form.get('id_comuna_destino', '').strip()

        if not numero.isdigit():
            error = 'Número de orden válido es obligatorio.'
        elif not fecha:
            error = 'Fecha de la orden es obligatoria.'
        elif not monto:
            error = 'Monto base de la orden es obligatorio.'
        elif not estado:
            error = 'Estado de la orden es obligatorio.'
        elif not rut_cliente:
            error = 'Cliente asociado es obligatorio.'
        elif not direccion_origen or not direccion_destino:
            error = 'Dirección de origen y destino son obligatorias.'
        elif not id_comuna_origen.isdigit() or not id_comuna_destino.isdigit():
            error = 'Comunas de origen y destino deben seleccionarse correctamente.'

        if error is None:
            try:
                cur.execute(
                    """
                    INSERT INTO public.ordenderetiro
                        (numero_de_orden, fecha, monto_base, estado, rut_cliente,
                         direccion_origen, direccion_destino, id_comuna_origen, id_comuna_destino)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        int(numero), fecha, monto, estado, rut_cliente,
                        direccion_origen, direccion_destino,
                        int(id_comuna_origen), int(id_comuna_destino),
                    ),
                )
                db.commit()
                flash('Orden de retiro creada correctamente.')
                return redirect(url_for('logistics.list_orders'))
            except Exception:
                db.rollback()
                error = 'No se pudo crear la orden. Verifica que el número de orden no exista y los datos sean válidos.'

        flash(error)

    return render_template(
        'orders/form.html',
        clients=clients,
        communes=communes,
        states=ORDER_STATES,
        is_admin=_is_admin(),
    )


@bp.route('/orders/<int:order_id>')
@login_required
def order_detail(order_id):
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT o.numero_de_orden,
               o.fecha,
               o.monto_base,
               o.estado,
               o.direccion_origen,
               o.direccion_destino,
               o.rut_cliente,
               COALESCE(p.nombre, e.razon_social, c.email, c.rut) AS cliente_nombre,
               c.email AS cliente_email,
               c.fono AS cliente_fono
        FROM public.ordenderetiro o
        JOIN public.cliente c ON c.rut = o.rut_cliente
        LEFT JOIN public.persona p ON p.rut = c.rut
        LEFT JOIN public.empresa e ON e.rut = c.rut
        WHERE o.numero_de_orden = %s
        """,
        (order_id,),
    )
    order_record = cur.fetchone()
    if order_record is None:
        abort(404)
    if not _is_admin() and order_record.rut_cliente != g.user['rut']:
        abort(404)

    cur.execute(
        "SELECT numero_de_factura, fecha, monto_total, metodo_de_pago FROM public.factura WHERE numero_de_orden = %s",
        (order_id,),
    )
    receipt = cur.fetchone()

    return render_template('orders/detail.html', order=order_record, receipt=receipt, is_admin=_is_admin())


@bp.route('/receipts')
@login_required
def list_receipts():
    db = get_db()
    cur = db.cursor()
    if _is_admin():
        cur.execute(
            """
            SELECT f.numero_de_factura,
                   f.fecha,
                   f.monto_total,
                   f.metodo_de_pago,
                   f.numero_de_orden,
                   o.estado AS orden_estado,
                   COALESCE(p.nombre, e.razon_social, c.email, c.rut) AS cliente_nombre
            FROM public.factura f
            JOIN public.ordenderetiro o ON o.numero_de_orden = f.numero_de_orden
            JOIN public.cliente c ON c.rut = o.rut_cliente
            LEFT JOIN public.persona p ON p.rut = c.rut
            LEFT JOIN public.empresa e ON e.rut = c.rut
            ORDER BY f.fecha DESC, f.numero_de_factura DESC
            """
        )
    else:
        cur.execute(
            """
            SELECT f.numero_de_factura,
                   f.fecha,
                   f.monto_total,
                   f.metodo_de_pago,
                   f.numero_de_orden,
                   o.estado AS orden_estado,
                   COALESCE(p.nombre, e.razon_social, c.email, c.rut) AS cliente_nombre
            FROM public.factura f
            JOIN public.ordenderetiro o ON o.numero_de_orden = f.numero_de_orden
            JOIN public.cliente c ON c.rut = o.rut_cliente
            LEFT JOIN public.persona p ON p.rut = c.rut
            LEFT JOIN public.empresa e ON e.rut = c.rut
            WHERE o.rut_cliente = %s
            ORDER BY f.fecha DESC, f.numero_de_factura DESC
            """,
            (g.user['rut'],),
        )
    receipts = cur.fetchall()
    return render_template('receipts/list.html', receipts=receipts, is_admin=_is_admin())


@bp.route('/receipts/create', methods=('GET', 'POST'))
@admin_required
def create_receipt():
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT o.numero_de_orden,
               COALESCE(p.nombre, e.razon_social, c.email, c.rut) AS cliente_nombre,
               o.monto_base
        FROM public.ordenderetiro o
        JOIN public.cliente c ON c.rut = o.rut_cliente
        LEFT JOIN public.persona p ON p.rut = c.rut
        LEFT JOIN public.empresa e ON e.rut = c.rut
        WHERE NOT EXISTS (
            SELECT 1 FROM public.factura f WHERE f.numero_de_orden = o.numero_de_orden
        )
        ORDER BY o.fecha DESC
        """
    )
    pending_orders = cur.fetchall()

    if request.method == 'POST':
        numero_factura = request.form.get('numero_de_factura', '').strip()
        fecha = request.form.get('fecha', '').strip()
        monto_total = request.form.get('monto_total', '').strip()
        metodo_pago = request.form.get('metodo_de_pago', '').strip()
        numero_de_orden = request.form.get('numero_de_orden', '').strip()

        error = None
        if not numero_factura.isdigit():
            error = 'Número de factura válido es obligatorio.'
        elif not fecha:
            error = 'Fecha de la factura es obligatoria.'
        elif not monto_total:
            error = 'Monto total es obligatorio.'
        elif not metodo_pago:
            error = 'Método de pago es obligatorio.'
        elif not numero_de_orden.isdigit():
            error = 'Orden asociada a la factura es obligatoria.'

        if error is None:
            try:
                cur.execute(
                    """
                    INSERT INTO public.factura
                        (numero_de_factura, fecha, monto_total, metodo_de_pago, numero_de_orden)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        int(numero_factura), fecha, monto_total, metodo_pago,
                        int(numero_de_orden),
                    ),
                )
                db.commit()
                flash('Factura registrada correctamente.')
                return redirect(url_for('logistics.list_receipts'))
            except Exception:
                db.rollback()
                error = 'No se pudo registrar la factura. Verifica que el número no exista y la orden sea válida.'

        flash(error)

    return render_template(
        'receipts/form.html',
        pending_orders=pending_orders,
        payment_methods=PAYMENT_METHODS,
    )


@bp.route('/receipts/<int:receipt_id>')
@login_required
def receipt_detail(receipt_id):
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT f.numero_de_factura, f.fecha, f.monto_total, f.metodo_de_pago, f.numero_de_orden,
               o.direccion_origen, o.direccion_destino, o.rut_cliente, o.estado AS orden_estado,
               COALESCE(p.nombre, e.razon_social, c.email, c.rut) AS cliente_nombre,
               c.email AS cliente_email, c.fono AS cliente_fono
        FROM public.factura f
        JOIN public.ordenderetiro o ON o.numero_de_orden = f.numero_de_orden
        JOIN public.cliente c ON c.rut = o.rut_cliente
        LEFT JOIN public.persona p ON p.rut = c.rut
        LEFT JOIN public.empresa e ON e.rut = c.rut
        WHERE f.numero_de_factura = %s
        """,
        (receipt_id,),
    )
    invoice = cur.fetchone()
    if invoice is None:
        abort(404)
    if not _is_admin() and invoice.rut_cliente != g.user['rut']:
        abort(404)

    subtotal = float(invoice.monto_total) / 1.19
    iva = float(invoice.monto_total) - subtotal
    return render_template('receipts/detail.html', invoice=invoice, subtotal=subtotal, iva=iva)


@bp.route('/payments')
@login_required
def list_payments():
    db = get_db()
    cur = db.cursor()
    base_query = """
        SELECT o.numero_de_orden, o.fecha, o.monto_base, o.estado,
               COALESCE(p.nombre, e.razon_social, c.email, c.rut) AS cliente_nombre,
               f.numero_de_factura, f.metodo_de_pago
        FROM public.ordenderetiro o
        JOIN public.cliente c ON c.rut = o.rut_cliente
        LEFT JOIN public.persona p ON p.rut = c.rut
        LEFT JOIN public.empresa e ON e.rut = c.rut
        LEFT JOIN public.factura f ON f.numero_de_orden = o.numero_de_orden
    """
    if _is_admin():
        cur.execute(base_query + " ORDER BY o.fecha DESC, o.numero_de_orden DESC")
    else:
        cur.execute(base_query + " WHERE o.rut_cliente = %s ORDER BY o.fecha DESC, o.numero_de_orden DESC",
                    (g.user['rut'],))
    rows = cur.fetchall()

    pending_total = sum(float(r.monto_base) for r in rows if r.numero_de_factura is None)
    paid_total = sum(float(r.monto_base) for r in rows if r.numero_de_factura is not None)

    return render_template('payments/list.html', rows=rows, pending_total=pending_total, paid_total=paid_total)


@bp.route('/payments/<int:order_id>/pay', methods=('GET', 'POST'))
@login_required
def pay_order(order_id):
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT o.numero_de_orden, o.monto_base, o.rut_cliente, o.estado,
               COALESCE(p.nombre, e.razon_social, c.email, c.rut) AS cliente_nombre
        FROM public.ordenderetiro o
        JOIN public.cliente c ON c.rut = o.rut_cliente
        LEFT JOIN public.persona p ON p.rut = c.rut
        LEFT JOIN public.empresa e ON e.rut = c.rut
        WHERE o.numero_de_orden = %s
        """,
        (order_id,),
    )
    order_record = cur.fetchone()
    if order_record is None:
        abort(404)
    if not _is_admin() and order_record.rut_cliente != g.user['rut']:
        abort(404)

    cur.execute("SELECT numero_de_factura FROM public.factura WHERE numero_de_orden = %s", (order_id,))
    already_paid = cur.fetchone() is not None

    error = None
    if request.method == 'POST' and not already_paid:
        metodo_pago = request.form.get('metodo_de_pago', '').strip()
        if not metodo_pago:
            error = 'Selecciona un método de pago.'
        if error is None:
            try:
                cur.execute("SELECT COALESCE(MAX(numero_de_factura), 0) + 1 FROM public.factura")
                next_invoice = cur.fetchone()[0]
                cur.execute(
                    """
                    INSERT INTO public.factura (numero_de_factura, fecha, monto_total, metodo_de_pago, numero_de_orden)
                    VALUES (%s, CURRENT_DATE, %s, %s, %s)
                    """,
                    (next_invoice, order_record.monto_base, metodo_pago, order_id),
                )
                db.commit()
                flash('Pago registrado correctamente. Se generó la factura N.º {}.'.format(next_invoice))
                return redirect(url_for('logistics.receipt_detail', receipt_id=next_invoice))
            except Exception:
                db.rollback()
                error = 'No se pudo procesar el pago. Intenta nuevamente.'
        flash(error)

    return render_template(
        'payments/pay.html',
        order=order_record,
        already_paid=already_paid,
        payment_methods=PAYMENT_METHODS,
    )


@bp.route('/clients')
@admin_required
def list_clients():
    db = get_db()
    cur = db.cursor()
    cur.execute(
        """
        SELECT c.rut,
               COALESCE(p.nombre, e.razon_social, c.email, c.rut) AS cliente_nombre,
               c.email,
               c.fono,
               c.direccion_residencia,
               com.nombre AS comuna,
               CASE WHEN e.rut IS NULL THEN 'Persona' ELSE 'Empresa' END AS tipo
        FROM public.cliente c
        LEFT JOIN public.persona p ON p.rut = c.rut
        LEFT JOIN public.empresa e ON e.rut = c.rut
        JOIN public.comuna com ON c.id_comuna = com.id_comuna
        ORDER BY cliente_nombre
        """
    )
    clients = cur.fetchall()
    return render_template('clients/list.html', clients=clients)
