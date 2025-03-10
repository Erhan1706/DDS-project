import os
import atexit
from flask import Flask
from flask_sqlalchemy import SQLAlchemy


#db: redis.Redis = redis.Redis(host=os.environ['REDIS_HOST'],
#                              port=int(os.environ['REDIS_PORT']),
#                              password=os.environ['REDIS_PASSWORD'],
#                              db=int(os.environ['REDIS_DB']))


DB_ERROR_STR = "DB error"
REQ_ERROR_STR = "Requests error"

GATEWAY_URL = os.environ['GATEWAY_URL']

db: SQLAlchemy = SQLAlchemy()

def create_app():
    app = Flask("order-service")
    
    app.config["SQLALCHEMY_DATABASE_URI"] = 'postgresql://postgres:postgres@order-postgres:5432/order-db'
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "isolation_level": "SERIALIZABLE"  # Strongest isolation level for postgres
    }
    
    db.init_app(app)
    
    def close_db_connection():
        db.session.close()
    atexit.register(close_db_connection)
    
    # Create tables
    with app.app_context():
        db.create_all()
    
    # Register blueprints
    from routes import order_bp
    app.register_blueprint(order_bp)
    
    return app