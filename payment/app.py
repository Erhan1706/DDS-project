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
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.exc import OperationalError

from sqlalchemy.exc import OperationalError

DB_ERROR_STR = "DB error"
MAX_RETRIES = 10


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
        group_id='payment_action_listener',
        bootstrap_servers='kafka:9092',
        auto_offset_reset='latest',
        value_deserializer=lambda x: json.loads(x.decode('utf-8'))
    )
    with app.app_context():
        for message in consumer:
            #app.logger.info(f"Payment message consumed for {message.value['saga_id']}")
            retries = 0
            while retries < MAX_RETRIES:
                try:
                    # Atomic transaction block to make whole cart update a single transaction
                    db.session.begin()
                    payment_trans(message.value['user_id'], message.value['total_cost']) 
                    db.session.commit()
                    app.logger.info(f"Payment for {message.value['saga_id']} successful")
                    producer.send('payment_details_success', value={"saga_id": message.value['saga_id']})
                    break
                except ValueError as e: # No point in retrying if user has insufficient funds
                    db.session.rollback()
                    producer.send('payment_details_failure', value={"saga_id": message.value['saga_id']})
                    app.logger.error(f"Error updating payment for {message.value['saga_id']} due to insufficient funds")
                    break
                except OperationalError as e:
                    db.session.rollback()
                    app.logger.error(f"Error updating payment: {e}, current retries: {retries}")
                    retries += 1
            else:
                app.logger.error(f"Failed to update payment for {message.value['saga_id']}")
                producer.send('payment_details_failure', value={"saga_id": message.value['saga_id']})
            producer.flush()
            
def start_payment_compensation_consumer():
    consumer = KafkaConsumer(
        'compensate_payment_details',   
        group_id='payment_compensation_listener',
        bootstrap_servers='kafka:9092',
        auto_offset_reset='latest',
        value_deserializer=lambda x: json.loads(x.decode('utf-8'))
    )
    with app.app_context():
        for message in consumer:
            retries = 0
            while retries < MAX_RETRIES:
                try:
                    # Atomic transaction block to make whole cart update a single transaction
                    db.session.begin()
                    return_payment_trans(message.value['user_id'], message.value['total_cost']) 
                    db.session.commit()
                    break
                except OperationalError as e:
                    db.session.rollback()
                    app.logger.error(f"Error compensating payment: {e}, current retries: {retries}")
                    retries += 1
            else:
                app.logger.error(f"Failed to compensate payment for {message.value['saga_id']}")
            producer.flush()

def payment_trans(user_id: str, amount: int):
    user = get_user_from_db(user_id)
    user.credit -= int(amount)
    if user.credit < 0:
        raise ValueError(f"User: {user_id} credit cannot get reduced below zero!")
    db.session.add(user)

def return_payment_trans(user_id: str, amount: int):
    user = get_user_from_db(user_id)
    user.credit += int(amount)
    db.session.add(user)

# Start consumer in a separate thread
threading.Thread(target=start_payment_action_consumer, daemon=True).start()
threading.Thread(target=start_payment_compensation_consumer, daemon=True).start()

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.String, primary_key=True)
    credit = db.Column(db.Integer)  

def get_user_from_db(user_id: str) -> User | None:
    user = User.query.get(user_id)
    if user is None:
        raise ValueError(f"Item: {user_id} not found!")
    return user

@app.post('/create_user')
def create_user():
    key = str(uuid.uuid4())
    user = User(id=key, credit=0)
    try:
        db.session.add(user)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return abort(400, DB_ERROR_STR)
    return jsonify({'user_id': str(user.id)})


@app.post('/batch_init/<n>/<starting_money>')
def batch_init_users(n: int, starting_money: int):
    n = int(n)
    starting_money = int(starting_money)
    users = [User(id=key, credit=starting_money) for key in range(n)]
    try:
        db.session.bulk_save_objects(users)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return abort(400, DB_ERROR_STR)
    return jsonify({"msg": "Batch init for users successful"})


@app.get('/find_user/<user_id>')
def find_user(user_id: str):
    user_entry: User = get_user_from_db(user_id)
    return jsonify(
        {
            "user_id": user_id,
            "credit": user_entry.credit
        }
    )

@app.post('/add_funds/<user_id>/<amount>')
def add_credit(user_id: str, amount: int):
    user_entry: User = get_user_from_db(user_id)
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
        app.logger.error(f"Failed to add credit to user: {user_id}")
        return abort(400, DB_ERROR_STR)
    return Response(f"User: {user_id} credit updated to: {user_entry.credit}", status=200)


@app.post('/pay/<user_id>/<amount>')
def remove_credit(user_id: str, amount: int):
    user_entry: User = get_user_from_db(user_id)
    # update credit, serialize and update database
    user_entry.credit -= int(amount)
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
        app.logger.error(f"Failed to remove credit to user: {user_id}")
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
