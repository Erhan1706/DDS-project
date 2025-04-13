from typing import List
import json
import redis
from kafka import KafkaProducer
from threading import Event

from sqlalchemy.exc import OperationalError

from models import OrderState
from __init__ import db, DB_ERROR_STR, redis_db, pubsub
from flask import abort, current_app as app
from time import sleep

MAX_RETRIES = 10

class Step():
    def __init__(self, name, action, compensation):
        self.name = name
        self.action = action
        self.compensation = compensation

    def run(self, context):
        self.action(context)

    def revert(self, context):
        self.compensation(context)

class Orchestrator():

    def __init__(self, producer: KafkaProducer, steps: List[Step] = []):
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
        return None if step == len(self.steps) - 1 else step + 1
            
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
        retries = 0
        while retries < MAX_RETRIES:
            try:
                orderStatus: OrderState = OrderState(saga_id=saga_id, order_id=context["order_id"], state="PENDING")
                db.session.add(orderStatus)
                db.session.commit()
                break
            except OperationalError:
                app.logger.info(f"Retrying to add order {saga_id} to DB, current attempt: {retries}")
                db.session.rollback()
                retries += 1
        else:
            app.logger.error(f"Failed to change order state to pending {saga_id}, order: {context['order_id']}")
            return abort(400, DB_ERROR_STR)
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
            retries = 0
            while retries < MAX_RETRIES:
                try:
                    orderStatus = OrderState.query.get(saga_id)
                    orderStatus.state = "COMPLETED"
                    db.session.add(orderStatus)
                    db.session.commit()
                    break
                except OperationalError:
                    db.session.rollback()
                    retries += 1
                    sleep(0.1 * retries)
            else:
                saga = self.get_saga(saga_id)
                saga["current_step"] = 2
                self.add_saga(saga_id, saga)
                self.compensate(saga_id)
                app.logger.error(f"Failed to change order state to completed {saga_id}")
                return abort(400, DB_ERROR_STR)
            redis_db.publish(f"event_finished: {saga_id}", json.dumps({"saga_id": saga_id}))
            #if saga_id in self.pending_events:
            #    self.pending_events[saga_id].set()
            #else:
            #    self.finishing_event.run({"saga_id": saga_id})                

    def compensate(self, saga_id: str):
        saga = self.get_saga(saga_id)
        current_step: int = saga["current_step"]
        # Revert all steps
        current_step -= 1
        while current_step >= 0:
            try:
              self.steps[current_step].revert(saga["context"])
              current_step -= 1
            except Exception as e:
                app.logger.error(f"Compensation error in {self.steps[current_step].name}: {e}")

        retries = 0
        while retries < MAX_RETRIES:
            try:
                orderStatus = OrderState.query.get(saga_id)
                if orderStatus is None:
                    app.logger.error(f"Failed to find order status {saga_id}")
                    return abort(400, f"Order: {saga_id} not found!")
                orderStatus.state = "FAILED"
                db.session.add(orderStatus)
                db.session.commit()
                break
            except OperationalError:
                db.session.rollback()
                retries += 1
        else:
            app.logger.error(f"Failed to change order state to failed {saga_id}")
            return abort(400, DB_ERROR_STR)
        redis_db.publish(f"event_finished: {saga_id}", json.dumps({"saga_id": saga_id}))

    def finish_event(self, saga_id: str):
        if saga_id in self.pending_events:
            self.pending_events[saga_id].set()