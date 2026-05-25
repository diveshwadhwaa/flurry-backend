"""
Flurry Buddy - Flask Backend with PostgreSQL
Permanent order storage that survives server restarts.
"""

import os
import hmac
import hashlib
import json
import uuid
from datetime import datetime

import razorpay
import psycopg2
import psycopg2.extras
from flask import Flask, request, jsonify
from flask_cors import CORS

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────
# CORS - update with your actual domains
# ─────────────────────────────────────────────────────────────
CORS(app, origins=[
    "http://localhost:5500",
    "http://127.0.0.1:5500",
    "https://yoursite.netlify.app",   # UPDATE THIS
    "https://www.yourdomain.com",     # UPDATE THIS
    "https://yourdomain.com",         # UPDATE THIS
])

# ─────────────────────────────────────────────────────────────
# CREDENTIALS (set as environment variables on Render)
# ─────────────────────────────────────────────────────────────
RAZORPAY_KEY_ID     = os.environ.get("RAZORPAY_KEY_ID",     "rzp_test_YourKeyIdHere")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "YourKeySecretHere")
ADMIN_SECRET_KEY    = os.environ.get("ADMIN_SECRET_KEY",    "flurry-admin-secret-2025")
DATABASE_URL        = os.environ.get("DATABASE_URL",        "")

client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))


# ─────────────────────────────────────────────────────────────
# DATABASE SETUP
# ─────────────────────────────────────────────────────────────
def get_db():
    """Get a database connection."""
    conn = psycopg2.connect(DATABASE_URL, sslmode="require")
    return conn


def init_db():
    """Create tables if they don't exist. Runs on server startup."""
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id               SERIAL PRIMARY KEY,
            order_id         VARCHAR(50)  UNIQUE NOT NULL,
            razorpay_order_id   VARCHAR(100),
            razorpay_payment_id VARCHAR(100),
            amount           INTEGER      NOT NULL,
            currency         VARCHAR(10)  DEFAULT 'INR',
            status           VARCHAR(20)  DEFAULT 'pending',

            customer_name    TEXT,
            customer_email   TEXT,
            customer_phone   TEXT,
            customer_address TEXT,
            customer_city    TEXT,
            customer_state   TEXT,
            customer_pin     VARCHAR(10),

            items            JSONB,

            created_at       TIMESTAMP    DEFAULT NOW(),
            confirmed_at     TIMESTAMP
        )
    """)
    conn.commit()
    cur.close()
    conn.close()
    print("[DB] Tables ready.")


# ─────────────────────────────────────────────────────────────
# ADMIN AUTH HELPER
# ─────────────────────────────────────────────────────────────
def is_admin(req):
    return req.headers.get("X-Admin-Key") == ADMIN_SECRET_KEY


# ─────────────────────────────────────────────────────────────
# HEALTH CHECK
# ─────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def health():
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM orders WHERE status = 'paid'")
        count = cur.fetchone()[0]
        cur.close()
        conn.close()
        return jsonify({
            "status":        "ok",
            "message":       "Flurry Buddy backend is running!",
            "orders_in_db":  count
        })
    except Exception as e:
        return jsonify({"status": "ok", "message": "Running (DB not connected yet)", "error": str(e)})


# ─────────────────────────────────────────────────────────────
# STEP 1 — CREATE RAZORPAY ORDER
# ─────────────────────────────────────────────────────────────
@app.route("/create-order", methods=["POST"])
def create_order():
    try:
        data = request.get_json()

        for field in ["amount", "currency", "customer", "items"]:
            if field not in data:
                return jsonify({"error": f"Missing field: {field}"}), 400

        amount   = int(data["amount"])
        currency = data.get("currency", "INR")

        if amount <= 0:
            return jsonify({"error": "Invalid amount"}), 400

        receipt   = "fb_" + str(uuid.uuid4())[:8]
        rzp_order = client.order.create({
            "amount":   amount,
            "currency": currency,
            "receipt":  receipt,
            "notes": {
                "customer_name":  data["customer"].get("name",  ""),
                "customer_email": data["customer"].get("email", ""),
            }
        })

        # Save pending order to DB
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO orders
                (order_id, razorpay_order_id, amount, currency, status,
                 customer_name, customer_email, customer_phone,
                 customer_address, customer_city, customer_state, customer_pin,
                 items, created_at)
            VALUES (%s,%s,%s,%s,'pending',%s,%s,%s,%s,%s,%s,%s,%s,NOW())
            ON CONFLICT (order_id) DO NOTHING
        """, (
            "PENDING-" + rzp_order["id"],
            rzp_order["id"],
            amount,
            currency,
            data["customer"].get("name",    ""),
            data["customer"].get("email",   ""),
            data["customer"].get("phone",   ""),
            data["customer"].get("address", ""),
            data["customer"].get("city",    ""),
            data["customer"].get("state",   ""),
            data["customer"].get("pin",     ""),
            json.dumps(data.get("items", []))
        ))
        conn.commit()
        cur.close()
        conn.close()

        return jsonify({
            "razorpay_order_id": rzp_order["id"],
            "amount":            amount,
            "currency":          currency,
            "key_id":            RAZORPAY_KEY_ID
        })

    except razorpay.errors.BadRequestError as e:
        return jsonify({"error": "Razorpay error: " + str(e)}), 400
    except Exception as e:
        return jsonify({"error": "Server error: " + str(e)}), 500


# ─────────────────────────────────────────────────────────────
# STEP 2 — VERIFY PAYMENT + CONFIRM ORDER
# ─────────────────────────────────────────────────────────────
@app.route("/verify-payment", methods=["POST"])
def verify_payment():
    try:
        data = request.get_json()

        rzp_order_id   = data.get("razorpay_order_id")
        rzp_payment_id = data.get("razorpay_payment_id")
        rzp_signature  = data.get("razorpay_signature")
        customer       = data.get("customer", {})
        items          = data.get("items",    [])
        amount         = data.get("amount",   0)

        if not all([rzp_order_id, rzp_payment_id, rzp_signature]):
            return jsonify({"error": "Missing payment details"}), 400

        # ── SIGNATURE VERIFICATION ───────────────────────────
        body         = rzp_order_id + "|" + rzp_payment_id
        expected_sig = hmac.new(
            RAZORPAY_KEY_SECRET.encode("utf-8"),
            body.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

        if expected_sig != rzp_signature:
            print(f"[FRAUD ATTEMPT] Order {rzp_order_id} - signature mismatch!")
            return jsonify({
                "verified": False,
                "error":    "Payment verification failed. Signature mismatch."
            }), 400

        # ── PAYMENT VERIFIED - Save confirmed order ──────────
        order_id     = "FB-" + str(uuid.uuid4())[:8].upper()
        confirmed_at = datetime.utcnow()

        conn = get_db()
        cur  = conn.cursor()

        cur.execute("""
            INSERT INTO orders
                (order_id, razorpay_order_id, razorpay_payment_id,
                 amount, currency, status,
                 customer_name, customer_email, customer_phone,
                 customer_address, customer_city, customer_state, customer_pin,
                 items, created_at, confirmed_at)
            VALUES (%s,%s,%s,%s,'INR','paid',%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)
            ON CONFLICT (order_id) DO UPDATE SET
                status              = 'paid',
                razorpay_payment_id = EXCLUDED.razorpay_payment_id,
                confirmed_at        = EXCLUDED.confirmed_at
        """, (
            order_id,
            rzp_order_id,
            rzp_payment_id,
            int(amount),
            customer.get("name",    ""),
            customer.get("email",   ""),
            customer.get("phone",   ""),
            customer.get("address", ""),
            customer.get("city",    ""),
            customer.get("state",   ""),
            customer.get("pin",     ""),
            json.dumps(items),
            confirmed_at
        ))
        conn.commit()
        cur.close()
        conn.close()

        print(f"[ORDER CONFIRMED] {order_id} | {customer.get('name')} | Rs.{int(amount)//100:,}")

        return jsonify({
            "verified": True,
            "order_id": order_id,
            "message":  "Payment verified and order confirmed!"
        })

    except Exception as e:
        print(f"[ERROR] verify_payment: {e}")
        return jsonify({"error": "Verification error: " + str(e)}), 500


# ─────────────────────────────────────────────────────────────
# ADMIN — GET ALL ORDERS
# ─────────────────────────────────────────────────────────────
@app.route("/admin/orders", methods=["GET"])
def get_orders():
    if not is_admin(request):
        return jsonify({"error": "Unauthorized"}), 401

    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("""
            SELECT
                order_id, razorpay_order_id, razorpay_payment_id,
                amount, currency, status,
                customer_name, customer_email, customer_phone,
                customer_address, customer_city, customer_state, customer_pin,
                items, created_at, confirmed_at
            FROM orders
            WHERE status = 'paid'
            ORDER BY confirmed_at DESC
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()

        orders = []
        total_revenue = 0
        total_items   = 0

        for row in rows:
            o = dict(row)
            # Convert datetime to string
            if o.get("created_at"):
                o["created_at"] = o["created_at"].isoformat()
            if o.get("confirmed_at"):
                o["confirmed_at"] = o["confirmed_at"].isoformat()
            # Parse items if string
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


# ─────────────────────────────────────────────────────────────
# ADMIN — GET SINGLE ORDER
# ─────────────────────────────────────────────────────────────
@app.route("/admin/orders/<order_id>", methods=["GET"])
def get_order(order_id):
    if not is_admin(request):
        return jsonify({"error": "Unauthorized"}), 401

    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
        row = cur.fetchone()
        cur.close()
        conn.close()

        if not row:
            return jsonify({"error": "Order not found"}), 404

        o = dict(row)
        if o.get("created_at"):  o["created_at"]  = o["created_at"].isoformat()
        if o.get("confirmed_at"): o["confirmed_at"] = o["confirmed_at"].isoformat()
        if isinstance(o.get("items"), str):
            try:    o["items"] = json.loads(o["items"])
            except: o["items"] = []

        return jsonify(o)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# ADMIN — SIMPLE DASHBOARD STATS
# ─────────────────────────────────────────────────────────────
@app.route("/admin/stats", methods=["GET"])
def get_stats():
    if not is_admin(request):
        return jsonify({"error": "Unauthorized"}), 401

    try:
        conn = get_db()
        cur  = conn.cursor()

        cur.execute("SELECT COUNT(*), COALESCE(SUM(amount),0) FROM orders WHERE status='paid'")
        count, revenue = cur.fetchone()

        cur.execute("SELECT confirmed_at FROM orders WHERE status='paid' ORDER BY confirmed_at DESC LIMIT 1")
        last = cur.fetchone()

        cur.close()
        conn.close()

        return jsonify({
            "total_orders":  count,
            "total_revenue": int(revenue),
            "avg_order":     int(revenue // count) if count else 0,
            "last_order_at": last[0].isoformat() if last and last[0] else None
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# RUN
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if DATABASE_URL:
        init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
