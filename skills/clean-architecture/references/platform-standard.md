# Platform Clean Architecture Standard

This reference extends the `clean-architecture` skill with a platform-neutral
standard for Android, iOS, React, React Native, Python, and comparable stacks.

## Adoption Policy

- The Android/Samantha structure is the canonical source for module family,
  package/folder shape, and boundary naming.
- Android, iOS, React, React Native, and Python should mirror the same semantic
  architecture. The same semantic architecture means app shell, feature API, feature presentation, core UI/design
  system/resources/platform/network/navigation, core domain, and core data.
- Platform mapping changes names and build/package mechanics only. It must not
  change ownership, dependency direction, or layer boundaries.
- DI may use each platform's normal library or framework. If there is no
  Hilt-equivalent standard, create an equivalent app-shell composition root plus
  feature/data factories, providers, or container modules.
- Do not ask downstream agents to re-validate sources for this standard. Treat
  this file as the checked project standard and only flag concrete contradictions
  in an implementation.

## Target Architecture

```text
App Shell
  -> Feature API
  -> Feature Presentation
  -> Core UI / Design System / Resources / Platform
  -> Core Domain
  -> Core Data

Feature Presentation
  -> its own Feature API
  -> Core Domain use cases and domain models
  -> Core UI / Design System / Resources / Platform interfaces

Feature API
  -> route, destination, entry, or public feature contracts only

Core Data
  -> Core Domain contracts and models
  -> network, persistence, platform adapters, cache, mappers

Core Domain
  -> no data implementation
  -> no UI framework
  -> no platform or transport framework
```

Dependency direction is inward toward domain contracts. UI and app-shell layers
can depend on domain. Domain must not depend on UI, data, app shell, navigation
implementation, HTTP clients, databases, ORMs, or platform view frameworks.

## Module Families

### App Shell

- Owns process/app startup.
- Owns the root dependency graph or composition root.
- Wires feature modules, data implementations, app-level navigation/routing,
  global dialogs, global error hosts, analytics, deep links, scheduled jobs, and
  session/process-level side effects.
- May depend on feature API/presentation and core domain/data modules.
- Must not contain feature business rules.

Platform examples:

| Platform | Suggested module |
|---|---|
| Android | `:app` |
| iOS | `App` target plus `AppShell` source folder |
| React | `apps/web` |
| React Native | `apps/mobile-rn` |
| Python | `apps/api`, `apps/worker`, `apps/cli`, or `src/<project>/app` |

### Feature API

- Defines the public contract of one feature.
- Exposes route keys, destination types, deep-link contracts, entry contracts,
  and feature-level public interfaces.
- Allows app shell and approved callers to refer to a feature without depending
  on its presentation internals.
- Must not contain screens, views, controllers, reducers, UI state/events, DTOs,
  repositories, data sources, or business logic.

Platform examples:

| Platform | Example path |
|---|---|
| Android | `feature/<name>/api/.../feature/<name>/api/navigation` |
| iOS | `Sources/Feature<Name>API/Navigation` |
| React | `packages/feature-<name>-api/src/navigation` |
| React Native | `packages/feature-<name>-api/src/navigation` |
| Python | `src/<project>/features/<name>/api` |

### Feature Presentation

- Owns route/container, screen/view, ViewModel/controller/reducer/handler, UI
  state, UI actions, UI events, UI display models, response models, and
  domain-to-output mappers.
- Depends on domain use cases/contracts, not data implementations.
- Must not import DTOs, database/ORM entities, API clients, repository
  implementations, SQL rows, or transport response DTOs directly.
- Screens/views/adapters render primitives and presentation models, not raw
  domain entities.
- Route/container/controller owns state collection, side-effect collection, and
  navigation/routing callback wiring.

Python presentation means API routers/controllers, CLI command handlers, worker
handlers, request/response schemas, presenters, and serializers at the adapter
edge. It calls use cases through injected ports and must not import concrete data
clients, ORM models, or repository implementations.

Platform examples:

| Platform | Example path |
|---|---|
| Android | `feature/<name>/presentation/.../feature/<name>/presentation` |
| iOS | `Sources/Feature<Name>Presentation` |
| React | `packages/feature-<name>-presentation-web/src` |
| React Native | `packages/feature-<name>-presentation-rn/src` |
| Python | `src/<project>/features/<name>/presentation` |

### Core Domain

- Owns business language and business rules for one bounded context.
- Defines domain models, value objects, repository interfaces, use cases, domain
  services, policies, and domain errors.
- Repository interfaces live in domain.
- Use cases depend on repository interfaces, not concrete repositories.
- Domain errors are explicit when presentation behavior depends on them.

Platform examples:

| Platform | Example path |
|---|---|
| Android | `core/domain/<context>/.../core/domain/<context>` |
| iOS | `Sources/CoreDomain<Context>` |
| React | `packages/core-domain-<context>/src` |
| React Native | shared TypeScript package with React when possible |
| Python | `src/<project>/core/domain/<context>` |

### Core Data

- Implements domain repository interfaces.
- Owns transport DTOs, database/ORM entities, remote/local data sources,
  persistence, API clients, data mappers, cache policy, and data DI bindings.
- Data depends on domain, never the reverse.
- Repository implementations return domain models, never DTOs or entities.
- Repository implementations translate infrastructure failures into domain-level
  failures.
- Cache is a data-layer detail. Split memory cache and disk cache when lifetime
  or change reason differs.

Platform examples:

| Platform | Example path |
|---|---|
| Android | `core/data/<context>/.../core/data/<context>` |
| iOS | `Sources/CoreData<Context>` |
| React | `packages/core-data-<context>/src` |
| React Native | shared TypeScript package when API/storage adapters match |
| Python | `src/<project>/core/data/<context>` |

### Core UI, Core Design System, Resources, Platform, Network, Navigation/Routing

- Core UI owns shared non-feature UI helpers. It may expose UI framework APIs
  because it is explicitly a UI layer.
- Core design system owns tokens, typography, colors, spacing, icons, primitive
  components, themes, and component variants.
- Core resources own shared strings, assets, localized resources, fonts, and
  resource access helpers.
- Core platform wraps platform services behind stable interfaces.
- Core network owns common HTTP/RPC clients, request metadata, serialization
  setup, and raw infrastructure failure classification.
- Navigation/routing API defines platform-neutral route keys, destination
  contracts, and graph interfaces. Implementation adapts those contracts to the
  platform framework.

Platform examples:

| Platform | UI | Platform | Network | Navigation/Routing |
|---|---|---|---|---|
| Android | `:core:ui` | `:core:platform:<adapter>` | `:core:network` | `:core:navigation:{api,impl}` |
| iOS | `CoreUI` | `CorePlatform<Adapter>` | `CoreNetwork` | `CoreNavigationAPI`, `CoreNavigationImpl` |
| React | `packages/core-ui-web` | `packages/core-platform-web` | `packages/core-network` | `packages/core-navigation-api`, `packages/core-navigation-react` |
| React Native | `packages/core-ui-rn` | `packages/core-platform-rn` | `packages/core-network` | `packages/core-navigation-api`, `packages/core-navigation-rn` |
| Python | `core/presentation` when needed | `core/platform/<adapter>` | `core/network` | `core/routing/{api,impl}` |

Design system examples:

| Platform | Design system |
|---|---|
| Android | `:core:designsystem` |
| iOS | `CoreDesignSystem` |
| React | `packages/core-design-system-web` |
| React Native | `packages/core-design-system-rn` |
| Python | only when reusable CLI/API presentation conventions exist |

## API Location Rules

Feature API allowed:

- Route/destination identifiers.
- Serializable navigation/routing arguments.
- Feature entry contracts.
- Minimal callbacks needed by app shell.

Feature API forbidden:

- UI state/action/event.
- Screens/views/components.
- ViewModels/controllers/reducers/handlers.
- Domain use case implementations.
- Repository interfaces or implementations.
- DTOs/entities/API clients.

Domain API allowed:

- Domain models and value objects.
- Repository interfaces.
- Use cases.
- Domain services/policies.
- Domain errors.

Domain API forbidden:

- UI/response models.
- DTOs/entities.
- Retrofit/OkHttp/URLSession/fetch/`requests`/`httpx` client types.
- SQL/ORM row types.
- App shell navigation/routing types.
- React/SwiftUI/Compose/React Native components.
- FastAPI/Flask/Django route, request, response, dependency, or ORM types.

Data API allowed:

- Remote service interfaces.
- DTOs/request/response models.
- Local entities.
- Data source interfaces and implementations.
- Repository implementations.
- Data-to-domain mappers.
- Data DI bindings.

Visibility:

- Public only when required by DI or tests.
- Internal/package-private by default.
- Never exported to feature presentation.

Presentation API allowed:

- Route/container.
- Screen/view.
- ViewModel/controller/reducer/handler.
- UI state/action/event.
- UI display models and response models.
- UI/response mappers.
- Feature-local components/helpers.

Presentation API forbidden:

- Remote DTO rendering.
- Entity/ORM rendering.
- Direct API client calls.
- Direct repository implementation calls.
- Global app-shell navigation/routing mutation.

## Naming Rules

Module names:

```text
app
core-ui
core-design-system
core-resources
core-platform
core-platform-<adapter>
core-network
core-navigation-api / core-routing-api
core-navigation-impl / core-routing-impl
core-domain-<context>
core-data-<context>
feature-<name>-api
feature-<name>-presentation
```

Folder names:

```text
api
model
mapper
repository
usecase
service
source
remote
local
cache
di
navigation
routing
route
screen
view
viewmodel
controller
handler
component
error
resource
platform
```

Recommended suffixes:

| Type | Suffix |
|---|---|
| UI display model | `UiModel` |
| UI state/action/event | `UiState`, `UiAction`, `UiEvent` |
| DTO/request/response | `Dto`, `Request`, `Response` |
| Local persistence entity | `Entity` |
| Repository interface | `Repository` |
| Repository implementation | `<Context>RepositoryImpl` |
| Use case | `<Verb><Noun>UseCase` |
| Mapper | `<Source><Target>Mapper` or local conversion function |
| Route contract | `<Feature>Route`, `<Feature>Destination`, or `<Feature>NavKey` |
| Python port | `<Context>Repository` or `<Verb><Noun>UseCase` as `typing.Protocol` when a public port is needed |

## Error Handling Standard

- Core network classifies raw network failures.
- Core data maps infrastructure failures into domain-level errors.
- Core domain defines app/domain errors that presentation can reason about.
- Feature presentation turns local recoverable errors into UI state or response
  models.
- App shell handles global errors such as session expiration, maintenance,
  forbidden access, forced update, global outage, account state changes, process
  startup failures, and worker/queue fatal failures.
- Every presentation-layer call to async domain logic must route failures
  through the common error notifier or equivalent global error boundary.
- Do not catch a domain failure and convert it only to local UI state when a
  global boundary must also react.
- Do not drop a domain failure to `null`, `Idle`, or no-op without documented
  rationale.

Python adapter examples:

- Route/controller/handler `try/except` maps domain errors to response/presenter
  models and notifies the common error boundary when the failure is global.
- Async generators, queue consumers, scheduled jobs, and worker tasks expose an
  error channel or handler that reaches the app shell boundary.

## Data Mapping Standard

```text
Remote DTO -> Data mapper -> Domain model -> Presentation mapper -> UiModel/Response -> Adapter
Local Entity -> Data mapper -> Domain model -> Presentation mapper -> UiModel/Response -> Adapter
```

- DTOs and entities are data-layer details.
- Domain models are business-facing and stable.
- UI/response/presenter models are display-facing and may include formatted text,
  enabled flags, selected states, grouping, pagination cursors, or transport
  response shape.
- Do not reuse DTOs as domain models.
- Do not reuse domain models as UI state/response models when the adapter needs
  formatted or transport-specific fields.
- Mapper must not perform API calls, DB/cache access, or business policy
  decisions.

## Dependency Injection Rules

- App shell is the composition root.
- Data modules bind repository implementations to domain repository interfaces.
- Feature presentation receives use cases and platform/UI helpers through
  constructor injection or equivalent.
- Domain use cases receive repository interfaces.
- Domain logic must be testable without app shell or platform runtime.
- Avoid hidden service locators in domain, use cases, repositories, controllers,
  ViewModels, reducers, and handlers.
- Framework dependency hooks belong at app shell or adapter edge, not inside
  domain logic.
- DI shape is shared across platforms even when the concrete tool differs:
  app-level graph, feature presentation factories/providers, data repository
  bindings, and platform adapter bindings.

Preferred style by platform:

- Android: Hilt/Dagger modules in `di/`; assisted factories for route args.
- iOS: Swift protocols plus app-shell factories, or a project DI container when
  the app already uses one.
- React: explicit providers/factories or a project DI container; avoid hidden
  imports of concrete data clients.
- React Native: same as React; native modules stay behind platform adapters.
- Python: constructor injection with `Protocol` ports; app shell factory,
  container, or framework adapter wiring binds concrete repositories. FastAPI
  `Depends`, Flask app context, Django wiring, or CLI framework DI stays at the
  adapter edge.
- If a platform has no chosen DI library, create local `di` modules/factories
  that mirror the Android/Hilt binding responsibilities instead of removing DI
  boundaries.

## State and UDF Rules

- UI/presentation state is immutable.
- User input is represented as actions or commands.
- One-shot effects are represented separately from state.
- Route/container connects navigation/routing, state collection, and effects.
- Screen/view/response adapter renders only props/state/output models and
  callbacks.
- ViewModel/controller/reducer/handler maps domain outputs to presentation
  state/output.
- UI/response mappers own display and transport formatting.

Recommended flow:

```text
Screen/View/Route/CLI/Worker -> UiAction/Command -> ViewModel/Controller/Handler
  -> UseCase -> Repository Interface
Repository Impl -> Data Source -> DTO/Entity -> Mapper -> Domain Model
Domain Model -> Presentation Mapper -> UiState/UiModel/Response -> Adapter
```

For Python, map incoming HTTP requests, CLI arguments, queue messages, or
scheduled jobs to an action/command object before invoking a controller/handler
or use case. Response schemas and presenter models are output models; background
jobs and notifications are effects, not domain state.

## Testing Rules

- Domain use case tests with fake repository interfaces.
- Data mapper tests for DTO/entity to domain conversion.
- Repository implementation tests with fake remote/local sources.
- Presentation state tests for ViewModel/controller/reducer/handler behavior.
- Navigation/routing contract tests for route args and deep links.
- App-shell tests for global error handling, root navigation/routing, startup,
  and worker process boundaries.
- Dependency-boundary checks that forbid presentation-to-data imports and
  domain-to-platform imports.
- Cache policy tests when memory cache, disk cache, restart persistence, or
  invalidation rules exist.
- Python adapter tests should cover request/response schema mapping, framework
  dependency wiring, missing input, malformed input, and non-zero CLI exits when
  relevant.

## Platform Repository Layout Templates

Android:

```text
app/
core/{ui,designsystem,resources,platform,network,navigation/api,navigation/impl,domain/<context>,data/<context>}
feature/<name>/{api,presentation}
```

iOS:

```text
App/
Sources/{CoreUI,CoreDesignSystem,CoreResources,CorePlatform,CoreNetwork,CoreNavigationAPI,CoreNavigationImpl,CoreDomain<Context>,CoreData<Context>,Feature<Name>API,Feature<Name>Presentation}
```

React:

```text
apps/web/
packages/{core-ui-web,core-design-system-web,core-resources,core-platform-web,core-network,core-navigation-api,core-navigation-react,core-domain-<context>,core-data-<context>,feature-<name>-api,feature-<name>-presentation-web}
```

React Native:

```text
apps/mobile-rn/
packages/{core-ui-rn,core-design-system-rn,core-resources,core-platform-rn,core-network,core-navigation-api,core-navigation-rn,core-domain-<context>,core-data-<context>,feature-<name>-api,feature-<name>-presentation-rn}
```

Python:

```text
apps/{api,worker,cli}/
src/<project>/
  app/
  core/
    domain/<context>/
    data/<context>/
    design_system/
    network/
    platform/<adapter>/
    resources/
    routing/{api,impl}/
    presentation/
  features/<name>/{api,presentation}/
tests/{unit,integration,architecture}/
```

Python packages may collapse folders for small services, but the import boundary
must stay explicit.

## New Feature Checklist

For a new feature named `<name>`:

1. Create `feature-<name>-api`.
2. Put only route/destination/entry contracts in API.
3. Create `feature-<name>-presentation`.
4. Add route/container, screen/view/adapter, ViewModel/controller/reducer/handler,
   model, mapper, and feature-local component/helper.
5. Add or reuse `core-domain-<context>` use cases and repository interfaces.
6. Add or reuse `core-data-<context>` repository implementations and data sources.
7. Wire concrete implementations only in app shell or data DI.
8. Add dependency-boundary tests or lint/import rules.
9. Add domain tests, data mapper tests, and presentation state/adapter tests.
10. Verify presentation imports no data implementation, DTO, DB/ORM entity, or
    transport client type.

## Boundary Smell List

Request changes when any of these appears:

- Feature screen/view/route/handler imports `core.data`, DTO, entity, API service,
  HTTP/database client, or repository implementation.
- Domain imports UI frameworks, platform context/view objects, transport clients,
  route/dependency framework types, SQL rows, ORM models, or ORM entities.
- Feature API exposes UI state, response models, screen classes, controllers, or
  handlers.
- Data module imports feature or app-shell code.
- App shell owns feature business rules.
- Global session/process errors are handled only inside a feature.
- UI/response adapter displays DTO/entity/ORM fields directly.
- New feature is added without an API module or public entry contract.
- New data source returns DTOs to presentation.
- Mapper contains business policy that belongs in domain.
