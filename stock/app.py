import logging
import atexit
import uuid
import json
import threading

from flask import Flask, jsonify, abort, Response, request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.exc import OperationalError
from kafka import KafkaProducer, KafkaConsumer
from msgspec import Struct


DB_ERROR_STR = "DB error"
MAX_RETRIES = 3

app = Flask("stock-service")

app.config["SQLALCHEMY_DATABASE_URI"] = "postgresql://postgres:postgres@stock-postgres:5432/stock_db"
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "isolation_level": "SERIALIZABLE"  # Strongest isolation level for postgres
}

db = SQLAlchemy(app)

def close_db_connection():
    with app.app_context():
        db.session.close()

atexit.register(close_db_connection)

class StockValue(Struct):
    stock: int
    price: int


producer = KafkaProducer(
    bootstrap_servers='kafka:9092',
    value_serializer=lambda v: json.dumps(v).encode('utf-8')
)

# Temporary consumers to test saga behaviour
def start_stock_action_consumer():
    consumer = KafkaConsumer(
        'verify_stock_details',
        bootstrap_servers='kafka:9092',
        auto_offset_reset='latest',
        value_deserializer=lambda x: json.loads(x.decode('utf-8'))
    )
    for message in consumer:
        app.logger.info(f"Stock message consumed for {message.value['saga_id']}")
        try:
            # Atomic transaction block to make whole cart update a single transaction
            with db.session.begin():
                for item_id, amount in message.value["items"].items():
                    remove_stock_trans(item_id, amount) 
            producer.send('stock_details_success', value={"saga_id": message.value['saga_id']})
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error updating stock: {e}")
            producer.send('stock_details_failure', value={"saga_id": message.value['saga_id'], "reason": str(e)})

        producer.flush()

def remove_stock_trans(item_id: str, amount: int):
    item = get_item_from_db(item_id)
    item.stock -= int(amount)
    if item.stock < 0:
        raise ValueError(f"Item: {item_id} stock cannot get reduced below zero!")
    db.session.add(item) 

def start_stock_compensation_consumer():
    consumer = KafkaConsumer(
        'compensate_stock_details',
        bootstrap_servers='kafka:9092',
        auto_offset_reset='latest',
        value_deserializer=lambda x: json.loads(x.decode('utf-8'))
    )
    for message in consumer:
        #app.logger.info(f"Stock message consumed for {message.value['saga_id']}")
        try:
            # Atomic transaction block to make whole cart update a single transaction
            with db.session.begin():
                for item_id, amount in message.value["items"].items():
                    add_stock_trans(item_id, amount) 
            #producer.send('stock_details_success', value=message.value)
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"Error updating stock: {e}")
            #producer.send('stock_details_failure', value={"saga_id": message.value['saga_id'], "reason": str(e)})
        producer.flush()

def add_stock_trans(item_id: str, amount: int):
    item = get_item_from_db(item_id)
    item.stock += int(amount)
    db.session.add(item) 

def start_payment_consumer():
    consumer = KafkaConsumer(
        'verify_payment_details',
        bootstrap_servers='kafka:9092',
        auto_offset_reset='latest',
        value_deserializer=lambda x: json.loads(x.decode('utf-8'))
    )
    for message in consumer:
        app.logger.info("Payment message consumed")
        producer.send('payment_details_success', value=message.value)

class Stock(db.Model):
    __tablename__ = "stock"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    stock = db.Column(db.Integer, nullable=False, default=0)
    price = db.Column(db.Integer, nullable=False)

@app.post('/send')
def send_message():
    data = request.json
    message = data.get('message')
    producer.send('test-topic', value=message)
    producer.flush()
    return jsonify({'status': 'Message sent to Kafka'}), 200

threading.Thread(target=start_stock_action_consumer, daemon=True).start()
threading.Thread(target=start_stock_compensation_consumer, daemon=True).start()
threading.Thread(target=start_payment_consumer, daemon=True).start()

def get_item_from_db(item_id: str) -> Stock:
    item = Stock.query.get(item_id)
    if item is None:
        raise ValueError(f"Item: {item_id} not found!")
        #abort(400, f"Item: {item_id} not found!")
    return item

@app.post('/item/create/<price>')
def create_item(price: int):
    item = Stock(price=int(price), stock=0)
    try:
        db.session.add(item)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return abort(400, DB_ERROR_STR)
    return jsonify({'item_id': str(item.id)})

@app.post('/batch_init/<n>/<starting_stock>/<item_price>')
def batch_init_stock(n: int, starting_stock: int, item_price: int):
    n = int(n)
    starting_stock = int(starting_stock)
    item_price = int(item_price)
    items = [Stock(stock=starting_stock, price=item_price)
                                    for i in range(n)]
    try:
        db.session.bulk_save_objects(items)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for stock successful"})

@app.get('/find/<item_id>')
def find_item(item_id: str):
    item = get_item_from_db(item_id)
    return jsonify(
        {
            "stock": item.stock, 
            "price": item.price
        }
    )

@app.post('/add/<item_id>/<amount>')
def add_stock(item_id: str, amount: int):
    item = get_item_from_db(item_id)
    item.stock += int(amount)
    retries = 0
    while retries < MAX_RETRIES:
        try:
            db.session.add(item)
            db.session.commit()
            break
        except OperationalError:
            db.session.rollback()
            retries += 1
    else:
        app.logger.error("Failed to add stock")
        return abort(400, DB_ERROR_STR)
    return Response(f"Item: {item_id} stock updated to: {item.stock}", status=200)

@app.post('/subtract/<item_id>/<amount>')
def remove_stock(item_id: str, amount: int):
    item = get_item_from_db(item_id)
    item.stock -= int(amount)
    if item.stock < 0:
        abort(400, f"Item: {item_id} stock cannot get reduced below zero!")
    retries = 0
    while retries < MAX_RETRIES:
        try:
            db.session.add(item)
            db.session.commit()
            break
        except OperationalError:
            db.session.rollback()
            retries += 1
    else:
        app.logger.error("Failed to subtract stock")
        return abort(400, DB_ERROR_STR)
    return Response(f"Item: {item_id} stock updated to: {item.stock}", status=200)

with app.app_context():
    db.create_all()
    
if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
