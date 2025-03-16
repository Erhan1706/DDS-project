import random
import uuid
from collections import defaultdict
import requests

from flask import Flask, jsonify, abort, Response, Blueprint, current_app as app 
from sqlalchemy.exc import OperationalError
from threading import Event
from models import Order, OrderState, OrderValue
from __init__ import db, DB_ERROR_STR, REQ_ERROR_STR, GATEWAY_URL
from kafka_utils import orchestrator
#from asgiref.wsgi import WsgiToAsgi

MAX_RETRIES = 3
#asgi_app = WsgiToAsgi(app) 

order_bp = Blueprint('order_bp', __name__)

def get_order_from_db(order_id: str) -> OrderValue | None:
    order: Order = Order.query.get(order_id)
    if order is None:
        # if order does not exist in the database; abort
        abort(400, f"Order: {order_id} not found!")
    return order

@order_bp.post('/create/<user_id>')
def create_order(user_id: str):
    order = Order(user_id=user_id)
    try:
        db.session.add(order)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return abort(400, DB_ERROR_STR)
    return jsonify({'order_id': str(order.id)})


@order_bp.post('/batch_init/<n>/<n_items>/<n_users>/<item_price>')
def batch_init_users(n: int, n_items: int, n_users: int, item_price: int):

    n, n_items, n_users, item_price = map(int, [n, n_items, n_users, item_price])
    
    orders = []
    for i in range(n):
        user_id = str(random.randint(0, n_users - 1))
        items = {str(random.randint(0, n_items - 1)): 1 for _ in range(2)}
        orders.append(Order(id=str(i), user_id=user_id, items=items, total_cost=2 * item_price))
    try:
        db.session.bulk_save_objects(orders)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for orders successful"})


@order_bp.get('/find/<order_id>')
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


@order_bp.post('/addItem/<order_id>/<item_id>/<quantity>')
def add_item(order_id: str, item_id: str, quantity: int):
    order: Order = get_order_from_db(order_id)
    item_reply = send_get_request(f"{GATEWAY_URL}/stock/find/{item_id}")
    if item_reply.status_code != 200:
        # Request failed because item does not exist
        abort(400, f"Item: {item_id} does not exist!")
    item_json: dict = item_reply.json()
    order.items[item_id] = int(quantity)
    order.total_cost += int(quantity) * item_json["price"]
    retries = 0
    while retries < MAX_RETRIES:
        try:
            db.session.add(order)
            db.session.commit()
            break
        except OperationalError:
            db.session.rollback()
            retries += 1
    else:
        app.logger.error("Failed to add item")
        return abort(400, DB_ERROR_STR)

    return Response(f"Item: {item_id} added to: {order_id} price updated to: {order.total_cost}",
                    status=200)


def rollback_stock(removed_items: list[tuple[str, int]]):
    for item_id, quantity in removed_items:
        send_post_request(f"{GATEWAY_URL}/stock/add/{item_id}/{quantity}")


@order_bp.post('/checkout/<order_id>')
def checkout(order_id: str):
    order: Order = get_order_from_db(order_id)
    # get the quantity per item
    items_quantities: dict[str, int] = defaultdict(int)
    for item_id, quantity in order.items.items():
        items_quantities[item_id] += quantity

    saga_id = str(uuid.uuid4())
    event = Event()
    context = dict(order_id=order_id, items=items_quantities, user_id=order.user_id, total_cost=order.total_cost, saga_id=saga_id)
    orchestrator.start(event, context)

    if not event.wait(timeout=5):  # Block until notified or timeout
        app.logger.error("Checkout timed out")
        return Response("Checkout timed out", status=400)
    
    order_status = OrderState.query.filter_by(saga_id=saga_id).first()
    if order_status.state != "COMPLETED":
        app.logger.error("Failed to checkout")
        return abort(400, DB_ERROR_STR)
    order.paid = True
    retries = 0
    while retries < MAX_RETRIES:
        try:
            db.session.add(order)
            db.session.commit()
            break
        except OperationalError:
            db.session.rollback()
            retries += 1
    else:
        app.logger.error("Failed to commit order to database")
        return abort(400, DB_ERROR_STR)
    app.logger.info("Checkout successful")
    return Response("Checkout successful", status=200)