# React Native/Expo 앱 아키텍처 조사와 승인안

조사 기준일: 2026-09-27. 처음에는 앱 아키텍처 승인 요청 자료로 작성했다. 이후 사용자가 이 run의 host 세션에서 아래 제안을 `Aiproject` 로컬·팀 설치 시험에 적용하도록 승인했다. run 산출물에는 그 대화 원문이 없으므로, 승인 범위는 이 문서의 마지막 절에 기록한다.

## 실제로 가장 많이 쓰는 구조는?

**Clean/MVVM/MVI/feature-first 중 어느 앱 아키텍처가 1위인지는 확인할 수 없다.** 운영 앱을 대표하는 표본에 동일한 아키텍처 분류 기준을 적용한 통계를 찾지 못했다. 공식 템플릿은 권장 출발점이지 점유율 자료가 아니다. [Expo Router 소개](https://docs.expo.dev/router/introduction/), [Expo 앱 템플릿](https://docs.expo.dev/more/create-expo/), [RN CLI 기본 템플릿](https://github.com/react-native-community/template).

[State of React Native 2024 상태관리 설문](https://results.2024.stateofreactnative.com/en-US/state-management/?view=usage)의 **사용 경험(Used it)** 비율은 React built-ins 91.5%(응답 2,434명), Redux 71%(2,440명), Redux Toolkit 61.1%(2,421명), Zustand 46.7%(2,413명). 복수 경험·Redux/Toolkit 중복이 가능하고 '현재 앱의 선택'도 '아키텍처 채택률'도 아니다. [설문 방법](https://results.2024.stateofreactnative.com/en-US/about/)에 따르면 2024-12-09~2025-01-08 공개 자원자 3,501명, 선택 응답·비표본 모집이며 Meta/RN 공식 조사가 아니다. RN의 [New Architecture](https://reactnative.dev/architecture/overview)는 앱의 Clean/MVI가 아니라 RN 런타임 구조다.

참고 사례: [Bluesky](https://github.com/bluesky-social/social-app/blob/main/package.json), [Mattermost](https://github.com/mattermost/mattermost-mobile/blob/main/package.json), [Expensify](https://github.com/Expensify/App/blob/main/package.json), [Rocket.Chat](https://github.com/RocketChat/Rocket.Chat.ReactNative/blob/develop/package.json)는 서로 다른 데이터·상태 라이브러리를 사용한다. 의도적으로 고른 네 앱의 의존성 목록으로 앱 아키텍처나 시장점유율을 추론할 수 없다. 질문에 연결된 [Reddit 글](https://www.reddit.com/r/expo/comments/1i6r5r9/whats_the_recommended_architecture_for_a_react/)은 직접 접근이 차단되었다. [제3자 보관본](https://api.pullpush.io/reddit/search/submission/?ids=1i6r5r9)의 원문·댓글은 Android식 구조를 RN으로 가져올지 묻는 개인 의견이며, 원문 완전성·최신성과 대표성은 확인되지 않았다.

## 공식 문서가 실제로 제시하는 선택

- 새 **Expo** 앱에는 [Expo Router](https://docs.expo.dev/router/introduction/)를 권장하며 `app`/`src/app`에 파일 기반 라우트를 둔다. 다른 navigation 라이브러리도 허용한다. [Expo의 폴더 구성 글](https://expo.dev/blog/expo-app-folder-structure-best-practices)은 라우트 밖에 재사용 UI와 hooks를 두는 사례를 제시하지만 보편 도메인/데이터 계층을 강제하지 않는다.
- [React의 상태 공유](https://react.dev/learn/sharing-state-between-components)는 필요한 가장 가까운 범위에 상태를 두고, [reducer](https://react.dev/learn/extracting-state-logic-into-a-reducer)와 [context 조합](https://react.dev/learn/scaling-up-with-reducer-and-context)은 복잡한 전이 또는 넓은 공유에 선택적으로 사용한다. [Custom Hook](https://react.dev/learn/reusing-logic-with-custom-hooks)은 로직을 공유하지만 호출 간 상태 인스턴스를 자동 공유하지 않는다. 따라서 Android `ViewModel`과 일대일 수명 계약은 아니다.
- 서버 응답 캐시가 필요하면 TanStack Query 등을 선택할 수 있지만 필수는 아니다. 사용 시 [RN용 Query 가이드](https://tanstack.com/query/latest/docs/framework/react/react-native)의 `onlineManager`·`AppState` 포커스 처리를 웹과 구분한다. 네이티브 상태·폼 초안·원격 캐시를 한 곳에 복제하지 않는 것은 설계 제안이다.
- 비교 대상인 [Android 도메인 계층](https://developer.android.com/topic/architecture/domain-layer)조차 use case를 **선택 사항**이라고 규정하고 단순 전달 계층 강제의 비용을 명시한다. [Android ViewModel](https://developer.android.com/topic/libraries/architecture/viewmodel)의 수명/구성 변경 보존을 RN hook 이름만으로 얻지는 못한다. RN에서도 필요성이 검증되면 순수 함수·port·repository를 사용할 수 있으나 플랫폼 의무 구조라는 근거는 없다.

## Claude Opus 5.5 적대적 리뷰

실제 Claude CLI 2.1.283에 `--model claude-opus-5-5 --tools "" --permission-mode dontAsk --output-format json`으로 병렬 호출했다. 결과 JSON의 `modelUsage.claude-opus-5-5.canonicalModel=claude-opus-5-5`, `provider=firstParty`, `is_error=false`; Opus 5 등의 대체 모델을 사용하지 않았다. 리뷰어는 문서 링크를 입력으로 받았고 직접 웹을 다시 열지 않았으며, 현재 코드베이스의 앱 구현도 검사하지 않았다. 다음은 리뷰어의 **설계 판단**이며 공식 표준이나 사용률 통계가 아니다.

- Android식 Clean+MVI+ViewModel+UseCase+Repository를 **모든 화면에 강제**하면 단순 화면에 전달 계층이 쌓인다. [Android optional domain 지침](https://developer.android.com/topic/architecture/domain-layer)과도 맞지 않는다. [React Effect 수명](https://react.dev/reference/react/useEffect)과 [Android ViewModel 수명](https://developer.android.com/topic/libraries/architecture/viewmodel)은 같지 않으므로 구독 정리·화면 전환을 따로 증명해야 한다. Query 원격 상태와 ViewModel 복제본 불일치 위험은 [추론].
- 반대로 **경계 없는 feature 모음**은 공유 규칙, 결제·오프라인 다중 소스 조정, 앱 [background/active 전이](https://reactnative.dev/docs/appstate)의 소유자를 흐리게 한다는 반론도 제기했다. 그런 작업에는 필요한 port/순수 정책/영속화 경계가 유용하다는 판단이다. 특정 앱에 결제·오프라인 요구가 있는지는 [미확인]이다.
- 리뷰어 결론: Expo route/feature 중심과 **상태 종류별 소유권**을 기본으로 하되, 재사용 도메인 규칙·복수 데이터 소스 조정·결제처럼 실제 경계가 필요한 경우에 한해 순수 로직과 port를 검토한다. 전 화면 MVI, ViewModel, UseCase 클래스를 필수로 하지 않는다. `2곳 이상` 같은 획일적 정량 트리거는 리뷰어의 미검증 제안으로 의무 기준에서 제외한다.

## 승인된 범위와 변경 경계

**승인된 제안:** React Native/Expo는 Expo Router를 신규 앱의 기본 라우팅 선택지로 두고 기존 navigation은 유지한다. UI 지역 상태, 공유 상태, 원격 캐시, 폼 초안, 영속 상태의 소유자를 분리한다. 복잡한 상태 전이는 reducer/MVI 형태를 **선택**하고, 반복되는 비즈니스 규칙·오프라인/결제 조정이 실제로 필요하면 순수 함수·port·repository를 도입한다. Android식 ViewModel/UseCase/Repository 세트를 화면마다 강제하지 않는다.

`react-native-feature-architecture`는 설치·구현·리뷰 단계에서 검증한다. 이후 사용자 결정에 따라 React Native, React Web, Kotlin(Spring·Ktor) 백엔드는 기본 설치에서 Clean 대신 각 스택 아키텍처(`stack` 모드)를 받는다. 선언 없이 Clean 기본값으로 설치된 기존 프로젝트는 재설치할 때 전환된다. `clean`을 명시하면 Clean을 유지하고, local·team 계약도 종전처럼 설치된다. 앱 코드의 아키텍처는 바꾸지 않는다. 실제 앱 마이그레이션은 각 대상의 현행 구조와 의존성을 확인한 뒤에만 논의한다.
