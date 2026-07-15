import psycopg

conn = psycopg.connect(service='django-bdd')
cur = conn.cursor()

cur.execute("""
    SELECT prosrc 
    FROM pg_proc 
    WHERE proname = 'bloquear_orden_vehiculo_en_traslado'
""")
row = cur.fetchone()
if row:
    print(row[0])
else:
    print("Trigger function not found.")

conn.close()
