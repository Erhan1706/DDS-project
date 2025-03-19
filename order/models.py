from sqlalchemy.dialects.postgresql import UUID, JSONB
from __init__ import db
import uuid
from msgspec import Struct
from sqlalchemy.ext.mutable import MutableDict

class Order(db.Model):
    __tablename__ = "orders"
    
    id = db.Column(db.String(), primary_key=True,default = lambda: str(uuid.uuid4()))
    user_id = db.Column(db.String(50), nullable=False)
    paid = db.Column(db.Boolean, default=False, nullable=False)
    items = db.Column(MutableDict.as_mutable(JSONB), default=dict)  # Store items as JSON array
    total_cost = db.Column(db.Integer, default=0, nullable=False)

    def to_dict(self):
        return {
            "order_id": str(self.id),
            "paid": self.paid,
            "items": self.items,
            "user_id": self.user_id,
            "total_cost": self.total_cost
        }
    
class OrderState(db.Model):
    __tablename__ = "order_states"

    id = db.Column(db.String(), primary_key=True, default = lambda: str(uuid.uuid4()))
    saga_id = db.Column(db.String(), nullable=False, default = lambda: str(uuid.uuid4()))
    order_id = db.Column(db.String, db.ForeignKey('orders.id'), nullable=False)
    state = db.Column(db.String(50), nullable=False)

# Old models
class OrderValue(Struct):
    paid: bool
    items: list[tuple[str, int]]
    user_id: str
    total_cost: int
