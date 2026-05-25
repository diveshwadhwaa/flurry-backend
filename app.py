import os
import hmac
import hashlib
import json
import uuid
from datetime import datetime

from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)

CORS(app, origins=["*"])

RAZORPAY_KEY_ID     = os.environ.get("RAZORPAY_KEY_ID",     "rzp_test_placeholder")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "placeholder")
ADMIN_SECRET_KEY    = os.environ.get("ADMIN_SECRET_KEY",    "flurry-admin-secret-2025")
DATABASE_URL        = os.environ.get("DATABASE_URL",        "")

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
        conn.commit()
        cur.close()
        conn.close()
        print("[DB] Tables ready.")
    except Exception as e:
        print("[DB] Error during init:", e)

def is_admin(req):
    return req.headers.get("X-Admin-Key") == ADMIN_SECRET_KEY

@app.route("/", methods=["GET"])
def health():
    return jsonify({"status": "ok", "message": "Flurry Buddy backend is running!"})

@app.route("/create-order", methods=["POST"])
def create_order():
    try:
        data     = request.get_json()
        amount   = int(data.get("amount", 0))
        currency = data.get("currency", "INR")
        customer = data.get("customer", {})
        items    = data.get("items", [])

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
                         customer_pin, items, created_at)
                    VALUES (%s,%s,%s,%s,'pending',%s,%s,%s,%s,%s,%s,%s,%s,NOW())
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
                    json.dumps(items)
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
                         customer_pin, items, created_at, confirmed_at)
                    VALUES (%s,%s,%s,%s,'INR','paid',%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)
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
                    confirmed_at
                ))
                conn.commit()
                cur.close()
                conn.close()
            except Exception as e:
                print("[DB] verify_payment save error:", e)

        print("[ORDER CONFIRMED]", order_id, "|", customer.get("name"), "| Rs.", int(amount)//100)
        return jsonify({"verified": True, "order_id": order_id})

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/admin/orders", methods=["GET"])
def get_orders():
    if not is_admin(request):
        return jsonify({"error": "Unauthorized"}), 401
    if not DATABASE_URL:
        return jsonify({"orders": [], "total_orders": 0, "total_revenue": 0, "total_items": 0, "avg_order": 0})
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
            total_revenue += o.get("amount", 0)
            for it in (o.get("items") or []):
                total_items += it.get("qty", 0)
            orders.append(o)

        avg = (total_revenue // len(orders)) if orders else 0
        return jsonify({
            "orders":        orders,
            "total_orders":  len(orders),
            "total_revenue": total_revenue,
            "total_items":   total_items,
            "avg_order":     avg
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
