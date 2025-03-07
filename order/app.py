import logging
import os
import atexit
import random
import uuid
from collections import defaultdict

#import redis
import requests

from msgspec import msgpack, Struct
from flask import Flask, jsonify, abort, Response
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.dialects.postgresql import UUID, JSONB
from flask import request
from kafka import KafkaProducer, KafkaConsumer
import json
import threading
from sqlalchemy.exc import OperationalError
from orchestrator import Orchestrator, Step
from threading import Event
#from asgiref.wsgi import WsgiToAsgi


# For debugging purposes
#import debugpy
#debugpy.listen(("0.0.0.0", 5678))


DB_ERROR_STR = "DB error"
REQ_ERROR_STR = "Requests error"

GATEWAY_URL = os.environ['GATEWAY_URL']

MAX_RETRIES = 3

#db: redis.Redis = redis.Redis(host=os.environ['REDIS_HOST'],
#                              port=int(os.environ['REDIS_PORT']),
#                              password=os.environ['REDIS_PASSWORD'],
#                              db=int(os.environ['REDIS_DB']))
app = Flask("order-service")

app.config["SQLALCHEMY_DATABASE_URI"]  = f'postgresql://postgres:postgres@order-postgres:5432/order-db'

app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "isolation_level": "SERIALIZABLE" # Strongest isolation level for postgres
}

db: SQLAlchemy = SQLAlchemy(app)

#asgi_app = WsgiToAsgi(app) 

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
    
class OrderState(db.Model):
    __tablename__ = "order_states"

    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    saga_id = db.Column(UUID(as_uuid=True), nullable=False)
    order_id = db.Column(UUID(as_uuid=True), db.ForeignKey('orders.id'), nullable=False)
    state = db.Column(db.String(50), nullable=False)

with app.app_context():
    db.create_all() 

atexit.register(close_db_connection)

producer = KafkaProducer(
    bootstrap_servers='kafka:9092',
    value_serializer=lambda v: json.dumps(v).encode('utf-8')
)

def start_consumer():
    consumer = KafkaConsumer(
        'test-topic',
        bootstrap_servers='kafka:9092',
        auto_offset_reset='earliest',
        value_deserializer=lambda x: json.loads(x.decode('utf-8'))
    )
    for message in consumer:
        print(f"Consumed message: {message.value}")

# Start consumer in a separate thread
threading.Thread(target=start_consumer, daemon=True).start() 

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

@app.get("/get")
def test_get():
    producer.send("stock_details_success", value={"order_id": "1234"})
    #producer.send("stock_details_failure", value={"order_id": "1234"})
    return jsonify({"msg": "Hello from order service"})

""" Temporary test endpoint to send messages to Kafka """
@app.post('/send')
def send_message():
    data = request.json
    message = data.get('message')
    producer.send('test-topic', value=message)
    producer.flush()
    return jsonify({'status': 'Message sent to Kafka'}), 200

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
    #removed_items: list[tuple[str, int]] = []

    saga_id = str(uuid.uuid4())
    event = Event()
    orchestrator.start(order_id, items_quantities, saga_id)

    if not event.wait(timeout=5):  # Block until notified or timeout
        app.logger.error("Checkout timed out")
        return Response("Checkout timed out", status=400)
    
    order_status = OrderState.query.filter_by(order_id=order_id).first()
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
        return Response("Checkout successful", status=200)
    else:
        app.logger.error("Failed to commit order to database")
        return abort(400, DB_ERROR_STR)
    """ user_reply = send_post_request(f"{GATEWAY_URL}/payment/pay/{order.user_id}/{order.total_cost}")
    if user_reply.status_code != 200:
        # If the user does not have enough credit we need to rollback all the item stock subtractions
        rollback_stock(removed_items)
        abort(400, "User out of credit")"
    """

def send_stock_rollback(order_id: str, items: dict[str, int], saga_id: str):
    orderStatus = OrderState.query.filter_by(order_id=order_id).first()
    if orderStatus is None:
        return abort(400, f"Order: {order_id} not found!")
    orderStatus.state = "FAILED" # Maybe delete this instead?
    try:
        db.session.add(orderStatus)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return abort(400, DB_ERROR_STR)
    producer.send('compensate_stock_details', value=dict(items=items, order_id=order_id, saga_id=saga_id))
    producer.flush()
    app.logger.debug("Sent compensating event for {order_id}")

def send_stock_event(order_id: str, items: dict[str, int], saga_id: str):
    orderStatus: OrderState = OrderState(saga_id=saga_id, order_id=order_id, state="PENDING")
    try:
        db.session.add(orderStatus)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return abort(400, DB_ERROR_STR)
    producer.send('verify_stock_details', value=dict(items=items, order_id=order_id, saga_id=saga_id))
    producer.flush()
    app.logger.info(f"Sent stock event for {saga_id}")

def start_stock_listener():
    consumer = KafkaConsumer(
        'stock_details_success',
        'stock_details_failure',
        bootstrap_servers='kafka:9092',
        auto_offset_reset='latest',
        value_deserializer=lambda x: json.loads(x.decode('utf-8'))
    )
    for message in consumer:
        handle_stock_message(message.value, message.topic)

def handle_stock_message(data: dict, topic: str):
    saga_id = data.get("saga_id")
    if not saga_id:
        app.logger.error("No saga_id in message")
        return
    app.logger.info(f"Consumed message: {data}")
    try: 
        # Get order state corresponding to the saga_id
        with app.app_context():
            orderState: OrderState = OrderState.query.filter_by(saga_id=saga_id).first()
            if orderState is None:
                app.logger.error(f"Order state not found for {saga_id}")
                return
        orchestrator.process_step(topic, saga_id)
    except Exception as e:
        app.logger.error(f"Error in getting order state: {e}")
        return
    
def start_payment_listener():
    consumer = KafkaConsumer(
        'payment_details_success',
        'payment_details_failure',
        bootstrap_servers='kafka:9092',
        auto_offset_reset='latest',
        value_deserializer=lambda x: json.loads(x.decode('utf-8'))
    )
    for message in consumer:
        handle_payment_message(message.value, message.topic)

def handle_payment_message(data: dict, topic: str):
    saga_id = data.get("saga_id")
    if not saga_id:
        app.logger.error("No saga_id in message")
        return
    app.logger.info(f"Consumed message: {data}")
    try: 
        # Get order state corresponding to the saga_id
        with app.app_context():
            orderState: OrderState = OrderState.query.filter_by(saga_id=saga_id).first()
            if orderState is None:
                app.logger.error(f"Order state not found for {saga_id}")
                return
        orchestrator.process_step(topic, saga_id)
    except Exception as e:
        app.logger.error(f"Error in getting order state: {e}")
        return

stock_step = Step("verify_stock", send_stock_event, send_stock_rollback)
orchestrator = Orchestrator(producer, steps=[stock_step])

threading.Thread(target=start_stock_listener, daemon=True).start()

def finish_checkout():
    app.logger.debug("Checkout successful")

if __name__ == '__main__':
    import uvicorn
    #uvicorn.run(asgi_app, host="0.0.0.0", port=8000, log_level="debug")
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
