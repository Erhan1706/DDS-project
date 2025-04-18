from kafka import KafkaProducer, KafkaConsumer
import json

from sqlalchemy.exc import OperationalError

from models import ProcessedPayment, ProcessedStock
from flask import abort, current_app as app
from __init__ import db, DB_ERROR_STR, REQ_ERROR_STR
from orchestrator import Orchestrator, Step

MAX_RETRIES = 10

producer = KafkaProducer(
    bootstrap_servers='kafka:9092',
    value_serializer=lambda v: json.dumps(v).encode('utf-8')
)

def send_stock_rollback(data: dict):
    producer.send('compensate_stock_details', value=dict(items=data["items"], order_id=data["order_id"], saga_id=data["saga_id"]))
    producer.flush()
    app.logger.info(f"Sent compensating stock event for {data["saga_id"]}")

def send_stock_event(data: dict):
    producer.send('verify_stock_details', value=dict(items=data["items"], order_id=data["order_id"], saga_id=data["saga_id"]))
    producer.flush()
    app.logger.info(f"Sent stock event for {data["saga_id"]}")

def send_payment_rollback(data: dict):
    producer.send('compensate_payment_details', value=dict(saga_id=data["saga_id"], user_id=data["user_id"], total_cost=data["total_cost"]))
    producer.flush()
    app.logger.info(f"Sent compensating payment event for {data["saga_id"]}")

def send_payment_event(data: dict):
    producer.send('verify_payment_details', value=dict(saga_id=data["saga_id"], user_id=data["user_id"], total_cost=data["total_cost"]))
    producer.flush()
    app.logger.info(f"Sent payment event for {data["saga_id"]}")

# def send_finished_event(data: dict):
#     producer.send('event_finished', value=dict(saga_id=data["saga_id"]))
#     producer.flush()
#     app.logger.info(f"Sent event finished for {data["saga_id"]}")

def start_stock_listener(app):
    with app.app_context():
        consumer = KafkaConsumer(
            'stock_details_success',
            'stock_details_failure',
            group_id='order_stock_listener',
            bootstrap_servers='kafka:9092',
            auto_offset_reset='latest',
            value_deserializer=lambda x: json.loads(x.decode('utf-8'))
        )
        for message in consumer:
            handle_message(message.value, message.topic)

def handle_message(data: dict, topic: str):
    saga_id = data.get("saga_id")
    if topic == 'stock_details_success' or topic == 'stock_details_failure':
        with db.session.begin():
            val = ProcessedStock.query.get(saga_id)
            if val is not None:
                app.logger.warning(f"Stock action already processed: {saga_id}")
                return
    else:
        with db.session.begin():
            val = ProcessedPayment.query.get(saga_id)
            if val is not None:
                app.logger.warning(f"Stock action already processed: {saga_id}")
                return
    if not saga_id:
        app.logger.error("No saga_id in message")
        return
    app.logger.info(f"Consumed message: {data} for {topic}")
    try: 
        orchestrator.process_step(topic, saga_id)
        if topic == 'stock_details_success' or topic == 'stock_details_failure':
            retries = 0
            while retries < MAX_RETRIES:
                try:
                    val = ProcessedStock(saga_id=saga_id)
                    db.session.add(val)
                    db.session.commit()
                    break
                except OperationalError:
                    db.session.rollback()
                    retries += 1
            else:
                app.logger.error(f"Failed to change order state for processed payment {saga_id}")
                return
        else:
            retries = 0
            while retries < MAX_RETRIES:
                try:
                    val = ProcessedPayment(saga_id=saga_id)
                    db.session.add(val)
                    db.session.commit()
                    break
                except OperationalError:
                    db.session.rollback()
                    retries += 1
            else:
                app.logger.error(f"Failed to change order state for processed payment {saga_id}")
                return
    except Exception as e:
        app.logger.error(f"Error in getting order state: {e} for data: {data}, topic: {topic}")
        return

def start_payment_listener(app):
    with app.app_context():
        consumer = KafkaConsumer(
            'payment_details_success',
            'payment_details_failure',
            group_id='order_payment_listener',
            bootstrap_servers='kafka:9092',
            auto_offset_reset='latest',
            value_deserializer=lambda x: json.loads(x.decode('utf-8'))
        )
        for message in consumer:
            handle_message(message.value, message.topic)

stock_step = Step("verify_stock", send_stock_event, send_stock_rollback)
payment_step = Step("verify_payment", send_payment_event, send_payment_rollback)
orchestrator = Orchestrator(producer, steps=[stock_step, payment_step])
