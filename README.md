# Distributed Data Systems Project 

## Application start instructions

The project uses docker swarm, to start the application do: 

```bash
docker swarm init
docker compose build
docker stack deploy -c docker-compose.yml mystack
docker stack rm mystack #to stop services
```

#### For Windows users:

In case you see error similar to this when starting services:
```
./wait-for-kafka.sh: line 3: $'\r': command not found
```
Make sure the corresponding files are using Unix line separator (\n).
List of files:
- wait-for-services.sh
- order/wait-for-kafka.sh
- payment/wait-for-kafka.sh
- stock/wait-for-kafka.sh
- pgpool/init-order/wait-for-dbs.sh
- pgpool/init-payment/wait-for-dbs.sh
- pgpool/init-stock/wait-for-dbs.sh


**Note:** ocasionally in some operating systems (e.g. Linux), docker swarm sometimes does not start properly, giving name resolution errors. Sometimes, just retrying again fixes this, but if there's consistent issues with the containers, or the consistency tests are not initially passing, please use the **compose-version** branch. This branch only uses docker compose, however it has more limited functionality in terms of replication of the payment and stock services compared to this version.

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

- Replication + high-availability of stock-service and payment-service. Stopping these containers will not stop the execution of the orders as the requests will be redirected to the replica. 

**Stopping any other container will most likely break the application. This includes: kafka, zookeper and redis.**