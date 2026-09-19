# Java–Kotlin Server Interoperability

Read for an actual Java-to-Kotlin server conversion or a change to a Java/Kotlin JVM boundary, including the initial build-only transition before any `.kt` source exists. Ordinary Java-only maintenance, unrelated Kotlin syntax, Android/client-only work, and technologies absent from the changed path do not acquire these rules.

## Activate and bound the transition

For a build-only transition, use the active profile's source-neutral `kotlin-backend` concern or explicit Kotlin server migration task selection. State the intended server source scope; an existing Kotlin file is not a prerequisite, and a Gradle/Maven file alone is not migration evidence. This reference describes applicability; profile metadata and the resolver determine selection.

Preserve the selected `clean`, `local`, or `pending` architecture contract. Language conversion does not authorize new layers, DI, database/schema changes, or a switch from blocking code to coroutines/reactive execution. Keep unaffected Java code and existing build/runtime choices. Resolve compiler, framework, serializer, persistence provider, and build-plugin versions from the project before using version-sensitive options.

## Preserve the caller contract

- Identify remaining Java source callers, generated callers, reflective consumers, and independently deployed binaries. Preserve the compatibility promised to each; an internal API whose callers are migrated together need not promise external binary compatibility.
- Compare JVM class/package names, constructors, field versus accessor exposure, method descriptors, visibility, generic wildcards, and interface default-method mode where changed. Moving a method to a top-level function, replacing a class with an inline value class, or changing primitive/boxed types can change the Java-facing surface. Kotlin `internal` is not Java package-private visibility.
- Kotlin default arguments do not generally expose Java overloads. Retain the required explicit overloads or use supported `@JvmOverloads` where its generated signatures match existing callers. Use `@JvmStatic`, `@JvmField`, or `@JvmName` only for the static, field, or naming contract actually needed; none is a blanket migration requirement.
- Kotlin has no checked-exception enforcement, but Java callers still observe declared exceptions. Preserve required `throws` exposure with `@Throws` where applicable, along with the actual exception type and handling behavior. A declaration is not a rollback policy; inspect Spring behavior through the conditional reference below.

## Preserve null, construction, and wire meaning

- Java platform types can contain null despite compiling at a non-null Kotlin use site. Resolve nullable Java results and collection elements from their actual contract; use a nullable type or validate at the existing boundary rather than adding `!!` to silence uncertainty. Changing a formerly nullable Java parameter to Kotlin non-null can move failure to an implicit runtime check.
- Keep omitted, explicit-null, defaulted, and supplied values distinct wherever the public contract distinguishes them. A constructor default is not proof that a serializer treats missing and null identically. Use the existing presence-aware representation for PATCH; see [API and security](api-security.md) only when that boundary changes.
- Preserve constructors and metadata used by Java callers, serializers, dependency injection, and reflection. A Kotlin primary constructor need not provide the no-argument constructor a consumer expects. Explicit constructors, supported constructor binding, or a scoped compiler plugin are valid when they meet that consumer's contract; do not invent empty/default values just to satisfy construction.
- For actual serialization and Bean Validation, inspect the installed modules, mapper settings, names, creators, and annotation targets. `@field:`, `@get:`, and `@param:` reach different JVM elements; Kotlin `property` annotations are not Java-visible. Compiler-version defaults can differ. Select the target the consumer reads, rather than requiring `@field:` everywhere. Exercise invalid and nested input through the real binding/validation path, not only direct DTO construction. For Spring, reuse [Web and security](../../spring-boot-development-guide/references/web-security.md).

## Apply only present framework constraints

**ORM entities:** Preserve field/property access, identity, equality/hash-code behavior before and after persistence, and comparison with proxies. Generated data-class equality/hash codes use primary-constructor properties; a mutable generated ID or association can therefore change set/map behavior or traverse lazy state. A regular class with suitable explicit equality is valid; a data-class DTO is not an ORM defect. Match the actual ORM's constructor, mutability, and proxy/enhancement constraints. JPA requires a suitable no-argument constructor and non-final entity surface; supported compiler plugins may supply parts of that contract. The no-arg plugin's constructor is synthetic and reflection-accessible, not a replacement for a Java-callable constructor; initializer execution is configurable. No-arg generation alone does not establish proxyability. Non-JPA constructor-mapped persistence need not adopt JPA plugins.

**Spring advice and transactions:** Read [Transactions and JPA — Interception and transaction participation](../../spring-boot-development-guide/references/transactions-jpa.md#interception-and-transaction-participation) for the actual proxy path, finality, propagation, and effective rollback rules. Follow only that section when no JPA is present. Preserve the real bean call and failure outcome, including any changed exception wrapping; copying annotations or applying `kotlin-spring` is not runtime evidence. For an existing reactive transaction path, use [Reactive data](../../spring-boot-development-guide/references/reactive-data.md) instead of imposing imperative transaction semantics.

## Preserve the mixed build

Resolve existing Java/Kotlin source roots, compilation order, generated sources, packaging, toolchains, JVM targets, and runtime dependencies for both main and test code. Java compilation must see required Kotlin outputs, and Kotlin compilation must see required Java declarations. Keep the project's Gradle or Maven integration; no universal build snippet or source tree is required.

Align related Java/Kotlin bytecode targets with the supported deployment JDK, using the existing toolchain or explicit compatible settings. Bytecode target alone does not restrict use of newer JDK APIs. Preserve applicable annotation processing and generated-code consumers: Java processors on Kotlin may need kapt; existing supported KSP or Java-only processing can remain. Check processor support and task wiring rather than moving every processor or introducing a processor framework where none exists.

## Evidence proportional to the changed boundary

Use authorized project gates and existing evidence; this reference adds no mandatory suite, scaffold, or CI gate. Separate:

- **Build evidence:** the relevant mixed sources and generated code compile and package for the intended target. With no Kotlin source yet, a successful existing Java build establishes only that build-only step, not future Kotlin interoperability.
- **Caller evidence:** retained Java callers compile and execute; where external binary compatibility is promised, previously compiled consumers still link and execute. Recompiling all consumers cannot establish that binary guarantee.
- **Wire/runtime evidence:** changed inputs retain missing/null/default and validation behavior; reflection constructs the intended objects; affected ORM identity/proxy behavior and Spring commit/rollback outcomes remain correct through the real framework path.

Report the observed input, outcome, and unexecuted boundary. Concrete failures include a removed Java overload or static member, a linkage error in an old binary, null becoming an unintended 500, invalid input bypassing validation, or a partially committed write after conversion. These support findings; absence of a preferred annotation or plugin does not. Reading this reference or compiling alone is not proof of preserved runtime behavior.

## Official basis

Use documentation matching the installed versions, not the newest version in an example. The review criteria above apply those contracts to migration; they are not claims that a particular project has been exercised.

- Kotlin [calling Java](https://kotlinlang.org/docs/java-interop.html) and [calling Kotlin from Java](https://kotlinlang.org/docs/java-to-kotlin-interop.html): platform types, JVM exposure, overloads, exceptions, generics, and ABI-sensitive constructs.
- Kotlin [annotation use-site targets](https://kotlinlang.org/docs/annotations.html#annotation-use-site-targets), [data classes](https://kotlinlang.org/docs/data-classes.html), and [no-arg plugin](https://kotlinlang.org/docs/no-arg-plugin.html).
- Spring [Kotlin requirements](https://docs.spring.io/spring-framework/reference/languages/kotlin/requirements.html) and [Spring projects in Kotlin](https://docs.spring.io/spring-framework/reference/languages/kotlin/spring-projects-in.html): reflection/serialization support, finality, persistence construction, and checked-exception exposure.
- [Jakarta Persistence entity contract](https://jakarta.ee/specifications/persistence/3.2/apidocs/jakarta.persistence/jakarta/persistence/entity): consult the project's persistence version and provider for applicable constraints.
- Kotlin [Gradle configuration](https://kotlinlang.org/docs/gradle-configure-project.html), [Maven mixed compilation](https://kotlinlang.org/docs/maven-configure-project.html#compile-kotlin-and-java-sources), and [kapt](https://kotlinlang.org/docs/kapt.html): source visibility, target/toolchain settings, and processor integration.
