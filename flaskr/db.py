import psycopg

from psycopg.rows import namedtuple_row
from flask import g

def get_db():
    if 'db' not in g:
        g.db = psycopg.connect(
                service='django-bdd',
                row_factory = namedtuple_row,
                )
    return g.db

def close_db(e=None):
    db = g.pop('db', None)
    if db is not None:
        db.close()

def init_app(app):
    app.teardown_appcontext(close_db)

