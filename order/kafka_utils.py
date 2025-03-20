from kafka import KafkaProducer, KafkaConsumer
import json
from models import OrderState
from flask import abort, current_app as app
from __init__ import db, DB_ERROR_STR, REQ_ERROR_STR
from orchestrator import Orchestrator, Step, EventFinisher


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

def send_finished_event(data: dict):
    producer.send('event_finished', value=dict(saga_id=data["saga_id"]))
    producer.flush()
    app.logger.info(f"Sent event finished for {data["saga_id"]}")

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
            handle_stock_message(message.value, message.topic)

def handle_stock_message(data: dict, topic: str):
    saga_id = data.get("saga_id")
    if not saga_id:
        app.logger.error("No saga_id in message")
        return
    app.logger.info(f"Consumed message: {data}")
    try: 
        orchestrator.process_step(topic, saga_id)
    except Exception as e:
        app.logger.error(f"Error in getting order state: {e}")
        return

def handle_finished_event_message(data: dict, topic: str):
    saga_id = data.get("saga_id")
    if not saga_id:
        app.logger.error("No saga_id in message")
        return
    app.logger.info(f"Consumed message: {data}")
    try:
        orchestrator.finish_event(saga_id)
    except Exception as e:
        app.logger.error(f"Error in finishing event: {e}")
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
            handle_stock_message(message.value, message.topic)

def start_event_finished_listener(app):
    with app.app_context():
        consumer = KafkaConsumer(
            'payment_details_success',
            'payment_details_failure',
            group_id='event_finished_listener',
            bootstrap_servers='kafka:9092',
            auto_offset_reset='latest',
            value_deserializer=lambda x: json.loads(x.decode('utf-8'))
        )
        for message in consumer:
            handle_finished_event_message(message.value, message.topic)

stock_step = Step("verify_stock", send_stock_event, send_stock_rollback)
payment_step = Step("verify_payment", send_payment_event, send_payment_rollback)
finishing_step = EventFinisher(send_finished_event)
orchestrator = Orchestrator(producer, steps=[stock_step, payment_step], finishing_event = finishing_step)
