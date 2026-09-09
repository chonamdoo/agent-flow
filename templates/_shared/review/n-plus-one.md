# Review Angle: N+1 Queries (Spring / JPA)

Check query fan-out, lazy loading, batching, pagination, and data-access
hot paths.

## What to verify

1. **JPA fetch strategy**
   - JPA to-many associations default to LAZY and to-one associations to EAGER, but these defaults do not establish the actual query count or join shape. Trace traversal, serialization, session lifetime, and generated SQL.
   - Choose fetch joins, entity graphs, batching, projections, or bounded secondary queries for the actual data need. Collection fetch joins with pagination can change cardinality or cause in-memory paging; a healthy page query plus bounded association query is valid.

2. **Repository methods returning collections**
   - Bound large result sets with pagination or the actual streaming/cursor API and its resource lifetime. `Slice` is a pagination result without a total-count contract, not a streaming API.
   - Association traversal in a loop is a fan-out risk, not proof of N+1 without considering fetch strategy, batching, and cache. Preserve page/order semantics rather than forcing one join.

3. **Native queries / Spring Data**
   - Check actual generated/native SQL and requested associations. A join is not mandatory if a bounded alternative satisfies the contract.
   - Require an explicit `countQuery` when the requested total-count contract or generated count SQL needs it; preserve distinct/cardinality semantics rather than adding one to every paginated method.

4. **DTO projection**
   - Use projections when they reduce unnecessary data/materialization. Read-only entity loading is valid when it fits the consumer and measured cost; DTO projection is not mandatory.

5. **Batch operations**
   - `saveAll` does not guarantee JDBC batching; check provider settings, statement shape, identifier generation, and flush behavior where relevant.
   - Bulk DML/deletes can bypass entity callbacks/cascades/version checks and leave the persistence context stale. Preserve these semantics before replacing per-entity writes; lifecycle-dependent deletion can legitimately remain per-entity.

6. **Verification**
   - For an observed performance finding, cite actual SQL/count/cardinality and representative input size from authorized evidence. Without execution, label the specific static fan-out risk; never invent an observed count.
   - Use active authorized gates and existing query evidence. Do not impose a new test or logging library, a universal one-query target, or production SQL logging that exposes secrets.

## Output format

```text
## N+1 review findings

verdict: approve | request-changes

### Must-fix
- <severity:high> [path:line] <trigger and query/cardinality evidence, or explicitly labeled static risk>. Impact: <latency/resource/contract consequence>.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...
```

Cite paths as `path/to/file:line`. Keep total under 150 lines.

Emit exactly one unfenced final verdict line: `verdict: approve` or `verdict: request-changes`.
