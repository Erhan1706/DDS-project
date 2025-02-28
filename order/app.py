import logging
import os
import atexit
import random
import uuid
from collections import defaultdict

import redis
import requests

from msgspec import msgpack, Struct
from flask import Flask, jsonify, abort, Response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import text

DB_ERROR_STR = "DB error"
REQ_ERROR_STR = "Requests error"

GATEWAY_URL = os.environ['GATEWAY_URL']

#db: redis.Redis = redis.Redis(host=os.environ['REDIS_HOST'],
#                              port=int(os.environ['REDIS_PORT']),
#                              password=os.environ['REDIS_PASSWORD'],
#                              db=int(os.environ['REDIS_DB']))



class Base(DeclarativeBase):
  pass

app = Flask("order-service")

app.config["SQLALCHEMY_DATABASE_URI"]  = f'postgresql://postgres:postgres@order-postgres:5432/order-db'

app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "isolation_level": "SERIALIZABLE" # Strongest isolation level for postgres
}

db: SQLAlchemy = SQLAlchemy(app)

def close_db_connection():
    with app.app_context():
        db.session.close()

class Order(db.Model):
    __tablename__ = "orders"
    
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = db.Column(db.String(50), nullable=False)
    paid = db.Column(db.Boolean, default=False, nullable=False)
    items = db.Column(JSONB, default=[])  # Store items as JSON array
    total_cost = db.Column(db.Integer, default=0, nullable=False)

    def to_dict(self):
        return {
            "order_id": str(self.id),
            "paid": self.paid,
            "items": self.items,
            "user_id": self.user_id,
            "total_cost": self.total_cost
        }

with app.app_context():
    db.create_all() 

atexit.register(close_db_connection)

class OrderValue(Struct):
    paid: bool
    items: list[tuple[str, int]]
    user_id: str
    total_cost: int


def get_order_from_db(order_id: str) -> OrderValue | None:
    order: Order = Order.query.get(order_id)
    if order is None:
        # if order does not exist in the database; abort
        abort(400, f"Order: {order_id} not found!")
    return order

@app.post('/create/<user_id>')
def create_order(user_id: str):
    order = Order(user_id=user_id)
    try:
        db.session.add(order)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return abort(400, DB_ERROR_STR)
    
    return jsonify({'order_id': str(order.id)})


@app.post('/batch_init/<n>/<n_items>/<n_users>/<item_price>')
def batch_init_users(n: int, n_items: int, n_users: int, item_price: int):

    n, n_items, n_users, item_price = map(int, [n, n_items, n_users, item_price])
    
    orders = []
    for _ in range(n):
        user_id = str(random.randint(0, n_users - 1))
        items = [(str(random.randint(0, n_items - 1)), 1) for _ in range(2)]
        orders.append(Order(user_id=user_id, items=items, total_cost=2 * item_price))
    
    try:
        db.session.bulk_save_objects(orders)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for orders successful"})


@app.get('/find/<order_id>')
def find_order(order_id: str):
    order_entry: Order = get_order_from_db(order_id)
    return jsonify(order_entry.to_dict())


def send_post_request(url: str):
    try:
        response = requests.post(url)
    except requests.exceptions.RequestException:
        abort(400, REQ_ERROR_STR)
    else:
        return response


def send_get_request(url: str):
    try:
        response = requests.get(url)
    except requests.exceptions.RequestException:
        abort(400, REQ_ERROR_STR)
    else:
        return response


@app.post('/addItem/<order_id>/<item_id>/<quantity>')
def add_item(order_id: str, item_id: str, quantity: int):
    order: Order = get_order_from_db(order_id)
    item_reply = send_get_request(f"{GATEWAY_URL}/stock/find/{item_id}")
    if item_reply.status_code != 200:
        # Request failed because item does not exist
        abort(400, f"Item: {item_id} does not exist!")
    item_json: dict = item_reply.json()
    order.items.append((item_id, int(quantity)))
    order.total_cost += int(quantity) * item_json["price"]
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return abort(400, DB_ERROR_STR)
    return Response(f"Item: {item_id} added to: {order_id} price updated to: {order.total_cost}",
                    status=200)


def rollback_stock(removed_items: list[tuple[str, int]]):
    for item_id, quantity in removed_items:
        send_post_request(f"{GATEWAY_URL}/stock/add/{item_id}/{quantity}")


@app.post('/checkout/<order_id>')
def checkout(order_id: str):
    app.logger.debug(f"Checking out {order_id}")
    order: Order = get_order_from_db(order_id)
    # get the quantity per item
    items_quantities: dict[str, int] = defaultdict(int)
    for item_id, quantity in order.items:
        items_quantities[item_id] += quantity
    # The removed items will contain the items that we already have successfully subtracted stock from
    # for rollback purposes.
    removed_items: list[tuple[str, int]] = []
    for item_id, quantity in items_quantities.items():
        stock_reply = send_post_request(f"{GATEWAY_URL}/stock/subtract/{item_id}/{quantity}")
        if stock_reply.status_code != 200:
            # If one item does not have enough stock we need to rollback
            rollback_stock(removed_items)
            abort(400, f'Out of stock on item_id: {item_id}')
        removed_items.append((item_id, quantity))
    user_reply = send_post_request(f"{GATEWAY_URL}/payment/pay/{order.user_id}/{order.total_cost}")
    if user_reply.status_code != 200:
        # If the user does not have enough credit we need to rollback all the item stock subtractions
        rollback_stock(removed_items)
        abort(400, "User out of credit")
    order.paid = True
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        return abort(400, DB_ERROR_STR)
    app.logger.debug("Checkout successful")
    return Response("Checkout successful", status=200)

@app.get('/check_isolation')
def check_isolation():
    result = db.session.execute(text('SHOW transaction_isolation;')).scalar()
    return jsonify({'isolation_level': result})

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
