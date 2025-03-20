from typing import List
import json
import redis
from kafka import KafkaProducer
from threading import Event

from sqlalchemy.exc import OperationalError

from models import OrderState
from __init__ import db, DB_ERROR_STR, redis_db
from flask import abort, current_app as app

MAX_RETRIES = 3

class Step():
    def __init__(self, name, action, compensation):
        self.name = name
        self.action = action
        self.compensation = compensation

    def run(self, context):
        self.action(context)

    def revert(self, context):
        self.compensation(context)

class EventFinisher():
    def __init__(self, action):
        self.action = action

    def run(self, context):
        self.action(context)

class Orchestrator():

    def __init__(self, producer: KafkaProducer, steps: List[Step] = [], finishing_event: EventFinisher = None):
        self.finishing_event = finishing_event
        self.steps: List[Step] = steps
        self.producer: KafkaProducer = producer
        self.pending_events: dict[str, Event] = {}

    def add_saga(self, saga_id, sagas_info: dict):
        redis_db.set(saga_id, json.dumps(sagas_info))

    def get_saga(self, saga_id):
        try:
            entry = json.loads(redis_db.get(saga_id))
            return entry
        except redis.exceptions.RedisError:
            raise ValueError(f"Saga: {saga_id} not found in Redis!")

    def get_next_step(self, step: int| None = None) -> int | None:
        return 0 if step == len(self.steps) - 1 else step + 1
            
    def start(self, event: Event, context: dict):
        saga_id = context["saga_id"]
        self.add_saga(saga_id, {
            "current_step": 0,
            "context": {
                "order_id": context["order_id"],
                "items": context["items"],
                "user_id": context["user_id"],
                "total_cost": context["total_cost"],
                "saga_id": saga_id
            }
        })
        # Put current order in PENDING state
        orderStatus: OrderState = OrderState(saga_id=saga_id, order_id=context["order_id"], state="PENDING")
        try: 
            db.session.add(orderStatus)
            db.session.commit()
        except Exception:
            db.session.rollback()

        self.steps[0].run(context)
        self.pending_events[saga_id] = event

    def process_step(self, topic: str, saga_id: str):
        if topic == "stock_details_failure" or topic == "payment_details_failure":
            self.compensate(saga_id)
            return
        saga = self.get_saga(saga_id)
        # Any other topic message is a success message -> move to the next step
        current_step = self.get_next_step(saga["current_step"])
        saga["current_step"] = current_step
        self.add_saga(saga_id, saga)
        if current_step:
            self.steps[current_step].run(saga["context"])
        else:
            # Update order state to COMPLETED and notify the main thread
            orderStatus = OrderState.query.filter_by(saga_id=saga_id).first()
            orderStatus.state = "COMPLETED"
            retries = 0
            while retries < MAX_RETRIES:
                try:
                    db.session.add(orderStatus)
                    db.session.commit()
                    break
                except OperationalError:
                    db.session.rollback()
                    retries += 1
            else:
                app.logger.error(f"Failed to change order state to completed {saga_id}")
                return abort(400, DB_ERROR_STR)
            if saga_id in self.pending_events:
                self.pending_events[saga_id].set()
            else:
                self.finishing_event.run({"saga_id": saga_id})
                

    def compensate(self, saga_id: str):
        orderStatus = OrderState.query.filter_by(saga_id=saga_id).first()
        if orderStatus is None:
            return abort(400, f"Order: {saga_id} not found!")
        orderStatus.state = "FAILED"
        retries = 0
        while retries < MAX_RETRIES:
            try:
                db.session.add(orderStatus)
                db.session.commit()
                break
            except OperationalError:
                db.session.rollback()
                retries += 1
        else:
            app.logger.error(f"Failed to change order state to failed {saga_id}")
            return abort(400, DB_ERROR_STR)
        app.logger.info(f"Halfway here: {saga_id}")
        saga = self.get_saga(saga_id)
        current_step: int = saga["current_step"]
        app.logger.info(f"Halfway here2: {saga_id}")
        # Revert all steps
        current_step -= 1
        while current_step >= 0:
            try:
              self.steps[current_step].revert(saga["context"])
              current_step -= 1
            except Exception as e:
                app.logger.error(f"Compensation error in {self.steps[current_step].name}: {e}")
        # Notify the main thread
        if saga_id in self.pending_events:
            self.pending_events[saga_id].set()
        else:
            self.finishing_event.run({"saga_id": saga_id})

    def finish_event(self, saga_id: str):
        if saga_id in self.pending_events:
            self.pending_events[saga_id].set()