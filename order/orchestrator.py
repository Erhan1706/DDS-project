from typing import List
import asyncio
from kafka import KafkaProducer

class Step():
    def __init__(self, name, action, compensation):
        self.name = name
        self.action = action
        self.compensation = compensation

    def run(self, kwargs):
        self.action(**kwargs)

    def revert(self, **kwargs):
        self.compensation(kwargs)


class Orchestrator():
    current_step: int = 0

    def __init__(self, producer: KafkaProducer, steps: List[Step] = []):
        self.steps: List[Step] = steps
        self.producer: KafkaProducer = producer
    
    def add_step(self, step_name, action, compensation):
        step = Step(step_name, action, compensation)
        self.steps.append(step)

    def get_next_step(self, step: int| None = None) -> int | None:
        if not step:
            return 0
        elif step == len(self.steps) - 1:
            return None
        else: 
            return step + 1
            
    def start(self, order_id, items, saga_id):
        self.current_step = self.get_next_step()
        self.steps[self.current_step].run(dict(order_id=order_id, items=items, saga_id=saga_id))

    def process_step(self, topic: str, saga_id: str, **kwargs):
        if topic == "stock_details_success":
            self.current_step = self.get_next_step(self.current_step)
            if self.current_step:
                self.steps[self.current_step].run(kwargs)
            else:
                print("Saga completed")
        elif topic == "stock_details_failure":
            self.compensate()
            print("Saga failed")
        else:
            print("Unknown topic")

    def compensate(self):
        while self.current_step >= 0:
            try:
              self.steps[self.current_step].revert()
              self.current_step -= 1
            except Exception as e:
                print(f"Compensation error in {self.steps[self.current_step].name}: {e}")
