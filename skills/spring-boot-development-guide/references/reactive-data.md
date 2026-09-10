# Spring Reactive Data

Read only for WebFlux/R2DBC or coroutine/reactive transaction changes. Identify Framework/Data/Reactor/coroutine versions, reactive driver, transaction manager, and actual subscription path.

- ReactiveTransactionManager transactions use Reactor context; PlatformTransactionManager transactions are commonly thread-bound. Return type and manager must fit the transaction API. Arbitrary scheduler/dispatcher changes, detached subscriptions, or launching children do not establish transaction participation.
- Use the installed stack's supported TransactionalOperator and coroutine bridge when a suspending transaction is required. Keep participating operations in the same supported context. `suspend` on a JDBC/JPA method is not R2DBC and does not make database work nonblocking.
- Never block an event-loop thread with JDBC, filesystem, or blocking SDK work. If a blocking integration is required, isolate the entire coherent operation on bounded execution resources and account for its pool/timeouts; do not claim this turns it into a reactive transaction.
- Avoid unowned `subscribe()`/launch that detaches effect completion from the caller and transaction. Model cancellation and partial consumption: cancelling a Publisher may terminate a reactive transaction, but remote effects already attempted still require their own reconciliation. Confirm the installed operator's cancellation semantics before judging rollback.
- A stream's lifetime can also be a transaction/connection lifetime. Bound it, account for backpressure and client disconnect, and avoid presenting a partially consumed stream as a committed batch result.
- Transaction-bound reactive events are supported from Spring 6.1; the transaction context must be carried as the event source using supported publication facilities. This remains phase coupling, not durable publication.

Observe subscription, cancellation, transaction completion, and persistent effect state at the actual reactive boundary when authorized. An imperative test or a successful schema response is not proof of reactive context propagation. Keep a healthy imperative application imperative.

## Official sources

- [Spring coroutine support](https://docs.spring.io/spring-framework/reference/languages/kotlin/coroutines.html).
- [Programmatic transactions and cancel signals](https://docs.spring.io/spring-framework/reference/data-access/transaction/programmatic.html).
- [Transactional context](https://docs.spring.io/spring-framework/docs/current/javadoc-api/org/springframework/transaction/annotation/Transactional.html).
- [Transaction-bound events](https://docs.spring.io/spring-framework/reference/data-access/transaction/event.html).
