import functools

from flask import (
    Blueprint, flash, g, redirect, render_template, request, session, url_for
)
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db

bp = Blueprint('auth', __name__, url_prefix='/auth')

# Credenciales de administrador.
# TODO: mover a una tabla de usuarios administradores propia.
ADMIN_RUT = '21983444-6'
ADMIN_PASSWORD_HASH = generate_password_hash('123456')
ADMIN_NAME = 'Máximo Beltrán'


@bp.route('/login', methods=('GET', 'POST'))
def login():
    if request.method == 'POST':
        rut = request.form['rut'].strip()
        password = request.form['password']
        error = None

        if rut == ADMIN_RUT:
            if not check_password_hash(ADMIN_PASSWORD_HASH, password):
                error = 'Contraseña incorrecta.'
            if error is None:
                session.clear()
                session['rut'] = rut
                session['name'] = ADMIN_NAME
                session['role'] = 'admin'
                return redirect(url_for('logistics.dashboard'))
        else:
            db = get_db()
            cur = db.cursor()
            cur.execute(
                """
                SELECT c.rut, cc.password,
                       COALESCE(p.nombre, e.razon_social, c.email, c.rut) AS nombre
                FROM public.cliente c
                LEFT JOIN public.persona p ON p.rut = c.rut
                LEFT JOIN public.empresa e ON e.rut = c.rut
                LEFT JOIN public.credencial_cliente cc ON cc.rut_cliente = c.rut
                WHERE c.rut = %s
                """,
                (rut,),
            )
            client = cur.fetchone()
            if client is None:
                error = 'RUT no encontrado. ¿Necesitas crear una cuenta?'
            elif client.password is None or not check_password_hash(client.password, password):
                error = 'Contraseña incorrecta.'

            if error is None:
                session.clear()
                session['rut'] = client.rut
                session['name'] = client.nombre
                session['role'] = 'client'
                return redirect(url_for('logistics.dashboard'))

        flash(error)
    return render_template('auth/login.html')


@bp.route('/register', methods=('GET', 'POST'))
def register():
    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id_comuna, nombre FROM public.comuna ORDER BY nombre")
    communes = cur.fetchall()

    error = None
    if request.method == 'POST':
        tipo = request.form.get('tipo_cliente', 'persona')
        rut = request.form.get('rut', '').strip()
        nombre = request.form.get('nombre', '').strip()
        email = request.form.get('email', '').strip()
        fono = request.form.get('fono', '').strip()
        direccion = request.form.get('direccion_residencia', '').strip()
        id_comuna = request.form.get('id_comuna', '').strip()
        password = request.form.get('password', '')
        password_confirm = request.form.get('password_confirm', '')

        if not rut:
            error = 'El RUT es obligatorio.'
        elif not nombre:
            error = 'El nombre / razón social es obligatorio.'
        elif not email:
            error = 'El correo es obligatorio.'
        elif not id_comuna.isdigit():
            error = 'Debes seleccionar una comuna.'
        elif not password or len(password) < 6:
            error = 'La contraseña debe tener al menos 6 caracteres.'
        elif password != password_confirm:
            error = 'Las contraseñas no coinciden.'

        if error is None:
            try:
                cur.execute("SELECT 1 FROM public.cliente WHERE rut = %s", (rut,))
                if cur.fetchone() is not None:
                    error = 'Ya existe un cliente registrado con ese RUT.'
            except Exception:
                db.rollback()
                error = 'No se pudo validar el RUT.'

        if error is None:
            try:
                cur.execute(
                    """
                    INSERT INTO public.cliente (rut, email, fono, direccion_residencia, id_comuna)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (rut, email, fono, direccion, int(id_comuna)),
                )
                if tipo == 'empresa':
                    cur.execute(
                        "INSERT INTO public.empresa (rut, razon_social) VALUES (%s, %s)",
                        (rut, nombre),
                    )
                else:
                    cur.execute(
                        "INSERT INTO public.persona (rut, nombre) VALUES (%s, %s)",
                        (rut, nombre),
                    )
                cur.execute(
                    "INSERT INTO public.credencial_cliente (rut_cliente, password) VALUES (%s, %s)",
                    (rut, generate_password_hash(password)),
                )
                db.commit()

                session.clear()
                session['rut'] = rut
                session['name'] = nombre
                session['role'] = 'client'
                flash('Cuenta creada correctamente. ¡Bienvenido/a!')
                return redirect(url_for('logistics.dashboard'))
            except Exception:
                db.rollback()
                error = 'No se pudo crear la cuenta. Verifica los datos ingresados.'

        flash(error)

    return render_template('auth/register.html', communes=communes)


@bp.before_app_request
def load_logged_in_user():
    rut = session.get('rut')
    if rut is None:
        g.user = None
    else:
        g.user = {
            'rut': rut,
            'name': session.get('name', 'Usuario'),
            'role': session.get('role', 'client'),
        }


@bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))


def login_required(view):
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for('auth.login'))
        return view(**kwargs)
    return wrapped_view


def admin_required(view):
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for('auth.login'))
        if g.user.get('role') != 'admin':
            flash('No tienes permiso para acceder a esta sección.')
            return redirect(url_for('logistics.dashboard'))
        return view(**kwargs)
    return wrapped_view
