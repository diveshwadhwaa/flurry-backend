import os
import hmac
import hashlib
import json
import uuid
from datetime import datetime
from html import escape

from flask import Flask, request, jsonify
from flask_cors import CORS
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)

CORS(app, origins=[
    "http://127.0.0.1:5500",
    "https://flurrybuddy.com",
    "https://www.flurrybuddy.com",
    "https://jazzy-manatee-54a1b8.netlify.app"
])

RAZORPAY_KEY_ID     = os.environ.get("RAZORPAY_KEY_ID",     "rzp_test_placeholder")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "placeholder")
ADMIN_SECRET_KEY    = os.environ.get("ADMIN_SECRET_KEY",    "flurry-admin-secret-2025")
DATABASE_URL        = os.environ.get("DATABASE_URL",        "")
BREVO_API_KEY       = os.environ.get("BREVO_API_KEY",       "")
FROM_EMAIL          = os.environ.get("FROM_EMAIL",          "hello@flurrybuddy.com")
FROM_NAME           = os.environ.get("FROM_NAME",           "Flurry Buddy")
SUPPORT_EMAIL       = os.environ.get("SUPPORT_EMAIL",       "support@flurrybuddy.com")
ADMIN_NOTIFY_EMAIL  = os.environ.get("ADMIN_NOTIFY_EMAIL",  SUPPORT_EMAIL)
AUTH_SECRET         = os.environ.get("AUTH_SECRET",         ADMIN_SECRET_KEY)
AUTH_MAX_AGE        = 60 * 60 * 24 * 30

# Lazy imports so server starts even if packages have issues
def get_razorpay_client():
    import razorpay
    return razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

def get_db():
    import psycopg2
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    if not DATABASE_URL:
        print("[DB] No DATABASE_URL set, skipping DB init")
        return
    try:
        import psycopg2
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id                  SERIAL PRIMARY KEY,
                order_id            VARCHAR(50) UNIQUE NOT NULL,
                razorpay_order_id   VARCHAR(100),
                razorpay_payment_id VARCHAR(100),
                amount              INTEGER,
                currency            VARCHAR(10) DEFAULT 'INR',
                status              VARCHAR(20) DEFAULT 'pending',
                customer_name       TEXT,
                customer_email      TEXT,
                customer_phone      TEXT,
                customer_address    TEXT,
                customer_city       TEXT,
                customer_state      TEXT,
                customer_pin        VARCHAR(10),
                items               TEXT,
                created_at          TIMESTAMP DEFAULT NOW(),
                confirmed_at        TIMESTAMP
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            SERIAL PRIMARY KEY,
                name          TEXT NOT NULL,
                email         TEXT UNIQUE NOT NULL,
                phone         TEXT,
                password_hash TEXT NOT NULL,
                created_at    TIMESTAMP DEFAULT NOW()
            )
        """)
        cur.execute("ALTER TABLE orders ADD COLUMN IF NOT EXISTS user_id INTEGER")
        conn.commit()
        cur.close()
        conn.close()
        print("[DB] Tables ready.")
    except Exception as e:
        print("[DB] Error during init:", e)

def is_admin(req):
    return req.headers.get("X-Admin-Key") == ADMIN_SECRET_KEY

def auth_serializer():
    return URLSafeTimedSerializer(AUTH_SECRET, salt="flurry-buddy-auth")

def make_auth_token(user_id):
    return auth_serializer().dumps({"user_id": user_id})

def get_auth_user(required=False):
    auth = request.headers.get("Authorization", "")
    token = auth.replace("Bearer ", "", 1).strip() if auth.startswith("Bearer ") else ""
    if not token:
        if required:
            return None, (jsonify({"error": "Login required"}), 401)
        return None, None
    try:
        data = auth_serializer().loads(token, max_age=AUTH_MAX_AGE)
        user_id = int(data.get("user_id"))
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        if required:
            return None, (jsonify({"error": "Session expired. Please log in again."}), 401)
        return None, None

    if not DATABASE_URL:
        if required:
            return None, (jsonify({"error": "Accounts are not available yet"}), 503)
        return None, None
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, name, email, phone FROM users WHERE id=%s", (user_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row:
            if required:
                return None, (jsonify({"error": "User not found"}), 404)
            return None, None
        return {"id": row[0], "name": row[1], "email": row[2], "phone": row[3] or ""}, None
    except Exception as e:
        print("[AUTH] user lookup error:", e)
        if required:
            return None, (jsonify({"error": "Could not load account"}), 500)
        return None, None

def clean_email(email):
    return (email or "").strip().lower()

def rupees_from_paise(amount):
    try:
        return "Rs.{:,.0f}".format(int(amount) / 100)
    except Exception:
        return "Rs.0"

def customer_full_address(customer):
    customer = customer or {}
    line = (customer.get("address") or "").strip()
    city = (customer.get("city") or "").strip()
    state = (customer.get("state") or "").strip()
    pin = (customer.get("pin") or "").strip()
    parts = [p for p in [line, city, state] if p]
    address = ", ".join(parts)
    if pin:
        address = (address + " - " if address else "") + pin
    return address

def order_items_html(items):
    rows = []
    for item in items or []:
        name = escape(str(item.get("name", "Flurry Buddy")))
        color = escape(str(item.get("color", "")))
        size = escape(str(item.get("size", "")))
        qty = item.get("qty", 1)
        price = item.get("price", 0)
        item_name = name + (f"<br><span style='font-size:12px;color:#8b7ab8;'>Color: {color}</span>" if color else "")
        item_name += (f"<br><span style='font-size:12px;color:#8b7ab8;'>Size: {size}</span>" if size else "")
        rows.append(
            "<tr>"
            f"<td style='padding:8px 0;border-bottom:1px solid #eee;'>{item_name}</td>"
            f"<td style='padding:8px 0;border-bottom:1px solid #eee;text-align:center;'>x{qty}</td>"
            f"<td style='padding:8px 0;border-bottom:1px solid #eee;text-align:right;'>Rs.{int(price) * int(qty):,}</td>"
            "</tr>"
        )
    return "".join(rows)

def customer_details_html(customer):
    customer = customer or {}
    rows = [
        ("Name", customer.get("name", "")),
        ("Mobile", customer.get("phone", "")),
        ("Email", customer.get("email", "")),
        ("Address", customer_full_address(customer)),
    ]
    return "".join(
        "<tr>"
        f"<td style='padding:8px 0;border-bottom:1px solid #eee;font-weight:700;'>{escape(label)}</td>"
        f"<td style='padding:8px 0;border-bottom:1px solid #eee;text-align:right;'>{escape(str(value or '-'))}</td>"
        "</tr>"
        for label, value in rows
    )

def brevo_headers():
    return {
        "accept": "application/json",
        "api-key": BREVO_API_KEY,
        "content-type": "application/json"
    }

def send_order_email(order_id, customer, items, amount, payment_id=""):
    if not BREVO_API_KEY:
        print("[EMAIL] BREVO_API_KEY not set, skipping customer email")
        return False

    customer_email = (customer or {}).get("email", "").strip()
    customer_name  = (customer or {}).get("name", "there").strip() or "there"
    if not customer_email:
        print("[EMAIL] Customer email missing, skipping")
        return False

    html = f"""
    <div style="font-family:Arial,sans-serif;background:#f4edff;padding:28px;color:#4a3460;">
      <div style="max-width:620px;margin:auto;background:#ffffff;border-radius:22px;padding:28px;box-shadow:0 8px 32px rgba(124,92,191,0.14);">
        <h1 style="color:#7c5cbf;margin:0 0 8px;">Your buddy is officially adopted 💜</h1>
        <p style="font-size:16px;line-height:1.6;">Hi {customer_name}, thank you for ordering from Flurry Buddy. Your cozy little friend is getting packed with love and emotional support.</p>
        <p style="font-weight:700;">Order ID: {order_id}</p>
        <table style="width:100%;border-collapse:collapse;margin:18px 0;">
          <thead>
            <tr>
              <th style="text-align:left;padding-bottom:8px;">Buddy</th>
              <th style="text-align:center;padding-bottom:8px;">Qty</th>
              <th style="text-align:right;padding-bottom:8px;">Amount</th>
            </tr>
          </thead>
          <tbody>{order_items_html(items)}</tbody>
        </table>
        <p style="font-size:18px;font-weight:800;color:#7c5cbf;">Total paid: {rupees_from_paise(amount)}</p>
        <p style="line-height:1.6;">We will share tracking details once your buddy ships ✨</p>
        <p style="font-size:13px;color:#8b7ab8;">Questions? Reply to this email or write to {SUPPORT_EMAIL}.</p>
      </div>
    </div>
    """

    customer_payload = {
        "sender": {"name": FROM_NAME, "email": FROM_EMAIL},
        "to": [{"email": customer_email, "name": customer_name}],
        "replyTo": {"email": SUPPORT_EMAIL, "name": FROM_NAME},
        "subject": f"Your Flurry Buddy order is confirmed - {order_id}",
        "htmlContent": html
    }

    try:
        import requests
        resp = requests.post(
            "https://api.brevo.com/v3/smtp/email",
            headers=brevo_headers(),
            json=customer_payload,
            timeout=12
        )
        if resp.status_code >= 300:
            print("[EMAIL] Brevo send failed:", resp.status_code, resp.text)
            return False

        print("[EMAIL] Confirmation sent to", customer_email)
        if ADMIN_NOTIFY_EMAIL and ADMIN_NOTIFY_EMAIL.lower() != customer_email.lower():
            admin_html = f"""
            <div style="font-family:Arial,sans-serif;background:#f4edff;padding:28px;color:#4a3460;">
              <div style="max-width:680px;margin:auto;background:#ffffff;border-radius:18px;padding:26px;">
                <h2 style="color:#7c5cbf;margin:0 0 10px;">New Flurry Buddy order</h2>
                <p style="margin:0 0 14px;"><strong>Order ID:</strong> {escape(order_id)}</p>
                <p style="margin:0 0 18px;"><strong>Payment ID:</strong> {escape(payment_id or "-")}</p>
                <h3 style="color:#7c5cbf;margin:18px 0 8px;">Customer details</h3>
                <table style="width:100%;border-collapse:collapse;">{customer_details_html(customer)}</table>
                <h3 style="color:#7c5cbf;margin:22px 0 8px;">Product details</h3>
                <table style="width:100%;border-collapse:collapse;">
                  <thead>
                    <tr>
                      <th style="text-align:left;padding-bottom:8px;">Product</th>
                      <th style="text-align:center;padding-bottom:8px;">Qty</th>
                      <th style="text-align:right;padding-bottom:8px;">Amount</th>
                    </tr>
                  </thead>
                  <tbody>{order_items_html(items)}</tbody>
                </table>
                <p style="font-size:18px;font-weight:800;color:#7c5cbf;">Total paid: {rupees_from_paise(amount)}</p>
              </div>
            </div>
            """
            admin_payload = {
                "sender": {"name": FROM_NAME, "email": FROM_EMAIL},
                "to": [{"email": ADMIN_NOTIFY_EMAIL, "name": "Flurry Buddy"}],
                "replyTo": {"email": customer_email, "name": customer_name},
                "subject": f"New Flurry Buddy order - {order_id}",
                "htmlContent": admin_html
            }
            admin_resp = requests.post(
                "https://api.brevo.com/v3/smtp/email",
                headers=brevo_headers(),
                json=admin_payload,
                timeout=12
            )
            if admin_resp.status_code >= 300:
                print("[EMAIL] Admin notification failed:", admin_resp.status_code, admin_resp.text)
            else:
                print("[EMAIL] Admin notification sent to", ADMIN_NOTIFY_EMAIL)
        return True
    except Exception as e:
        print("[EMAIL] Error:", e)
        return False

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "Flurry Buddy backend is running!"})

@app.route("/auth/signup", methods=["POST"])
def auth_signup():
    if not DATABASE_URL:
        return jsonify({"error": "Accounts are not available yet"}), 503
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    email = clean_email(data.get("email"))
    phone = (data.get("phone") or "").strip()
    password = data.get("password") or ""

    if len(name) < 2:
        return jsonify({"error": "Please enter your name"}), 400
    if "@" not in email or "." not in email:
        return jsonify({"error": "Please enter a valid email"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email=%s", (email,))
        if cur.fetchone():
            cur.close()
            conn.close()
            return jsonify({"error": "An account already exists with this email"}), 409
        cur.execute("""
            INSERT INTO users (name, email, phone, password_hash, created_at)
            VALUES (%s,%s,%s,%s,NOW())
            RETURNING id, name, email, phone
        """, (name, email, phone, generate_password_hash(password)))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        conn.close()
        user = {"id": row[0], "name": row[1], "email": row[2], "phone": row[3] or ""}
        return jsonify({"token": make_auth_token(user["id"]), "user": user})
    except Exception as e:
        print("[AUTH] signup error:", e)
        return jsonify({"error": "Could not create account"}), 500

@app.route("/auth/login", methods=["POST"])
def auth_login():
    if not DATABASE_URL:
        return jsonify({"error": "Accounts are not available yet"}), 503
    data = request.get_json() or {}
    email = clean_email(data.get("email"))
    password = data.get("password") or ""
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT id, name, email, phone, password_hash FROM users WHERE email=%s", (email,))
        row = cur.fetchone()
        cur.close()
        conn.close()
        if not row or not check_password_hash(row[4], password):
            return jsonify({"error": "Incorrect email or password"}), 401
        user = {"id": row[0], "name": row[1], "email": row[2], "phone": row[3] or ""}
        return jsonify({"token": make_auth_token(user["id"]), "user": user})
    except Exception as e:
        print("[AUTH] login error:", e)
        return jsonify({"error": "Could not log in"}), 500

@app.route("/auth/me", methods=["GET"])
def auth_me():
    user, err = get_auth_user(required=True)
    if err:
        return err
    return jsonify({"user": user})

@app.route("/auth/orders", methods=["GET"])
def auth_orders():
    user, err = get_auth_user(required=True)
    if err:
        return err
    if not DATABASE_URL:
        return jsonify({"orders": []})
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("""
            SELECT order_id, razorpay_payment_id, amount, status, items, confirmed_at
            FROM orders
            WHERE status='paid' AND (user_id=%s OR LOWER(customer_email)=LOWER(%s))
            ORDER BY confirmed_at DESC
        """, (user["id"], user["email"]))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        orders = []
        for row in rows:
            try:
                items = json.loads(row[4]) if isinstance(row[4], str) else (row[4] or [])
            except Exception:
                items = []
            orders.append({
                "order_id": row[0],
                "payment_id": row[1],
                "amount": row[2],
                "status": row[3],
                "items": items,
                "confirmed_at": row[5].isoformat() if row[5] else ""
            })
        return jsonify({"orders": orders})
    except Exception as e:
        print("[AUTH] orders error:", e)
        return jsonify({"error": "Could not load orders"}), 500

@app.route("/create-order", methods=["POST"])
def create_order():
    try:
        data     = request.get_json()
        amount   = int(data.get("amount", 0))
        currency = data.get("currency", "INR")
        customer = data.get("customer", {})
        items    = data.get("items", [])
        auth_user, _ = get_auth_user(required=False)
        user_id = auth_user.get("id") if auth_user else None

        if amount <= 0:
            return jsonify({"error": "Invalid amount"}), 400

        client    = get_razorpay_client()
        receipt   = "fb_" + str(uuid.uuid4())[:8]
        rzp_order = client.order.create({
            "amount":   amount,
            "currency": currency,
            "receipt":  receipt,
        })

        if DATABASE_URL:
            try:
                conn = get_db()
                cur  = conn.cursor()
                cur.execute("""
                    INSERT INTO orders
                        (order_id, razorpay_order_id, amount, currency, status,
                         customer_name, customer_email, customer_phone,
                         customer_address, customer_city, customer_state,
                         customer_pin, items, user_id, created_at)
                    VALUES (%s,%s,%s,%s,'pending',%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                    ON CONFLICT (order_id) DO NOTHING
                """, (
                    "PENDING-" + rzp_order["id"],
                    rzp_order["id"],
                    amount, currency,
                    customer.get("name",""),
                    customer.get("email",""),
                    customer.get("phone",""),
                    customer.get("address",""),
                    customer.get("city",""),
                    customer.get("state",""),
                    customer.get("pin",""),
                    json.dumps(items),
                    user_id
                ))
                conn.commit()
                cur.close()
                conn.close()
            except Exception as e:
                print("[DB] create_order save error:", e)

        return jsonify({
            "razorpay_order_id": rzp_order["id"],
            "amount":            amount,
            "currency":          currency,
            "key_id":            RAZORPAY_KEY_ID
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/verify-payment", methods=["POST"])
def verify_payment():
    try:
        data           = request.get_json()
        rzp_order_id   = data.get("razorpay_order_id","")
        rzp_payment_id = data.get("razorpay_payment_id","")
        rzp_signature  = data.get("razorpay_signature","")
        customer       = data.get("customer", {})
        items          = data.get("items", [])
        amount         = data.get("amount", 0)
        auth_user, _ = get_auth_user(required=False)
        user_id = auth_user.get("id") if auth_user else None

        if not all([rzp_order_id, rzp_payment_id, rzp_signature]):
            return jsonify({"error": "Missing payment details"}), 400

        body         = rzp_order_id + "|" + rzp_payment_id
        expected_sig = hmac.new(
            RAZORPAY_KEY_SECRET.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        if expected_sig != rzp_signature:
            print("[FRAUD] Signature mismatch for order:", rzp_order_id)
            return jsonify({"verified": False, "error": "Signature mismatch"}), 400

        order_id     = "FB-" + str(uuid.uuid4())[:8].upper()
        confirmed_at = datetime.utcnow()

        if DATABASE_URL:
            try:
                conn = get_db()
                cur  = conn.cursor()
                cur.execute("""
                    INSERT INTO orders
                        (order_id, razorpay_order_id, razorpay_payment_id,
                         amount, currency, status,
                         customer_name, customer_email, customer_phone,
                         customer_address, customer_city, customer_state,
                         customer_pin, items, user_id, created_at, confirmed_at)
                    VALUES (%s,%s,%s,%s,'INR','paid',%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)
                    ON CONFLICT (order_id) DO NOTHING
                """, (
                    order_id,
                    rzp_order_id,
                    rzp_payment_id,
                    int(amount),
                    customer.get("name",""),
                    customer.get("email",""),
                    customer.get("phone",""),
                    customer.get("address",""),
                    customer.get("city",""),
                    customer.get("state",""),
                    customer.get("pin",""),
                    json.dumps(items),
                    user_id,
                    confirmed_at
                ))
                conn.commit()
                cur.close()
                conn.close()
            except Exception as e:
                print("[DB] verify_payment save error:", e)

        print("[ORDER CONFIRMED]", order_id, "|", customer.get("name"), "| Rs.", int(amount)//100)
        email_sent = send_order_email(order_id, customer, items, amount, rzp_payment_id)
        return jsonify({"verified": True, "order_id": order_id, "email_sent": email_sent})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/admin/orders", methods=["GET"])
def get_orders():
    if not is_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    if not DATABASE_URL:
        return jsonify({"orders": [], "total_orders": 0, "total_revenue": 0, "total_items": 0, "avg_order": 0, "database_connected": False})
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            SELECT order_id, razorpay_order_id, razorpay_payment_id,
                   amount, status, customer_name, customer_email,
                   customer_phone, customer_address, customer_city,
                   customer_state, customer_pin, items, confirmed_at
            FROM orders WHERE status='paid'
            ORDER BY confirmed_at DESC
        """)
        rows    = cur.fetchall()
        cols    = [d[0] for d in cur.description]
        cur.close()
        conn.close()

        orders        = []
        total_revenue = 0
        total_items   = 0

        for row in rows:
            o = dict(zip(cols, row))
            if o.get("confirmed_at"):
                o["confirmed_at"] = o["confirmed_at"].isoformat()
            if isinstance(o.get("items"), str):
                try:    o["items"] = json.loads(o["items"])
                except: o["items"] = []
            amount_paise = int(o.get("amount") or 0)
            total_revenue += amount_paise
            for it in (o.get("items") or []):
                total_items += it.get("qty", 0)
            address_parts = [
                o.get("customer_address") or "",
                o.get("customer_city") or "",
                o.get("customer_state") or ""
            ]
            full_address = ", ".join([p for p in address_parts if p])
            if o.get("customer_pin"):
                full_address = (full_address + " - " if full_address else "") + str(o.get("customer_pin"))
            o.update({
                "orderId": o.get("order_id") or "",
                "paymentId": o.get("razorpay_payment_id") or "",
                "date": o.get("confirmed_at") or "",
                "name": o.get("customer_name") or "",
                "email": o.get("customer_email") or "",
                "phone": o.get("customer_phone") or "",
                "address": full_address,
                "total": round(amount_paise / 100),
            })
            orders.append(o)

        avg = (total_revenue // len(orders)) if orders else 0
        return jsonify({
            "orders":        orders,
            "total_orders":  len(orders),
            "total_revenue": total_revenue,
            "total_items":   total_items,
            "avg_order":     avg,
            "database_connected": True
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/admin/orders/<order_id>", methods=["GET"])
def get_order(order_id):
    if not is_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT * FROM orders WHERE order_id=%s", (order_id,))
        row  = cur.fetchone()
        cols = [d[0] for d in cur.description]
        cur.close()
        conn.close()
        if not row:
            return jsonify({"error": "Order not found"}), 404
        o = dict(zip(cols, row))
        if o.get("confirmed_at"): o["confirmed_at"] = o["confirmed_at"].isoformat()
        if o.get("created_at"):   o["created_at"]   = o["created_at"].isoformat()
        if isinstance(o.get("items"), str):
            try:    o["items"] = json.loads(o["items"])
            except: o["items"] = []
        return jsonify(o)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port, debug=False)
