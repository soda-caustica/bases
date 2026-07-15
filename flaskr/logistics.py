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


def _find_available_grua(cur, id_comuna_origen):
    """Busca una grúa disponible, priorizando la sucursal de la comuna de
    origen, luego cualquier sucursal de la misma región, y por último
    cualquier grúa disponible en cualquier sucursal."""
    cur.execute(
        """
        SELECT g.patente
        FROM public.grua g
        JOIN public.vehiculo v ON v.patente = g.patente
        JOIN public.sucursal s ON s.id_sucursal = v.id_sucursal_esta
        WHERE g.estado = 'disponible' AND s.id_comuna = %s
        LIMIT 1
        """,
        (id_comuna_origen,),
    )
    row = cur.fetchone()
    if row:
        return row.patente

    cur.execute(
        """
        SELECT g.patente
        FROM public.grua g
        JOIN public.vehiculo v ON v.patente = g.patente
        JOIN public.sucursal s ON s.id_sucursal = v.id_sucursal_esta
        JOIN public.comuna com ON com.id_comuna = s.id_comuna
        WHERE g.estado = 'disponible'
          AND com.id_region = (SELECT id_region FROM public.comuna WHERE id_comuna = %s)
        LIMIT 1
        """,
        (id_comuna_origen,),
    )
    row = cur.fetchone()
    if row:
        return row.patente

    cur.execute("SELECT patente FROM public.grua WHERE estado = 'disponible' LIMIT 1")
    row = cur.fetchone()
    return row.patente if row else None


def _find_available_chofer(cur):
    """No existe información de sucursal para choferes en el esquema actual,
    así que se asigna cualquiera disponible."""
    cur.execute("SELECT rut FROM public.chofer WHERE disponibilidad = true LIMIT 1")
    row = cur.fetchone()
    return row.rut if row else None


def _assign_resources_to_order(cur, numero_de_orden, id_comuna_origen):
    """Crea el registro de 'retiro' (despacho) asignando una grúa y un chofer
    disponibles a la orden recién creada, dejándolos marcados como ocupados.
    Devuelve un dict con lo asignado, o None si no había recursos libres."""
    patente_grua = _find_available_grua(cur, id_comuna_origen)
    rut_chofer = _find_available_chofer(cur)
    if patente_grua is None or rut_chofer is None:
        return None

    cur.execute("SELECT COALESCE(MAX(id_retiro), 0) + 1 FROM public.retiro")
    next_retiro = cur.fetchone()[0]
    cur.execute(
        """
        INSERT INTO public.retiro (id_retiro, fecha_hora, estado, rut_chofer, numero_de_orden)
        VALUES (%s, NOW(), 'realizado', %s, %s)
        """,
        (next_retiro, rut_chofer, numero_de_orden),
    )
    cur.execute(
        "INSERT INTO public.grua_retiro (patente_grua, id_retiro) VALUES (%s, %s)",
        (patente_grua, next_retiro),
    )
    cur.execute("UPDATE public.grua SET estado = 'en_transito' WHERE patente = %s", (patente_grua,))
    cur.execute("UPDATE public.chofer SET disponibilidad = false WHERE rut = %s", (rut_chofer,))
    return {'patente_grua': patente_grua, 'rut_chofer': rut_chofer, 'id_retiro': next_retiro}


def _release_resources_for_order(cur, numero_de_orden):
    """Al completar/cancelar una orden, libera la(s) grúa(s) y chofer(es)
    que quedaron asociados a sus despachos (retiro) para que vuelvan a
    estar disponibles."""
    cur.execute(
        """
        SELECT gr.patente_grua, r.rut_chofer
        FROM public.retiro r
        JOIN public.grua_retiro gr ON gr.id_retiro = r.id_retiro
        WHERE r.numero_de_orden = %s AND r.estado = 'realizado'
        """,
        (numero_de_orden,),
    )
    rows = cur.fetchall()
    for row in rows:
        cur.execute(
            "UPDATE public.grua SET estado = 'disponible' WHERE patente = %s AND estado = 'en_transito'",
            (row.patente_grua,),
        )
        cur.execute("UPDATE public.chofer SET disponibilidad = true WHERE rut = %s", (row.rut_chofer,))


def _fetch_order_dispatch(cur, numero_de_orden):
    """Devuelve la grúa y el chofer asignados (si existen) a una orden."""
    cur.execute(
        """
        SELECT r.id_retiro, r.fecha_hora, r.estado AS retiro_estado,
               r.observacion_de_servicio AS retiro_observacion,
               gr.patente_grua, r.rut_chofer,
               p.nombre AS chofer_nombre,
               s.nombre AS sucursal_nombre
        FROM public.retiro r
        JOIN public.grua_retiro gr ON gr.id_retiro = r.id_retiro
        LEFT JOIN public.persona p ON p.rut = r.rut_chofer
        LEFT JOIN public.vehiculo v ON v.patente = gr.patente_grua
        LEFT JOIN public.sucursal s ON s.id_sucursal = v.id_sucursal_esta
        WHERE r.numero_de_orden = %s
        ORDER BY r.fecha_hora DESC
        LIMIT 1
        """,
        (numero_de_orden,),
    )
    return cur.fetchone()


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

    filtro_estado = request.args.get('estado', '').strip()
    filtro_desde = request.args.get('desde', '').strip()
    filtro_hasta = request.args.get('hasta', '').strip()
    filtro_busqueda = request.args.get('q', '').strip()
    filtro_con_retiro = request.args.get('con_retiro', '').strip()
    filtro_chofer = request.args.get('rut_chofer', '').strip()
    filtro_grua = request.args.get('patente_grua', '').strip()

    query = """
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
        WHERE 1 = 1
    """
    params = []

    if not _is_admin():
        query += " AND o.rut_cliente = %s"
        params.append(g.user['rut'])
    if filtro_estado:
        query += " AND o.estado = %s"
        params.append(filtro_estado)
    if filtro_desde:
        query += " AND o.fecha >= %s"
        params.append(filtro_desde)
    if filtro_hasta:
        query += " AND o.fecha <= %s"
        params.append(filtro_hasta)
    if filtro_busqueda:
        query += " AND (o.rut_cliente ILIKE %s OR p.nombre ILIKE %s OR e.razon_social ILIKE %s)"
        like = '%{}%'.format(filtro_busqueda)
        params.extend([like, like, like])
    if filtro_con_retiro == 'con_retiro':
        query += " AND EXISTS (SELECT 1 FROM public.retiro r WHERE r.numero_de_orden = o.numero_de_orden)"
    elif filtro_con_retiro == 'sin_retiro':
        query += " AND NOT EXISTS (SELECT 1 FROM public.retiro r WHERE r.numero_de_orden = o.numero_de_orden)"
    if filtro_chofer:
        query += " AND EXISTS (SELECT 1 FROM public.retiro r WHERE r.numero_de_orden = o.numero_de_orden AND r.rut_chofer = %s)"
        params.append(filtro_chofer)
    if filtro_grua:
        query += " AND EXISTS (SELECT 1 FROM public.retiro r JOIN public.grua_retiro gr ON gr.id_retiro = r.id_retiro WHERE r.numero_de_orden = o.numero_de_orden AND gr.patente_grua = %s)"
        params.append(filtro_grua)

    query += " ORDER BY o.fecha DESC, o.numero_de_orden DESC"
    cur.execute(query, params)
    orders = cur.fetchall()

    drivers = []
    gruas = []
    if _is_admin():
        cur.execute("""
            SELECT ch.rut, p.nombre
            FROM public.chofer ch
            JOIN public.persona p ON p.rut = ch.rut
            ORDER BY p.nombre
        """)
        drivers = cur.fetchall()
        cur.execute("SELECT patente FROM public.grua ORDER BY patente")
        gruas = cur.fetchall()

    return render_template(
        'orders/list.html',
        orders=orders,
        states=ORDER_STATES,
        drivers=drivers,
        gruas=gruas,
        filters={
            'estado': filtro_estado,
            'desde': filtro_desde,
            'hasta': filtro_hasta,
            'q': filtro_busqueda,
            'con_retiro': filtro_con_retiro,
            'rut_chofer': filtro_chofer,
            'patente_grua': filtro_grua
        },
    )



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
        fecha = request.form.get('fecha', '').strip()
        monto = request.form.get('monto_base', '').strip()
        estado = request.form.get('estado', 'pendiente').strip()
        rut_cliente = request.form.get('rut_cliente', '').strip() if _is_admin() else g.user['rut']
        direccion_origen = request.form.get('direccion_origen', '').strip()
        direccion_destino = request.form.get('direccion_destino', '').strip()
        id_comuna_origen = request.form.get('id_comuna_origen', '').strip()
        id_comuna_destino = request.form.get('id_comuna_destino', '').strip()

        # Datos del vehículo
        vehiculo_patente = request.form.get('vehiculo_patente', '').strip().upper()
        vehiculo_tipo = request.form.get('vehiculo_tipo', '').strip()
        vehiculo_observacion = request.form.get('vehiculo_observacion', '').strip()

        if not fecha:
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
        elif not vehiculo_patente:
            error = 'La patente del vehículo a retirar es obligatoria.'
        elif not vehiculo_tipo:
            error = 'El tipo de vehículo a retirar es obligatorio.'

        if error is None:
            try:
                # 1. Insertar orden de retiro
                cur.execute(
                    """
                    INSERT INTO public.ordenderetiro
                        (fecha, monto_base, estado, rut_cliente,
                         direccion_origen, direccion_destino, id_comuna_origen, id_comuna_destino)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING numero_de_orden
                    """,
                    (
                        fecha, monto, estado, rut_cliente,
                        direccion_origen, direccion_destino,
                        int(id_comuna_origen), int(id_comuna_destino),
                    ),
                )
                new_order_id = cur.fetchone()[0]

                # 2. Insertar / Actualizar vehículo
                cur.execute("SELECT patente FROM public.vehiculo WHERE patente = %s", (vehiculo_patente,))
                veh_exists = cur.fetchone() is not None
                if not veh_exists:
                    cur.execute("INSERT INTO public.vehiculo (patente, id_sucursal_esta) VALUES (%s, NULL)", (vehiculo_patente,))
                    cur.execute(
                        "INSERT INTO public.vehiculoretirable (patente, observacion, tipo, estado) VALUES (%s, %s, %s, 'en_traslado')",
                        (vehiculo_patente, vehiculo_observacion, vehiculo_tipo)
                    )
                else:
                    # Si ya existía, actualizamos sus datos para este nuevo traslado
                    cur.execute(
                        "UPDATE public.vehiculoretirable SET observacion = %s, tipo = %s, estado = 'en_traslado' WHERE patente = %s",
                        (vehiculo_observacion, vehiculo_tipo, vehiculo_patente)
                    )

                # 3. Vincular vehículo a orden
                cur.execute(
                    "INSERT INTO public.vehiculoretirable_orden (patente_vehiculo, numero_de_orden) VALUES (%s, %s)",
                    (vehiculo_patente, new_order_id)
                )

                # 4. Asignar chofer y grúa si están disponibles
                assignment = _assign_resources_to_order(cur, new_order_id, int(id_comuna_origen))
                db.commit()

                if assignment:
                    flash(
                        'Orden de retiro creada correctamente. Vehículo {} registrado. Grúa {} y chofer {} asignados.'.format(
                            vehiculo_patente, assignment['patente_grua'], assignment['rut_chofer']
                        ),
                        'success'
                    )
                else:
                    flash('Orden de retiro creada correctamente. Vehículo {} registrado. No había grúas o choferes disponibles; asígnalos manualmente más tarde.'.format(vehiculo_patente), 'success')
                return redirect(url_for('logistics.list_orders'))
            except Exception:
                db.rollback()
                error = 'No se pudo crear la orden. Verifica que los datos sean válidos.'

        flash(error, 'error')

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
               co.nombre AS comuna_origen_nombre,
               cd.nombre AS comuna_destino_nombre,
               COALESCE(p.nombre, e.razon_social, c.email, c.rut) AS cliente_nombre,
               c.email AS cliente_email,
               c.fono AS cliente_fono,
               vr.patente AS vehiculo_patente,
               vr.tipo AS vehiculo_tipo,
               vr.observacion AS vehiculo_observacion,
               vr.estado AS vehiculo_estado
        FROM public.ordenderetiro o
        JOIN public.cliente c ON c.rut = o.rut_cliente
        LEFT JOIN public.persona p ON p.rut = c.rut
        LEFT JOIN public.empresa e ON e.rut = c.rut
        LEFT JOIN public.comuna co ON co.id_comuna = o.id_comuna_origen
        LEFT JOIN public.comuna cd ON cd.id_comuna = o.id_comuna_destino
        LEFT JOIN public.vehiculoretirable_orden vro ON vro.numero_de_orden = o.numero_de_orden
        LEFT JOIN public.vehiculoretirable vr ON vr.patente = vro.patente_vehiculo
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
    dispatch = _fetch_order_dispatch(cur, order_id)

    return render_template(
        'orders/detail.html',
        order=order_record,
        receipt=receipt,
        dispatch=dispatch,
        is_admin=_is_admin(),
        states=ORDER_STATES,
    )


@bp.route('/orders/<int:order_id>/status', methods=('POST',))
@admin_required
def update_order_status(order_id):
    db = get_db()
    cur = db.cursor()
    new_status = request.form.get('estado', '').strip()
    valid_states = {value for value, _ in ORDER_STATES}
    if new_status not in valid_states:
        flash('Estado no válido.', 'error')
        return redirect(url_for('logistics.order_detail', order_id=order_id))

    try:
        cur.execute(
            "UPDATE public.ordenderetiro SET estado = %s WHERE numero_de_orden = %s",
            (new_status, order_id),
        )
        if new_status in ('completada', 'cancelada'):
            _release_resources_for_order(cur, order_id)
        db.commit()
        flash('Estado de la orden actualizado.', 'success')
    except Exception:
        db.rollback()
        flash('No se pudo actualizar el estado de la orden.', 'error')

    return redirect(url_for('logistics.order_detail', order_id=order_id))


@bp.route('/orders/<int:order_id>/assign', methods=('POST',))
@admin_required
def assign_order_resources(order_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id_comuna_origen FROM public.ordenderetiro WHERE numero_de_orden = %s", (order_id,))
    row = cur.fetchone()
    if row is None:
        abort(404)
    try:
        assignment = _assign_resources_to_order(cur, order_id, row.id_comuna_origen)
        db.commit()
        if assignment:
            flash('Grúa {} y chofer {} asignados a la orden.'.format(assignment['patente_grua'], assignment['rut_chofer']), 'success')
        else:
            flash('No hay grúas o choferes disponibles en este momento.', 'error')
    except Exception:
        db.rollback()
        flash('No se pudo realizar la asignación.', 'error')
    return redirect(url_for('logistics.order_detail', order_id=order_id))


@bp.route('/receipts')
@login_required
def list_receipts():
    db = get_db()
    cur = db.cursor()

    filtro_desde = request.args.get('desde', '').strip()
    filtro_hasta = request.args.get('hasta', '').strip()
    filtro_busqueda = request.args.get('q', '').strip()
    filtro_empresa = request.args.get('rut_empresa', '').strip()

    query = """
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
        WHERE 1 = 1
    """
    params = []
    if not _is_admin():
        query += " AND o.rut_cliente = %s"
        params.append(g.user['rut'])
    if filtro_desde:
        query += " AND f.fecha >= %s"
        params.append(filtro_desde)
    if filtro_hasta:
        query += " AND f.fecha <= %s"
        params.append(filtro_hasta)
    if filtro_busqueda:
        query += " AND (o.rut_cliente ILIKE %s OR p.nombre ILIKE %s OR e.razon_social ILIKE %s)"
        like = '%{}%'.format(filtro_busqueda)
        params.extend([like, like, like])
    if filtro_empresa:
        query += " AND e.rut = %s"
        params.append(filtro_empresa)

    query += " ORDER BY f.fecha DESC, f.numero_de_factura DESC"
    cur.execute(query, params)
    receipts = cur.fetchall()

    companies = []
    if _is_admin():
        cur.execute("SELECT rut, razon_social FROM public.empresa ORDER BY razon_social")
        companies = cur.fetchall()

    if _is_admin():
        cur.execute("""
            SELECT metodo_de_pago, SUM(monto_total) AS total
            FROM public.factura
            GROUP BY metodo_de_pago
            ORDER BY total DESC
        """)
    else:
        cur.execute("""
            SELECT f.metodo_de_pago, SUM(f.monto_total) AS total
            FROM public.factura f
            JOIN public.ordenderetiro o ON o.numero_de_orden = f.numero_de_orden
            WHERE o.rut_cliente = %s
            GROUP BY f.metodo_de_pago
            ORDER BY total DESC
        """, (g.user['rut'],))
    payments_by_method = cur.fetchall()

    return render_template(
        'receipts/list.html',
        receipts=receipts,
        is_admin=_is_admin(),
        companies=companies,
        payments_by_method=payments_by_method,
        filters={
            'desde': filtro_desde,
            'hasta': filtro_hasta,
            'q': filtro_busqueda,
            'rut_empresa': filtro_empresa
        },
    )


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
        fecha = request.form.get('fecha', '').strip()
        monto_total = request.form.get('monto_total', '').strip()
        metodo_pago = request.form.get('metodo_de_pago', '').strip()
        numero_de_orden = request.form.get('numero_de_orden', '').strip()

        error = None
        if not fecha:
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
                        (fecha, monto_total, metodo_de_pago, numero_de_orden)
                    VALUES (%s, %s, %s, %s)
                    RETURNING numero_de_factura
                    """,
                    (
                        fecha, monto_total, metodo_pago,
                        int(numero_de_orden),
                    ),
                )
                db.commit()
                flash('Factura registrada correctamente.', 'success')
                return redirect(url_for('logistics.list_receipts'))
            except Exception:
                db.rollback()
                error = 'No se pudo registrar la factura. Verifica que la orden sea válida.'

        flash(error, 'error')

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
               co.nombre AS comuna_origen_nombre, cd.nombre AS comuna_destino_nombre,
               COALESCE(p.nombre, e.razon_social, c.email, c.rut) AS cliente_nombre,
               c.email AS cliente_email, c.fono AS cliente_fono,
               vr.patente AS vehiculo_patente, vr.tipo AS vehiculo_tipo
        FROM public.factura f
        JOIN public.ordenderetiro o ON o.numero_de_orden = f.numero_de_orden
        JOIN public.cliente c ON c.rut = o.rut_cliente
        LEFT JOIN public.persona p ON p.rut = c.rut
        LEFT JOIN public.empresa e ON e.rut = c.rut
        LEFT JOIN public.comuna co ON co.id_comuna = o.id_comuna_origen
        LEFT JOIN public.comuna cd ON cd.id_comuna = o.id_comuna_destino
        LEFT JOIN public.vehiculoretirable_orden vro ON vro.numero_de_orden = o.numero_de_orden
        LEFT JOIN public.vehiculoretirable vr ON vr.patente = vro.patente_vehiculo
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
    filtro_estado_pago = request.args.get('estado_pago', '').strip()  # 'pagado' / 'pendiente'
    filtro_desde = request.args.get('desde', '').strip()
    filtro_hasta = request.args.get('hasta', '').strip()
    filtro_busqueda = request.args.get('q', '').strip()
    filtro_empresa = request.args.get('rut_empresa', '').strip()

    base_query = """
        SELECT o.numero_de_orden, o.fecha, o.monto_base, o.estado,
               COALESCE(p.nombre, e.razon_social, c.email, c.rut) AS cliente_nombre,
               f.numero_de_factura, f.metodo_de_pago
        FROM public.ordenderetiro o
        JOIN public.cliente c ON c.rut = o.rut_cliente
        LEFT JOIN public.persona p ON p.rut = c.rut
        LEFT JOIN public.empresa e ON e.rut = c.rut
        LEFT JOIN public.factura f ON f.numero_de_orden = o.numero_de_orden
        WHERE 1 = 1
    """
    params = []
    if not _is_admin():
        base_query += " AND o.rut_cliente = %s"
        params.append(g.user['rut'])
    if filtro_estado_pago == 'pagado':
        base_query += " AND f.numero_de_factura IS NOT NULL"
    elif filtro_estado_pago == 'pendiente':
        base_query += " AND f.numero_de_factura IS NULL"
    if filtro_desde:
        base_query += " AND o.fecha >= %s"
        params.append(filtro_desde)
    if filtro_hasta:
        base_query += " AND o.fecha <= %s"
        params.append(filtro_hasta)
    if filtro_busqueda:
        base_query += " AND (o.rut_cliente ILIKE %s OR p.nombre ILIKE %s OR e.razon_social ILIKE %s)"
        like = '%{}%'.format(filtro_busqueda)
        params.extend([like, like, like])
    if filtro_empresa:
        base_query += " AND e.rut = %s"
        params.append(filtro_empresa)

    base_query += " ORDER BY o.fecha DESC, o.numero_de_orden DESC"
    cur.execute(base_query, params)
    rows = cur.fetchall()

    # Calcular totales globales (independientes de los filtros de fecha, estado y búsqueda, pero respetando empresa)
    tot_query = """
        SELECT o.monto_base, f.numero_de_factura
        FROM public.ordenderetiro o
        LEFT JOIN public.factura f ON f.numero_de_orden = o.numero_de_orden
        JOIN public.cliente c ON c.rut = o.rut_cliente
        LEFT JOIN public.empresa e ON e.rut = c.rut
        WHERE 1 = 1
    """
    tot_params = []
    if not _is_admin():
        tot_query += " AND o.rut_cliente = %s"
        tot_params.append(g.user['rut'])
    if filtro_empresa:
        tot_query += " AND e.rut = %s"
        tot_params.append(filtro_empresa)

    cur.execute(tot_query, tot_params)
    all_rows = cur.fetchall()

    pending_total = sum(float(r.monto_base) for r in all_rows if r.numero_de_factura is None)
    paid_total = sum(float(r.monto_base) for r in all_rows if r.numero_de_factura is not None)

    companies = []
    if _is_admin():
        cur.execute("SELECT rut, razon_social FROM public.empresa ORDER BY razon_social")
        companies = cur.fetchall()

    if _is_admin():
        cur.execute("""
            SELECT metodo_de_pago, SUM(monto_total) AS total
            FROM public.factura
            GROUP BY metodo_de_pago
            ORDER BY total DESC
        """)
    else:
        cur.execute("""
            SELECT f.metodo_de_pago, SUM(f.monto_total) AS total
            FROM public.factura f
            JOIN public.ordenderetiro o ON o.numero_de_orden = f.numero_de_orden
            WHERE o.rut_cliente = %s
            GROUP BY f.metodo_de_pago
            ORDER BY total DESC
        """, (g.user['rut'],))
    payments_by_method = cur.fetchall()

    return render_template(
        'payments/list.html',
        rows=rows,
        pending_total=pending_total,
        paid_total=paid_total,
        companies=companies,
        payments_by_method=payments_by_method,
        filters={
            'estado_pago': filtro_estado_pago,
            'desde': filtro_desde,
            'hasta': filtro_hasta,
            'q': filtro_busqueda,
            'rut_empresa': filtro_empresa
        },
    )


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
                cur.execute(
                    """
                    INSERT INTO public.factura (fecha, monto_total, metodo_de_pago, numero_de_orden)
                    VALUES (CURRENT_DATE, %s, %s, %s)
                    RETURNING numero_de_factura
                    """,
                    (order_record.monto_base, metodo_pago, order_id),
                )
                new_invoice = cur.fetchone()[0]
                db.commit()
                flash('Pago registrado correctamente. Se generó la factura N.º {}.'.format(new_invoice), 'success')
                return redirect(url_for('logistics.receipt_detail', receipt_id=new_invoice))
            except Exception:
                db.rollback()
                error = 'No se pudo procesar el pago. Intenta nuevamente.'
        flash(error, 'error')

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

    filtro_tipo = request.args.get('tipo', '').strip()  # 'persona' / 'empresa'
    filtro_comuna = request.args.get('id_comuna', '').strip()
    filtro_busqueda = request.args.get('q', '').strip()

    query = """
        SELECT c.rut,
               COALESCE(p.nombre, e.razon_social, c.email, c.rut) AS cliente_nombre,
               c.email,
               c.fono,
               c.direccion_residencia,
               com.nombre AS comuna,
               c.id_comuna,
               CASE
                   WHEN p.rut IS NOT NULL THEN 'Persona'
                   WHEN e.rut IS NOT NULL THEN 'Empresa'
               END AS tipo
        FROM public.cliente c
        LEFT JOIN public.persona p ON p.rut = c.rut
        LEFT JOIN public.empresa e ON e.rut = c.rut
        JOIN public.comuna com ON c.id_comuna = com.id_comuna
        WHERE 1 = 1
    """
    params = []
    if filtro_tipo == 'persona':
        query += " AND e.rut IS NULL"
    elif filtro_tipo == 'empresa':
        query += " AND e.rut IS NOT NULL"
    if filtro_comuna.isdigit():
        query += " AND c.id_comuna = %s"
        params.append(int(filtro_comuna))
    if filtro_busqueda:
        query += " AND (c.rut ILIKE %s OR p.nombre ILIKE %s OR e.razon_social ILIKE %s)"
        like = '%{}%'.format(filtro_busqueda)
        params.extend([like, like, like])

    query += " ORDER BY cliente_nombre"
    cur.execute(query, params)
    clients = cur.fetchall()

    cur.execute("SELECT id_comuna, nombre FROM public.comuna ORDER BY nombre")
    communes = cur.fetchall()

    return render_template(
        'clients/list.html',
        clients=clients,
        communes=communes,
        filters={'tipo': filtro_tipo, 'id_comuna': filtro_comuna, 'q': filtro_busqueda},
    )


@bp.route('/vehicles')
@admin_required
def list_vehicles():
    """Vehículos retirables de órdenes ya completadas: lo que la empresa
    tiene actualmente en sus instalaciones (o en traslado hacia ellas)."""
    db = get_db()
    cur = db.cursor()

    filtro_estado = request.args.get('estado', '').strip()
    filtro_comuna = request.args.get('id_comuna', '').strip()
    filtro_busqueda = request.args.get('q', '').strip()
    filtro_tipo = request.args.get('tipo', '').strip()

    query = """
        SELECT vr.patente, vr.tipo, vr.observacion, vr.estado,
               s.nombre AS sucursal, com.nombre AS comuna, com.id_comuna,
               o.numero_de_orden,
               COALESCE(p.nombre, e.razon_social, cl.email, cl.rut) AS cliente_nombre
        FROM public.vehiculoretirable vr
        JOIN public.vehiculo v ON v.patente = vr.patente
        LEFT JOIN public.sucursal s ON s.id_sucursal = v.id_sucursal_esta
        LEFT JOIN public.comuna com ON com.id_comuna = s.id_comuna
        LEFT JOIN public.vehiculoretirable_orden vro ON vro.patente_vehiculo = vr.patente
        LEFT JOIN public.ordenderetiro o ON o.numero_de_orden = vro.numero_de_orden
        LEFT JOIN public.cliente cl ON cl.rut = o.rut_cliente
        LEFT JOIN public.persona p ON p.rut = cl.rut
        LEFT JOIN public.empresa e ON e.rut = cl.rut
        WHERE 1 = 1
    """
    params = []
    if filtro_estado:
        query += " AND vr.estado = %s"
        params.append(filtro_estado)
    if filtro_comuna.isdigit():
        query += " AND com.id_comuna = %s"
        params.append(int(filtro_comuna))
    if filtro_busqueda:
        query += " AND (vr.patente ILIKE %s OR p.nombre ILIKE %s OR e.razon_social ILIKE %s)"
        like = '%{}%'.format(filtro_busqueda)
        params.extend([like, like, like])
    if filtro_tipo:
        query += " AND vr.tipo = %s"
        params.append(filtro_tipo)

    query += " ORDER BY vr.patente"
    cur.execute(query, params)
    vehicles = cur.fetchall()

    cur.execute("SELECT id_comuna, nombre FROM public.comuna ORDER BY nombre")
    communes = cur.fetchall()

    cur.execute("SELECT DISTINCT tipo FROM public.vehiculoretirable WHERE tipo IS NOT NULL AND tipo <> '' ORDER BY tipo")
    types = [row.tipo for row in cur.fetchall()]

    return render_template(
        'vehicles/list.html',
        vehicles=vehicles,
        communes=communes,
        types=types,
        filters={
            'estado': filtro_estado,
            'id_comuna': filtro_comuna,
            'q': filtro_busqueda,
            'tipo': filtro_tipo
        },
    )


@bp.route('/gruas')
@admin_required
def list_gruas():
    db = get_db()
    cur = db.cursor()

    filtro_estado = request.args.get('estado', '').strip()
    filtro_comuna = request.args.get('id_comuna', '').strip()
    filtro_busqueda = request.args.get('q', '').strip()

    query = """
        SELECT g.patente, g.estado,
               s.nombre AS sucursal, com.nombre AS comuna, com.id_comuna
        FROM public.grua g
        JOIN public.vehiculo v ON v.patente = g.patente
        LEFT JOIN public.sucursal s ON s.id_sucursal = v.id_sucursal_esta
        LEFT JOIN public.comuna com ON com.id_comuna = s.id_comuna
        WHERE 1 = 1
    """
    params = []
    if filtro_estado:
        query += " AND g.estado = %s"
        params.append(filtro_estado)
    if filtro_comuna.isdigit():
        query += " AND com.id_comuna = %s"
        params.append(int(filtro_comuna))
    if filtro_busqueda:
        query += " AND g.patente ILIKE %s"
        params.append('%{}%'.format(filtro_busqueda))

    query += " ORDER BY g.patente"
    cur.execute(query, params)
    gruas = cur.fetchall()

    cur.execute("SELECT id_comuna, nombre FROM public.comuna ORDER BY nombre")
    communes = cur.fetchall()

    return render_template(
        'gruas/list.html',
        gruas=gruas,
        communes=communes,
        filters={
            'estado': filtro_estado,
            'id_comuna': filtro_comuna,
            'q': filtro_busqueda
        },
    )


@bp.route('/gruas/<patente>/status', methods=('POST',))
@admin_required
def update_grua_status(patente):
    db = get_db()
    cur = db.cursor()
    new_status = request.form.get('estado', '').strip()
    valid_states = {'disponible', 'en_transito', 'mantenimiento'}

    if new_status not in valid_states:
        flash('Estado de grúa no válido.', 'error')
        return redirect(url_for('logistics.list_gruas'))

    try:
        cur.execute(
            "UPDATE public.grua SET estado = %s WHERE patente = %s",
            (new_status, patente)
        )
        db.commit()
        flash(f'Estado de la grúa {patente} actualizado a {new_status}.', 'success')
    except Exception:
        db.rollback()
        flash('No se pudo actualizar el estado de la grúa.', 'error')

    return redirect(url_for('logistics.list_gruas'))


@bp.route('/employees')
@admin_required
def list_employees():
    """Choferes registrados. Se llama 'Empleados' para poder escalar a
    futuro a otros tipos de personal sin cambiar el nombre de la sección."""
    db = get_db()
    cur = db.cursor()

    filtro_disponibilidad = request.args.get('disponibilidad', '').strip()  # 'true' / 'false'
    filtro_comuna = request.args.get('id_comuna', '').strip()
    filtro_busqueda = request.args.get('q', '').strip()

    query = """
        SELECT ch.rut, ch.disponibilidad,
               p.nombre, cl.email, cl.fono, com.nombre AS comuna, com.id_comuna
        FROM public.chofer ch
        JOIN public.persona p ON p.rut = ch.rut
        JOIN public.cliente cl ON cl.rut = ch.rut
        LEFT JOIN public.comuna com ON com.id_comuna = cl.id_comuna
        WHERE 1 = 1
    """
    params = []
    if filtro_disponibilidad in ('true', 'false'):
        query += " AND ch.disponibilidad = %s"
        params.append(filtro_disponibilidad == 'true')
    if filtro_comuna.isdigit():
        query += " AND cl.id_comuna = %s"
        params.append(int(filtro_comuna))
    if filtro_busqueda:
        query += " AND (ch.rut ILIKE %s OR p.nombre ILIKE %s)"
        like = '%{}%'.format(filtro_busqueda)
        params.extend([like, like])

    query += " ORDER BY p.nombre"
    cur.execute(query, params)
    employees = cur.fetchall()

    cur.execute("SELECT id_comuna, nombre FROM public.comuna ORDER BY nombre")
    communes = cur.fetchall()

    return render_template(
        'employees/list.html',
        employees=employees,
        communes=communes,
        filters={'disponibilidad': filtro_disponibilidad, 'id_comuna': filtro_comuna, 'q': filtro_busqueda},
    )


@bp.route('/sucursales')
@admin_required
def list_sucursales():
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT s.id_sucursal, s.nombre, s.direccion, c.nombre AS comuna,
               (SELECT COUNT(*) FROM public.vehiculoretirable vr JOIN public.vehiculo v ON vr.patente = v.patente WHERE v.id_sucursal_esta = s.id_sucursal) AS cant_retirables,
               (SELECT COUNT(*) FROM public.grua g JOIN public.vehiculo v ON g.patente = v.patente WHERE v.id_sucursal_esta = s.id_sucursal) AS cant_gruas
        FROM public.sucursal s
        JOIN public.comuna c ON s.id_comuna = c.id_comuna
        ORDER BY s.nombre
    """)
    sucursales = cur.fetchall()
    return render_template('sucursales/list.html', sucursales=sucursales)


@bp.route('/sucursales/<int:sucursal_id>')
@admin_required
def sucursal_detail(sucursal_id):
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        SELECT s.id_sucursal, s.nombre, s.direccion, c.nombre AS comuna
        FROM public.sucursal s
        JOIN public.comuna c ON s.id_comuna = c.id_comuna
        WHERE s.id_sucursal = %s
    """, (sucursal_id,))
    sucursal = cur.fetchone()
    if sucursal is None:
        abort(404)

    # Grúas almacenadas
    cur.execute("""
        SELECT g.patente, g.estado
        FROM public.grua g
        JOIN public.vehiculo v ON g.patente = v.patente
        WHERE v.id_sucursal_esta = %s
        ORDER BY g.patente
    """, (sucursal_id,))
    gruas = cur.fetchall()

    # Vehículos retirables en custodia
    cur.execute("""
        SELECT vr.patente, vr.tipo, vr.estado, vr.observacion
        FROM public.vehiculoretirable vr
        JOIN public.vehiculo v ON vr.patente = v.patente
        WHERE v.id_sucursal_esta = %s
        ORDER BY vr.patente
    """, (sucursal_id,))
    vehicles = cur.fetchall()

    return render_template(
        'sucursales/detail.html',
        sucursal=sucursal,
        gruas=gruas,
        vehicles=vehicles
    )

