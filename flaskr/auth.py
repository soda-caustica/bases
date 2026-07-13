import functools

from flask import (
    Blueprint, flash, g, redirect, render_template, request, session, url_for
)

bp = Blueprint('auth', __name__, url_prefix='/auth')

@bp.route('/login', methods=('GET', 'POST'))
def login():
    if request.method == 'POST':
        rut = request.form['rut']
        contraseña = request.form['password']
        error = None

        # TODO: cambiar por validacion correcta
        if rut != '21983444-6':
            error = 'Rut incorrecto'
        elif contraseña != '123456':
            error = 'Contraseña incorrecta'
        if error is None:
            session.clear()
            session['rut'] = rut
            session['name'] = 'Máximo Beltrán'
            return redirect(url_for('index'))

        flash(error)
    return render_template('auth/login.html')

@bp.before_app_request
def load_logged_in_user():
    rut = session.get('rut')
    if rut is None:
        g.user = None
    else:
        g.user = {
            'rut': rut,
            'name': session.get('name', 'Usuario'),
        }

@bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

def login_required(view):
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if g.user is None:
            return redirect(url_for('auth.login'))
        return view(**kwargs)
    return wrapped_view
