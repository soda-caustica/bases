import requests
from bs4 import BeautifulSoup
import psycopg

BASE_URL = 'http://127.0.0.1:5000'

def test_flow():
    # 1. Register a new client
    client_rut = '99999999-9'
    client_password = 'password123'
    
    session = requests.Session()
    
    # We need to clean up DB first if client or order from previous run exists
    conn = psycopg.connect(service='django-bdd')
    cur = conn.cursor()
    
    # Self-healing: make sure Carlos Diaz is available
    cur.execute("UPDATE public.chofer SET disponibilidad = true WHERE rut = '21456789-K'")
    
    # Clean up TEST99 vehicle and orders associated with it
    cur.execute("DELETE FROM public.grua_retiro WHERE id_retiro IN (SELECT id_retiro FROM public.retiro WHERE numero_de_orden IN (SELECT numero_de_orden FROM public.vehiculoretirable_orden WHERE patente_vehiculo = 'TEST99'))")
    cur.execute("DELETE FROM public.retiro WHERE numero_de_orden IN (SELECT numero_de_orden FROM public.vehiculoretirable_orden WHERE patente_vehiculo = 'TEST99')")
    cur.execute("DELETE FROM public.vehiculoretirable_orden WHERE patente_vehiculo = 'TEST99'")
    cur.execute("DELETE FROM public.vehiculoretirable WHERE patente = 'TEST99'")
    cur.execute("DELETE FROM public.vehiculo WHERE patente = 'TEST99'")
    
    # Clean up client and their data
    cur.execute("DELETE FROM public.grua_retiro WHERE id_retiro IN (SELECT id_retiro FROM public.retiro WHERE numero_de_orden IN (SELECT numero_de_orden FROM public.ordenderetiro WHERE rut_cliente = %s))", (client_rut,))
    cur.execute("DELETE FROM public.retiro WHERE numero_de_orden IN (SELECT numero_de_orden FROM public.ordenderetiro WHERE rut_cliente = %s)", (client_rut,))
    cur.execute("DELETE FROM public.credencial_cliente WHERE rut_cliente = %s", (client_rut,))
    cur.execute("DELETE FROM public.persona WHERE rut = %s", (client_rut,))
    cur.execute("DELETE FROM public.empresa WHERE rut = %s", (client_rut,))
    cur.execute("DELETE FROM public.vehiculoretirable_orden WHERE numero_de_orden IN (SELECT numero_de_orden FROM public.ordenderetiro WHERE rut_cliente = %s)", (client_rut,))
    cur.execute("DELETE FROM public.ordenderetiro WHERE rut_cliente = %s", (client_rut,))
    cur.execute("DELETE FROM public.cliente WHERE rut = %s", (client_rut,))
    
    conn.commit()
    print("Database cleaned up for test client and vehicle TEST99. Carlos Diaz availability restored.")

    try:
        # Register
        print("Registering client...")
        reg_data = {
            'tipo_cliente': 'persona',
            'rut': client_rut,
            'nombre': 'Cliente De Prueba',
            'email': 'prueba@test.com',
            'fono': '123456789',
            'direccion_residencia': 'Av Siempre Viva 742',
            'id_comuna': '1', # Santiago or similar
            'password': client_password,
            'password_confirm': client_password
        }
        
        r = session.post(f"{BASE_URL}/auth/register", data=reg_data, allow_redirects=True)
        assert r.status_code == 200, f"Register failed with status {r.status_code}"
        print("Client registered and logged in successfully.")
        
        # 2. Get order creation page and check that 'monto_base' input does NOT exist
        print("Accessing order creation page as client...")
        r = session.get(f"{BASE_URL}/orders/create")
        soup = BeautifulSoup(r.text, 'html.parser')
        
        monto_input = soup.find('input', {'name': 'monto_base'})
        assert monto_input is None, "ERROR: Client interface exposes 'monto_base' input!"
        print("SUCCESS: Client interface does not expose cost assignment input.")
        
        # 3. Create an order as client
        # Let's get list of communes to use valid source/destination
        cur.execute("SELECT id_comuna FROM public.comuna LIMIT 2")
        comunas = [row[0] for row in cur.fetchall()]
        assert len(comunas) >= 1, "No communes in DB"
        com_origen = comunas[0]
        com_destino = comunas[1] if len(comunas) > 1 else comunas[0]
        
        order_data = {
            'fecha': '2026-07-20',
            'direccion_origen': 'Origen 123',
            'id_comuna_origen': str(com_origen),
            'direccion_destino': 'Destino 456',
            'id_comuna_destino': str(com_destino),
            'vehiculo_patente': 'TEST99',
            'vehiculo_tipo': 'Sedan',
            'vehiculo_observacion': 'Rasguños menores'
        }
        
        print("Creating order as client...")
        r = session.post(f"{BASE_URL}/orders/create", data=order_data, allow_redirects=True)
        
        # Print flashed error messages if any
        soup = BeautifulSoup(r.text, 'html.parser')
        flashes = soup.find_all(class_='flash') or soup.find_all(class_='alert') or soup.find_all(class_='error')
        if flashes:
            print("Flashes found on page:")
            for f in flashes:
                print("-", f.text.strip())
                
        # Check DB
        cur.execute("SELECT numero_de_orden, monto_base FROM public.ordenderetiro WHERE rut_cliente = %s ORDER BY numero_de_orden DESC LIMIT 1", (client_rut,))
        row = cur.fetchone()
        if row is None:
            print("Page HTML response:")
            print(r.text[:2000])
            raise AssertionError("Order not found in DB!")
            
        order_id = row[0]
        monto_base = row[1]
        assert float(monto_base) == 0.0, f"ERROR: Expected base cost 0.0, got {monto_base}"
        print(f"SUCCESS: Order #{order_id} created with initial base cost: {monto_base}")
        
        # 5. Access detail page as client and check UI
        print("Accessing order detail as client...")
        r = session.get(f"{BASE_URL}/orders/{order_id}")
        soup = BeautifulSoup(r.text, 'html.parser')
        
        # Check if update cost form exists for client
        cost_form = soup.find('form', action=lambda x: x and '/cost' in x)
        assert cost_form is None, "ERROR: Client has access to update cost form!"
        print("SUCCESS: Client does not have access to cost assignment form in detail page.")
        
        # Check if "Ir a pagar" link is hidden/shown correctly
        pay_link = soup.find('a', href=lambda x: x and f'/payments/{order_id}/pay' in x)
        assert pay_link is None, "ERROR: Client can pay order with unassigned cost!"
        assert "El costo de esta orden aún no ha sido asignado por el administrador" in r.text, "ERROR: Missing pending cost message for client"
        print("SUCCESS: Client payment options are blocked/hidden due to unassigned cost.")
        
        # 6. Log in as admin and check order detail
        admin_session = requests.Session()
        print("Logging in as Admin...")
        admin_login_data = {
            'rut': '21983444-6',
            'password': '123456'
        }
        r = admin_session.post(f"{BASE_URL}/auth/login", data=admin_login_data, allow_redirects=True)
        assert r.status_code == 200, "Admin login failed"
        print("Admin logged in successfully.")
        
        print("Accessing order detail as Admin...")
        r = admin_session.get(f"{BASE_URL}/orders/{order_id}")
        soup = BeautifulSoup(r.text, 'html.parser')
        
        # Admin should see update cost form
        cost_form = soup.find('form', action=f"/orders/{order_id}/cost")
        assert cost_form is not None, "ERROR: Admin cannot see the cost assignment form!"
        print("SUCCESS: Admin can see the cost assignment form in detail page.")
        
        # 7. Assign cost as Admin
        assigned_cost = '55000.00'
        print(f"Assigning cost of {assigned_cost} to order #{order_id} as Admin...")
        r = admin_session.post(f"{BASE_URL}/orders/{order_id}/cost", data={'monto_base': assigned_cost}, allow_redirects=True)
        assert r.status_code == 200, "Admin cost assignment POST failed"
        print("Cost assigned successfully by Admin.")
        
        # 8. Check DB for updated cost
        cur.execute("SELECT monto_base FROM public.ordenderetiro WHERE numero_de_orden = %s", (order_id,))
        updated_monto = cur.fetchone()[0]
        assert float(updated_monto) == float(assigned_cost), f"ERROR: Expected updated cost {assigned_cost}, got {updated_monto}"
        print(f"SUCCESS: Order cost updated in database to: {updated_monto}")
        
        # 9. Verify that payment options are now active
        print("Verifying payment options are now active for the client...")
        r = session.get(f"{BASE_URL}/orders/{order_id}")
        soup = BeautifulSoup(r.text, 'html.parser')
        pay_link = soup.find('a', href=f"/payments/{order_id}/pay")
        assert pay_link is not None, "ERROR: Client payment option is still blocked after cost assignment!"
        print("SUCCESS: Client can now pay the order.")
        
        print("\n--- ALL TESTS PASSED SUCCESSFULLY! ---")
        
    finally:
        cur.close()
        conn.close()
        print("Database connection closed.")

if __name__ == '__main__':
    test_flow()
