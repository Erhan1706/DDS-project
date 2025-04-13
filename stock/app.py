import logging
import atexit
import uuid
import json
import threading

from flask import Flask, jsonify, abort, Response, request
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import OperationalError
from sqlalchemy import update
from kafka import KafkaProducer, KafkaConsumer
from msgspec import Struct, msgpack
from psycopg2 import errors


DB_ERROR_STR = "DB error"
MAX_RETRIES = 10

app = Flask("stock-service")

app.config["SQLALCHEMY_DATABASE_URI"] = "postgresql://postgres:postgres@stock-pgpool:5432/stock_db"
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "isolation_level": "SERIALIZABLE",  # Strongest isolation level for postgres
    "pool_pre_ping": True,
    "connect_args":{
        "keepalives": 1,
        "keepalives_idle": 20,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    }
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

def start_stock_action_consumer():
    consumer = KafkaConsumer(
        'verify_stock_details',
        group_id='stock_action_listener',
        bootstrap_servers='kafka:9092',
        auto_offset_reset='latest',
        value_deserializer=lambda x: json.loads(x.decode('utf-8'))
    )
    with app.app_context():
        for message in consumer:
            #app.logger.info(f"Stock message consumed for {message.value['saga_id']}")
            saga_id = message.value['saga_id']
            with db.session.begin():
                val = ProcessedTransaction.query.get(saga_id)
                if val is not None:
                    app.logger.warning(f"Payment action already processed: {saga_id}")
                    continue

            retries = 0
            while retries < MAX_RETRIES:
                try:
                    # Atomic transaction block to make whole cart update a single transaction
                    db.session.begin()
                    for item_id, amount in message.value["items"].items():
                        remove_stock_trans(item_id, amount)
                    transaction = ProcessedTransaction(saga_id=saga_id)
                    db.session.add(transaction)
                    db.session.commit()
                    producer.send('stock_details_success', value={"saga_id": message.value['saga_id']})
                    app.logger.info(f"Stock for {message.value['saga_id']} successful")
                    break
                except ValueError as e: # No point in retrying if stock goes below zero
                    db.session.rollback()
                    db.session.begin()
                    transaction = ProcessedTransaction(saga_id=saga_id)
                    db.session.add(transaction)
                    db.session.commit()
                    producer.send('stock_details_failure', value={"saga_id": message.value['saga_id']})
                    app.logger.error(f"Error updating stock for {message.value['saga_id']}: insufficient stock")
                    break  # No point retrying

                except (OperationalError, errors.SerializationFailure) as e:
                    db.session.rollback()
                    app.logger.error(f"Retryable error updating stock: {e}, current retries: {retries}")
                    retries += 1

                except Exception as e:
                    db.session.rollback()
                    app.logger.error(f"Unexpected error updating stock: {e}")
                    raise

            else:
                app.logger.error(f"Max retries reached for stock update: {saga_id}")
                producer.send('stock_details_failure', value={"saga_id": message.value['saga_id']})
            producer.flush()


def remove_stock_trans(item_id: str, amount: int):
    result = db.session.execute(
        update(Stock)
        .where(Stock.id == item_id)
        .where(Stock.stock >= amount)  # Ensure no negative stock
        .values(stock=Stock.stock - amount)
    )

    if result.rowcount == 0:
        raise ValueError(f"Item: {item_id} stock cannot get reduced below zero!")


def start_stock_compensation_consumer():
    consumer = KafkaConsumer(
        'compensate_stock_details',
        group_id='stock_compensation_listener',
        bootstrap_servers='kafka:9092',
        auto_offset_reset='latest',
        value_deserializer=lambda x: json.loads(x.decode('utf-8'))
    )
    with app.app_context():
        for message in consumer:
            #app.logger.info(f"Stock message consumed for {message.value['saga_id']}")
            saga_id = message.value['saga_id']
            with db.session.begin():
                val = RevertedTransaction.query.get(saga_id)
                if val is not None:
                    app.logger.warning(f"Payment action already reverted: {saga_id}")
                    continue

            retries = 0
            while retries < MAX_RETRIES:
                try:
                    # Atomic transaction block to make whole cart update a single transaction
                    db.session.begin()
                    for item_id, amount in message.value["items"].items():
                        add_stock_trans(item_id, amount)
                    transaction = RevertedTransaction(saga_id=saga_id)
                    db.session.add(transaction)
                    db.session.commit()
                    break
                except OperationalError as e:
                    db.session.rollback()
                    app.logger.error(f"Retryable error compensating stock: {e}, current retries: {retries}")
                    retries += 1

                except Exception as e:
                    db.session.rollback()
                    app.logger.error(f"Unexpected error compensating stock: {e}")
                    raise

            else:
                app.logger.error(f"Max retries reached for stock compensation: {saga_id}")
            producer.flush()

def add_stock_trans(item_id: str, amount: int):
    result = db.session.execute(
        update(Stock)
        .where(Stock.id == item_id)
        .values(stock=Stock.stock + amount)
    )

    if result.rowcount == 0:
        raise ValueError(f"Item: {item_id} not found!")


class ProcessedTransaction(db.Model):
    __tablename__ = 'processed_transactions'
    saga_id = db.Column(db.String, primary_key=True)
    status = db.Column(db.String)

class RevertedTransaction(db.Model):
    __tablename__ = 'reverted_transactions'
    saga_id = db.Column(db.String, primary_key=True)


class Stock(db.Model):
    __tablename__ = "stock"
    id = db.Column(db.String, primary_key=True, default=lambda: str(uuid.uuid4()))
    stock = db.Column(db.Integer, nullable=False, default=0)
    price = db.Column(db.Integer, nullable=False)

threading.Thread(target=start_stock_action_consumer, daemon=True).start()
threading.Thread(target=start_stock_compensation_consumer, daemon=True).start()

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
    kv_pairs: dict[str, bytes] = {f"{i}": msgpack.encode(StockValue(stock=starting_stock, price=item_price))
                                  for i in range(n)}
    items = [Stock(id=str(key), stock=starting_stock, price=item_price) for key in kv_pairs.keys()]
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
    retries = 0
    while retries < MAX_RETRIES:
        try:
            item = get_item_from_db(item_id)
            item.stock += int(amount)
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
    retries = 0
    while retries < MAX_RETRIES:
        try:
            item = get_item_from_db(item_id)
            item.stock -= int(amount)
            if item.stock < 0:
                abort(400, f"Item: {item_id} stock cannot get reduced below zero!")
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
    
if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
