import os
import redis
from sqlalchemy.orm import declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import AsyncAdaptedQueuePool, QueuePool

DB_ERROR_STR = "DB error"
REQ_ERROR_STR = "Requests error"

Base = declarative_base()

engine = create_async_engine(
    'postgresql+asyncpg://postgres:postgres@order-postgres:5432/order-db',
    isolation_level="SERIALIZABLE",
    pool_size=15, 
    max_overflow=0,
    pool_timeout=30,
)
db_session = async_sessionmaker(engine, expire_on_commit=True)

# Redis
redis_pool = redis.ConnectionPool(
    host=os.environ['REDIS_HOST'],
    port=int(os.environ['REDIS_PORT']),
    password=os.environ['REDIS_PASSWORD'],
    db=int(os.environ['REDIS_DB']),
    decode_responses=True
)

redis_db = redis.Redis(connection_pool=redis_pool)
pubsub = redis_db.pubsub()