import psycopg

conn = psycopg.connect(service='django-bdd')
cur = conn.cursor()

try:
    # Let's get list of communes
    cur.execute("SELECT id_comuna FROM public.comuna LIMIT 2")
    comunas = [row[0] for row in cur.fetchall()]
    com_origen = comunas[0]
    com_destino = comunas[1] if len(comunas) > 1 else comunas[0]

    # Let's do the client insertion first if not exists
    client_rut = '99999999-9'
    cur.execute("SELECT 1 FROM public.cliente WHERE rut = %s", (client_rut,))
    if not cur.fetchone():
        cur.execute("INSERT INTO public.cliente (rut, email, fono, direccion_residencia, id_comuna) VALUES (%s, 'test@test.com', '123', 'address', %s)", (client_rut, com_origen))
    
    # Try the exact insertion from create_order
    fecha = '2026-07-20'
    monto = '0.00'
    estado = 'pendiente'
    rut_cliente = client_rut
    direccion_origen = 'Origen 123'
    direccion_destino = 'Destino 456'
    id_comuna_origen = com_origen
    id_comuna_destino = com_destino
    
    vehiculo_patente = 'TEST99'
    vehiculo_tipo = 'Sedan'
    vehiculo_observacion = 'Rasguños menores'
    
    # Step 1
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
    print("Step 1 succeeded. order_id =", new_order_id)
    
    # Step 2
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
    print("Step 2 succeeded.")
    
    # Step 3
    cur.execute(
        "INSERT INTO public.vehiculoretirable_orden (patente_vehiculo, numero_de_orden) VALUES (%s, %s)",
        (vehiculo_patente, new_order_id)
    )
    print("Step 3 succeeded.")
    
    # Step 4
    # Wait, we need the assign resources function logic
    # Let's import it or run it
    from flaskr.logistics import _assign_resources_to_order
    assignment = _assign_resources_to_order(cur, new_order_id, int(id_comuna_origen))
    print("Step 4 succeeded. Assignment:", assignment)
    
    conn.commit()
    print("Direct insert full flow succeeded!")
    
except Exception as e:
    import traceback
    traceback.print_exc()
finally:
    conn.rollback()
    conn.close()
