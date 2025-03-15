import logging
import os
import atexit
import uuid

import redis
from flask_sqlalchemy import SQLAlchemy

from msgspec import msgpack, Struct
from flask import Flask, jsonify, abort, Response
from flask import request
from kafka import KafkaProducer, KafkaConsumer
from sqlalchemy.dialects.postgresql import UUID
import json
import threading

from sqlalchemy.exc import OperationalError

DB_ERROR_STR = "DB error"
MAX_RETRIES = 3


app = Flask("payment-service")

app.config["SQLALCHEMY_DATABASE_URI"] = "postgresql://postgres:postgres@payment-postgres:5432/payment_db"
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "isolation_level": "SERIALIZABLE"  # Strongest isolation level for postgres
}

db = SQLAlchemy(app)

def close_db_connection():
    with app.app_context():
        db.session.close()

atexit.register(close_db_connection)

producer = KafkaProducer(
    bootstrap_servers='kafka:9092',
    value_serializer=lambda v: json.dumps(v).encode('utf-8')
)

def start_payment_action_consumer():
    consumer = KafkaConsumer(
        'verify_payment_details',
        bootstrap_servers='kafka:9092',
        auto_offset_reset='latest',
        value_deserializer=lambda x: json.loads(x.decode('utf-8'))
    )
    with app.app_context():
        for message in consumer:
            app.logger.info(f"Payment message consumed for {message.value['saga_id']}")
            try:
                payment_trans(message.value['user_id'], message.value['total_cost'])
                producer.send('payment_details_success', value={"saga_id": message.value['saga_id']})
            except Exception as e:
                db.session.rollback()
                app.logger.error(f"Error updating payment: {e}")
                producer.send('payment_details_failure', value={"saga_id": message.value['saga_id'], "reason": str(e)})
            producer.flush()

def start_payment_compensation_consumer():
    consumer = KafkaConsumer(
        'compensate_payment_details',
        bootstrap_servers='kafka:9092',
        auto_offset_reset='latest',
        value_deserializer=lambda x: json.loads(x.decode('utf-8'))
    )
    with app.app_context():
        for message in consumer:
            app.logger.info(f"Payment compensation message consumed for {message.value['saga_id']}")
            try:
                return_payment_trans(message.value['user_id'], message.value['total_cost'])
            except Exception as e:
                db.session.rollback()
                app.logger.error(f"Error updating stock: {e}")
            producer.flush()

def payment_trans(user_id: str, amount: int):
    db.session.begin()
    user = get_user_from_db(user_id)
    user.credit -= int(amount)
    if user.credit < 0:
        raise ValueError(f"User: {user_id} credit cannot get reduced below zero!")
    db.session.add(user)
    db.session.commit()

def return_payment_trans(user_id: str, amount: int):
    db.session.begin()
    user = get_user_from_db(user_id)
    user.credit += int(amount)
    db.session.add(user)
    db.session.commit()

# Start consumer in a separate thread
threading.Thread(target=start_payment_action_consumer, daemon=True).start()
threading.Thread(target=start_payment_compensation_consumer, daemon=True).start()

class UserValue(db.Model):
    __tablename__ = "user_value_table"
    id = db.Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    credit = db.Column(db.Integer, nullable=False, default=0)

""" Temporary test endpoint to send messages to Kafka """
@app.post('/send')
def send_message():
    data = request.json
    message = data.get('message')
    producer.send('test-topic', value=message)
    producer.flush()
    return jsonify({'status': 'Message sent to Kafka'}), 200


def get_user_from_db(user_id: str) -> UserValue | None:
    user = UserValue.query.get(user_id)
    if user is None:
        raise ValueError(f"Item: {user_id} not found!")
    return user


@app.post('/create_user')
def create_user():
    user = UserValue(credit=0)
    try:
        db.session.add(user)
        db.session.commit()
    except Exception:
        return abort(400, DB_ERROR_STR)
    return jsonify({'user_id': str(user.id)})


@app.post('/batch_init/<n>/<starting_money>')
def batch_init_users(n: int, starting_money: int):
    n = int(n)
    starting_money = int(starting_money)
    users = [UserValue(credit=starting_money) for _ in range(n)]
    try:
        db.session.bulk_save_objects(users)
        db.session.commit()
    except Exception:
        return abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for users successful"})


@app.get('/find_user/<user_id>')
def find_user(user_id: str):
    user_entry: UserValue = get_user_from_db(user_id)
    return jsonify(
        {
            "user_id": user_id,
            "credit": user_entry.credit
        }
    )


@app.post('/add_funds/<user_id>/<amount>')
def add_credit(user_id: str, amount: int):
    user_entry: UserValue = get_user_from_db(user_id)
    # update credit, serialize and update database
    user_entry.credit += int(amount)
    retries = 0
    while retries < MAX_RETRIES:
        try:
            db.session.add(user_entry)
            db.session.commit()
            break
        except OperationalError:
            db.session.rollback()
            retries += 1
    else:
        app.logger.error("Failed to add funds")
        return abort(400, DB_ERROR_STR)
    return Response(f"User: {user_id} credit updated to: {user_entry.credit}", status=200)


@app.post('/pay/<user_id>/<amount>')
def remove_credit(user_id: str, amount: int):
    app.logger.debug(f"Removing {amount} credit from user: {user_id}")
    user_entry: UserValue = get_user_from_db(user_id)
    # update credit, serialize and update database
    user_entry.credit -= int(amount)
    if user_entry.credit < 0:
        abort(400, f"User: {user_id} credit cannot get reduced below zero!")
    retries = 0
    while retries < MAX_RETRIES:
        try:
            db.session.add(user_entry)
            db.session.commit()
            break
        except OperationalError:
            db.session.rollback()
            retries += 1
    else:
        app.logger.error(f"Failed to pay for {user_id} amount {amount}")
        return abort(400, DB_ERROR_STR)
    return Response(f"User: {user_id} credit updated to: {user_entry.credit}", status=200)

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=8000, debug=True)
else:
    gunicorn_logger = logging.getLogger('gunicorn.error')
    app.logger.handlers = gunicorn_logger.handlers
    app.logger.setLevel(gunicorn_logger.level)
