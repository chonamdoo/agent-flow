# Review Angle: N+1 Queries (Spring / JPA)

Check query fan-out, lazy loading, batching, pagination, and data-access
hot paths.

## What to verify

1. **JPA fetch strategy**
   - `@OneToMany` / `@ManyToMany` default to LAZY; `@ManyToOne` /
     `@OneToOne` default to EAGER. Endpoints that serialize the parent +
     children must eagerly fetch via `JOIN FETCH` or entity graph; otherwise
     each child triggers a separate query.
   - `@EntityGraph` declared on repository method or `@NamedEntityGraph`.

2. **Repository methods returning collections**
   - `findAll()` over a large table without pagination is a smell — use
     `Pageable` or a streaming `Slice`.
   - Methods that traverse associations (`order.getItems()` then
     `item.getProduct()`) in a loop generate N+1; load with a single
     join query.

3. **Native queries / Spring Data**
   - Custom `@Query` joins the necessary associations explicitly.
   - `@Query(countQuery=...)` provided for paginated results so the count
     doesn't hit the join.

4. **DTO projection**
   - Read-only endpoints project directly to a DTO via constructor
     expression or interface projection, not load full entities.

5. **Batch operations**
   - `saveAll` / `deleteAllInBatch` used for bulk writes (vs per-row `save`).
   - `hibernate.jdbc.batch_size` and `order_inserts` tuned where relevant.

6. **Verification**
   - Logs include the actual SQL count (`spring.jpa.show-sql` or `p6spy`).
   - Integration test asserts query count for the hot path.

## Output format

```text
## N+1 review findings

verdict: approve | request-changes

### Must-fix
- <severity:high> [path:line] <statement>. Expected 1 query, observed N.

### Should-fix
- <severity:med> ...

### Notes
- <severity:low> ...
```

Cite paths as `path/to/file:line`. Keep total under 150 lines.
