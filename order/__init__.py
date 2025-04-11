import os
import atexit

import redis
from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from redis.client import PubSub


#db: redis.Redis = redis.Redis(host=os.environ['REDIS_HOST'],
#                              port=int(os.environ['REDIS_PORT']),
#                              password=os.environ['REDIS_PASSWORD'],
#                              db=int(os.environ['REDIS_DB']))


DB_ERROR_STR = "DB error"
REQ_ERROR_STR = "Requests error"

GATEWAY_URL = os.environ['GATEWAY_URL']

db: SQLAlchemy = SQLAlchemy()

redis_pool = redis.ConnectionPool(host=os.environ['REDIS_HOST'], port=int(os.environ['REDIS_PORT']), password =os.environ['REDIS_PASSWORD'], db=int(os.environ['REDIS_DB']), decode_responses=True)
redis_db: redis.Redis = redis.Redis(connection_pool=redis_pool)
pubsub: PubSub = redis_db.pubsub()

def create_app():
    app = Flask("order-service")
    
    app.config["SQLALCHEMY_DATABASE_URI"] = 'postgresql://postgres:postgres@order-pgpool:5432/order-db'
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "isolation_level": "SERIALIZABLE",  # Strongest isolation level for postgres
        "pool_pre_ping": True
    }

    db.init_app(app)
    # Create tables
    #with app.app_context():
    #    db.create_all()

    @app.teardown_appcontext
    def close_db_connection(exception=None):
        db.session.remove()
        pubsub.close()
        redis_db.close()

    
    # Register blueprints
    from routes import order_bp
    app.register_blueprint(order_bp)
    
    return app