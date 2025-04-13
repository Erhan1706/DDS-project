# Web-scale Data Management Project Template
Technologies used:

- Flask
- Redis
- PostgresSQL
- Kafka
- postgres-repmgr & pgPool

## Architecture

### Transaction Protocol
The system uses orchestration-based SAGAs pattern to handle distributed transactions. The SAGA implementation is custom-coded (it can be found [here](order/orchestrator.py)), it does not rely on external libraries. No external orchestrator service is used, instead the order service doubles as the SAGA orchestrator, eliminating the need for a separate service and reducing communication overhead. The communication with the client stays synchronous. The initial service (order-service) starts the SAGAs protocol and waits for it to complete before returning the result back to user.

We use kafka as a message broker. Kafka makes sure that no events are lost during communication between microservices and helps avoiding potential inconsistencies. The communication between the microservices is event-driven, which shows performance benefits compared to using REST APIs.

### Database

The system is based on PostgreSQL. All services use the ['SERIALIZABLE' isolation level](https://www.postgresql.org/docs/current/transaction-iso.html), the strictest isolation transaction level in Postgres. This ensures strong consistency, preventing stock or payment from being lost. Postgres implements this using [Serializable Snapshot Isolation](https://wiki.postgresql.org/wiki/SSI). In this algorithm no locking is applied, instead it monitors read/write dependencies among transactions, in case it detects a conflict it raises an error like:
```
ERROR:  could not serialize access due to read/write dependencies among transactions
DETAIL:  Cancelled on identification as a pivot, during commit attempt.
HINT:  The transaction might succeed if retried.
```  
To handle this our application logic captures this exception in the relevant transactions, and retries them up to a configured `MAX_RETRY` limit. Our aim was to improve performance by avoiding blocking operations such as locking. However, it is worth mentioning that this strategy might be less efficient in very high concurrency scenarios where a lot of transactions touch the same data, as in the worst case, most transaction will have to eventually retry multiple times.

Additionally, during checkouts, some local state must be maintained for coordinating SAGA transactions. However, with multiple workers, there is no guarantees that the same worker which handled the initial checkout request is the one to receive the last saga event. To counteract this we also use Redis as a fast in-memory database to store this local state and make it so that its shared between every worker. Furthermore, to ensure the communication with the client stays synchronous and the original worker can still return the final result to the client, we use [redis pubsub](https://redis.io/docs/latest/develop/interact/pubsub/), where the initial worker subscribes a channel tied to the order ID, and any worker that completes the SAGA publishes to this channel.

#### System Design
The diagram below illustrates the message flow between services using Kafka topics:

TODO: insert diagram

  
## Replication
#### Database Replication
For PostgresSQL replication we use [pgpool](https://www.pgpool.net/mediawiki/index.php/Main_Page) and [repmgr](https://www.repmgr.org/). These tools allow for replication management and automatic failover of Postgres databases. The systes uses master-slave replication setup with synchronous replication between the replicas via write-ahead-logs.

- Pgpool will also detect any database failures and perform failover, achieving high-availability in case any db container is killed.  Killing any database container will not stop the execution of the orders. Note: this failover usually takes a bit to execute (around 10-50 seconds). 

#### Services Replication
- Replication + high-availability of order-service. Order service is replicated and killing one of the containers, does not stop the execution of the orders as the requests will be redirected to the replica. Our implementation also maintains consistency in case of failure. 


