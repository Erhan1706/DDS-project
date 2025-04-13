import random
import uuid
import json
from collections import defaultdict
import requests

from flask import jsonify, abort, Response, Blueprint, current_app as app 
from sqlalchemy.exc import OperationalError
from threading import Event
from models import Order, OrderState, OrderValue
from __init__ import db, DB_ERROR_STR, REQ_ERROR_STR, GATEWAY_URL, pubsub
from kafka_utils import orchestrator

MAX_RETRIES = 20

order_bp = Blueprint('order_bp', __name__)

def get_order_from_db(order_id: str) -> OrderValue | None:
    order: Order = Order.query.get(order_id)
    if order is None:
        # if order does not exist in the database; abort
        abort(400, f"Order: {order_id} not found!")
    return order

def get_order_state(order_id: str) -> OrderState | None:
    retries = 0
    while retries < MAX_RETRIES:
        try:
            order_state: OrderState = OrderState.query.get(order_id)
            break
        except OperationalError:
            db.session.rollback()
            retries += 1
    else:
        app.logger.error(f"Failed to get order state: {order_id}")
        return abort(400, DB_ERROR_STR)
    return order_state

@order_bp.post('/create/<user_id>')
def create_order(user_id: str):
    retries = 0
    db.session.rollback()
    while retries < MAX_RETRIES:
        try:
            order = Order(user_id=user_id)
            db.session.add(order)
            db.session.commit()
            break
        except OperationalError:
            db.session.rollback()
            retries += 1
    else:
        app.logger.error("Failed to add item")
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
        app.logger.error(f"Failed to save orders: {orders}, they probably already exist!")
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
    item_reply = send_get_request(f"{GATEWAY_URL}/stock/find/{item_id}")
    if item_reply.status_code != 200:
        # Request failed because item does not exist
        abort(400, f"Item: {item_id} does not exist!")
    item_json: dict = item_reply.json()
    retries = 0
    while retries < MAX_RETRIES:
        try:
            order: Order = get_order_from_db(order_id)
            order.items[item_id] = int(quantity)
            order.total_cost += int(quantity) * item_json["price"]
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
    app.logger.info(f"Checkout for order {order_id}")
    order_state = get_order_state(order_id)
    if order_state is not None:
        if order_state.state == "COMPLETED":
            return Response("Checkout successful", status=200)
        if order_state.state == "FAILED":
            return abort(400, "Order has failed already")
        else:
            app.logger.info(f"Subscribing for already existing order: {order_id}")
            pubsub.subscribe(f"event_finished: {order_state.saga_id}")
    else:
        order: Order = get_order_from_db(order_id)
        # get the quantity per item
        items_quantities: dict[str, int] = defaultdict(int)
        for item_id, quantity in order.items.items():
            items_quantities[item_id] += quantity

        saga_id = order_id
        event = Event()
        context = dict(order_id=order_id, items=items_quantities, user_id=order.user_id, total_cost=order.total_cost, saga_id=saga_id)
        orchestrator.start(event, context)

        pubsub.subscribe(f"event_finished: {saga_id}")

    for message in pubsub.listen():
        #app.logger.info(message)
        if message["type"] != "message":
            continue
        data = json.loads(message['data'])
        saga_id = data.get('saga_id')
        order_status = OrderState.query.get(saga_id)
        if order_status.state != "COMPLETED":
            return abort(400, DB_ERROR_STR)
        break # break out of the loop if the order is completed

    pubsub.unsubscribe(f"event_finished: {saga_id}")
    app.logger.info(f"Checkout successful for {saga_id}")
    return Response("Checkout successful", status=200)