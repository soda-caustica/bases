import psycopg

conn = psycopg.connect(service='django-bdd')
cur = conn.cursor()

# Restore Carlos Diaz availability
cur.execute("UPDATE public.chofer SET disponibilidad = true WHERE rut = '21456789-K'")
conn.commit()
print("Restored availability of Carlos Diaz.")

cur.execute("SELECT rut, disponibilidad FROM public.chofer")
for r in cur.fetchall():
    print(r)

conn.close()
