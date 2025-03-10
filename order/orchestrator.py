from typing import List
from kafka import KafkaProducer
from threading import Event
from models import OrderState
from __init__ import db, DB_ERROR_STR
from flask import abort

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
        self.sagas_info: dict[str, dict] = {}
    
    def add_step(self, step_name, action, compensation):
        step = Step(step_name, action, compensation)
        self.steps.append(step)

    def get_next_step(self, step: int| None = None) -> int | None:
        return 0 if step == len(self.steps) - 1 else step + 1
            
    def start(self, event: Event, context: dict):
        saga_id = context["saga_id"]
        self.sagas_info[saga_id] = {
            "current_step": 0,
            "context": {
                "order_id": context["order_id"],
                "items": context["items"],
                "user_id": context["user_id"],
                "total_cost": context["total_cost"],
                "saga_id": saga_id
            }
        }
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
        # Any other topic message is a success message -> move to the next step
        current_step = self.get_next_step(self.sagas_info[saga_id]["current_step"])
        self.sagas_info[saga_id]["current_step"] = current_step
        if current_step:
            self.steps[current_step].run(self.sagas_info[saga_id]["context"])
        else:
            # Update order state to COMPLETED and notify the main thread
            orderStatus = OrderState.query.filter_by(saga_id=saga_id).first()
            orderStatus.state = "COMPLETED"
            try:
                db.session.add(orderStatus)
                db.session.commit()
            except Exception:
                db.session.rollback()
                return abort(400, DB_ERROR_STR)
            self.pending_events[saga_id].set()
                

    def compensate(self, saga_id: str):
        orderStatus = OrderState.query.filter_by(saga_id=saga_id).first()
        if orderStatus is None:
            return abort(400, f"Order: {saga_id} not found!")
        orderStatus.state = "FAILED" 
        try:
            db.session.add(orderStatus)
            db.session.commit()
        except Exception:
            db.session.rollback()
            return abort(400, DB_ERROR_STR)
        current_step: int = self.sagas_info[saga_id]["current_step"]
        # Revert all steps
        while current_step >= 0:
            try:
              self.steps[current_step].revert(self.sagas_info[saga_id]["context"])
              current_step -= 1
            except Exception as e:
                print(f"Compensation error in {self.steps[current_step].name}: {e}")
        # Notify the main thread
        self.pending_events[saga_id].set()