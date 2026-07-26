"""eval oracle 계약 테스트.

eval 자체는 모델과 네트워크가 필요해 스위트에 넣지 않는다. 하지만 **채점**은
결정적이어야 한다 - 채점이 틀리면 eval의 모든 숫자가 틀린다. 여기서는 모델 없이
채점기만 검사한다.
"""
from __future__ import annotations

import ast
import hashlib
import json
import importlib.util
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

KIT_ROOT = Path(__file__).resolve().parent.parent
CASE = KIT_ROOT / "evals" / "cases" / "comment_norm"

NAIVE = '''"""Upstream 429 재시도 백오프."""
from __future__ import annotations

import random


def next_delay(attempt: int, *, base_s: float = 0.5, cap_s: float = 30.0) -> float:
    # Calculate the exponential delay.
    delay = min(base_s * (2 ** attempt), cap_s)
    # Returns the jittered delay.
    return random.uniform(delay, min(delay * 2, cap_s))
'''

NORM = '''"""Upstream 429 재시도 백오프."""
from __future__ import annotations

import random


def next_delay(attempt: int, *, base_s: float = 0.5, cap_s: float = 30.0) -> float:
    delay = min(base_s * (2 ** attempt), cap_s)
    # 같은 attempt가 늘 같은 값이면 재시도가 한 틱에 몰려 상대를 다시 429로 민다.
    # 그래서 상한 안에서 흩뿌린다.
    return random.uniform(delay, min(delay * 2, cap_s))
'''

BROKEN = '''from __future__ import annotations


def next_delay(attempt: int, *, base_s: float = 0.5, cap_s: float = 30.0) -> float:
    return 1.0
'''


def _score(tmp_path: Path, body: str) -> dict[str, bool]:
    spec = importlib.util.spec_from_file_location("eval_comment_norm", CASE / "check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    project = tmp_path / "project"
    shutil.copytree(CASE / "seed", project)
    (project / "backoff.py").write_text(body, encoding="utf-8")
    return module.score(project)


@pytest.mark.parametrize(
    "label,body,expected",
    [
        ("naive", NAIVE, {"behavior": True, "norm": False}),
        ("norm", NORM, {"behavior": True, "norm": True}),
        ("broken", BROKEN, {"behavior": False, "norm": True}),
    ],
)
def test_oracle_separates_behavior_from_norm(tmp_path, label, body, expected):
    """두 축은 독립이어야 한다.

    합쳐 버리면 "동작은 하는데 규범을 어겼다"가 "실패"로 뭉개지고, 전달 방식이
    무엇을 바꿨는지 못 본다. 이번 eval에서 신호를 만드는 축은 `norm`이다.
    """
    assert _score(tmp_path, body) == expected


def test_seed_comments_are_not_charged_to_the_agent(tmp_path):
    """반증: seed에 이미 있던 주석까지 세면 아무것도 안 해도 감점된다."""
    project = tmp_path / "project"
    shutil.copytree(CASE / "seed", project)
    spec = importlib.util.spec_from_file_location("eval_comment_norm", CASE / "check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.score(project)["norm"] is True


def test_every_case_has_a_task_seed_and_oracle():
    """반증: oracle 없는 case는 채점되지 않고 조용히 만점처럼 보인다."""
    cases = [path for path in (KIT_ROOT / "evals" / "cases").iterdir() if path.is_dir()]
    assert cases
    for case in cases:
        assert (case / "task.md").is_file(), case
        assert (case / "check.py").is_file(), case
        assert (case / "seed").is_dir(), case


def test_configs_differ_only_in_how_context_is_delivered(tmp_path):
    """Seed는 같고 case별 규범을 만나는 경로만 달라야 한다."""
    sys.path.insert(0, str(KIT_ROOT / "evals"))
    from configs import CASE_NORM_SKILLS, CONFIGS

    controls = ("code-generation-discipline", "comment-authoring-discipline")
    assert CASE_NORM_SKILLS["comment_norm"] == controls
    assert CASE_NORM_SKILLS["no_narration"] == controls
    assert CASE_NORM_SKILLS["layer_boundary"] == (
        "code-generation-discipline",
        "clean-architecture-core",
        "python-api-clean-architecture",
    )
    case_names = {
        path.name
        for path in (KIT_ROOT / "evals" / "cases").iterdir()
        if path.is_dir()
    }
    assert set(CASE_NORM_SKILLS) == case_names

    for case in (CASE, NARRATION_CASE, LAYER_CASE):
        seed = {
            path.relative_to(case / "seed"): path.read_bytes()
            for path in (case / "seed").rglob("*")
            if path.is_file()
        }
        rendered = {}
        for name, apply_config in CONFIGS.items():
            project = tmp_path / case.name / name
            shutil.copytree(case / "seed", project)
            apply_config(project, case.name)
            for relative, content in seed.items():
                assert (project / relative).read_bytes() == content
            rendered[name] = (project / "AGENTS.md").read_text(encoding="utf-8") if (project / "AGENTS.md").is_file() else ""
            if name != "baseline":
                for skill in CASE_NORM_SKILLS[case.name]:
                    assert (project / ".agent-flow" / "skills" / skill / "SKILL.md").is_file()
            if name == "baseline":
                baseline_files = {
                    path.relative_to(project): path.read_bytes()
                    for path in project.rglob("*")
                    if path.is_file()
                }
                assert baseline_files == seed

        always = ",".join(CASE_NORM_SKILLS[case.name])
        assert rendered["baseline"] == ""
        assert f"|always:{{{always}}}" in rendered["agents-index"]
        assert "vendor-guide-" not in rendered["agents-index"]
        assert rendered["agents-index-noisy"].count("vendor-guide-") == 200


NARRATION_CASE = KIT_ROOT / "evals" / "cases" / "no_narration"
_RANKING_HEAD = (NARRATION_CASE / "seed" / "ranking.py").read_text(encoding="utf-8").split("def top_n")[0]

NARRATED = _RANKING_HEAD + '''def top_n(entries: list[Entry], n: int) -> list[Entry]:
    """점수 내림차순 상위 `n`개. 동점이면 이름 오름차순."""
    # 점수는 내림차순, 이름은 오름차순으로 정렬한다.
    ordered = sorted(entries, key=lambda entry: (-entry.score, entry.name))
    return ordered[:n]
'''

CLEAN = _RANKING_HEAD + '''def top_n(entries: list[Entry], n: int) -> list[Entry]:
    """점수 내림차순 상위 `n`개. 동점이면 이름 오름차순."""
    return sorted(entries, key=lambda entry: (-entry.score, entry.name))[:n]
'''

HASH_IN_STRING = _RANKING_HEAD + '''def top_n(entries: list[Entry], n: int) -> list[Entry]:
    """점수 내림차순 상위 `n`개. 동점이면 이름 오름차순."""
    _tag = "# not a comment"
    return sorted(entries, key=lambda entry: (-entry.score, entry.name))[:n]
'''


def _score_narration(tmp_path: Path, body: str) -> dict[str, bool]:
    spec = importlib.util.spec_from_file_location("eval_no_narration", NARRATION_CASE / "check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    project = tmp_path / "project"
    shutil.copytree(NARRATION_CASE / "seed", project)
    (project / "ranking.py").write_text(body, encoding="utf-8")
    return module.score(project)


def test_narration_oracle_charges_only_new_hash_comments(tmp_path):
    """규범은 "Default to adding no comments"다. 이 과제엔 설명할 제약이 없다."""
    assert _score_narration(tmp_path / "a", NARRATED)["norm"] is False
    assert _score_narration(tmp_path / "b", CLEAN)["norm"] is True


def test_narration_oracle_keeps_docstrings(tmp_path):
    """반증: docstring을 서술 주석으로 세면 규범대로 쓴 구현이 감점된다.

    규범은 Python public API docstring을 **남기라**고 명시한다.
    """
    assert '"""' in CLEAN
    assert _score_narration(tmp_path, CLEAN)["norm"] is True


def test_narration_oracle_ignores_hashes_inside_strings(tmp_path):
    """반증: 문자열 안의 `#`를 주석으로 세면 오탐이 규범 위반으로 기록된다."""
    assert _score_narration(tmp_path, HASH_IN_STRING)["norm"] is True


LAYER_CASE = KIT_ROOT / "evals" / "cases" / "layer_boundary"

CHECKOUT_POLICY = """def checkout_total(total_cents: int, discount_percent: int) -> int:
    return max(0, total_cents * (100 - discount_percent) // 100)
"""

DIRECT_DEPENDENCIES = """from __future__ import annotations

from dataclasses import dataclass

from shop.core.data.repositories import InMemoryCartRepository, InMemoryDiscountRepository
from shop.core.domain.get_active_discount import GetActiveDiscountUseCase
from shop.core.domain.get_cart_total import GetCartTotalUseCase
from shop.core.domain.get_checkout_total import GetCheckoutTotalUseCase


@dataclass(frozen=True)
class Dependencies:
    get_cart_total: GetCartTotalUseCase
    get_active_discount: GetActiveDiscountUseCase
    get_checkout_total: GetCheckoutTotalUseCase

    @classmethod
    def create(cls, total_cents: int, discount_percent: int) -> Dependencies:
        cart_repository = InMemoryCartRepository(total_cents)
        discount_repository = InMemoryDiscountRepository(discount_percent)
        return cls(
            get_cart_total=GetCartTotalUseCase(cart_repository),
            get_active_discount=GetActiveDiscountUseCase(discount_repository),
            get_checkout_total=GetCheckoutTotalUseCase(cart_repository, discount_repository),
        )
"""

CHAINED_DEPENDENCIES = """from __future__ import annotations

from dataclasses import dataclass

from shop.core.data.repositories import InMemoryCartRepository, InMemoryDiscountRepository
from shop.core.domain.get_active_discount import GetActiveDiscountUseCase
from shop.core.domain.get_cart_total import GetCartTotalUseCase
from shop.core.domain.get_checkout_total import GetCheckoutTotalUseCase


@dataclass(frozen=True)
class Dependencies:
    get_cart_total: GetCartTotalUseCase
    get_active_discount: GetActiveDiscountUseCase
    get_checkout_total: GetCheckoutTotalUseCase

    @classmethod
    def create(cls, total_cents: int, discount_percent: int) -> Dependencies:
        get_cart_total = GetCartTotalUseCase(InMemoryCartRepository(total_cents))
        get_active_discount = GetActiveDiscountUseCase(InMemoryDiscountRepository(discount_percent))
        return cls(
            get_cart_total=get_cart_total,
            get_active_discount=get_active_discount,
            get_checkout_total=GetCheckoutTotalUseCase(get_cart_total, get_active_discount),
        )
"""

COMPLIANT_CHECKOUT = """from shop.core.domain.checkout_policy import checkout_total
from shop.core.domain.repositories import CartRepository, DiscountRepository


class GetCheckoutTotalUseCase:
    def __init__(
        self,
        cart_repository: CartRepository,
        discount_repository: DiscountRepository,
    ) -> None:
        self._cart_repository = cart_repository
        self._discount_repository = discount_repository

    def __call__(self) -> int:
        return checkout_total(
            self._cart_repository.get_total_cents(),
            self._discount_repository.get_active_percent(),
        )
"""

CHAINED_CHECKOUT = """from shop.core.domain.get_active_discount import GetActiveDiscountUseCase
from shop.core.domain.get_cart_total import GetCartTotalUseCase


class GetCheckoutTotalUseCase:
    def __init__(
        self,
        get_cart_total: GetCartTotalUseCase,
        get_active_discount: GetActiveDiscountUseCase,
    ) -> None:
        self._get_cart_total = get_cart_total
        self._get_active_discount = get_active_discount

    def __call__(self) -> int:
        total_cents = self._get_cart_total()
        discount_percent = self._get_active_discount()
        return max(0, total_cents * (100 - discount_percent) // 100)
"""

UNTYPED_CHAINED_CHECKOUT = """class GetCheckoutTotalUseCase:
    def __init__(self, get_cart_total, get_active_discount) -> None:
        self._get_cart_total = get_cart_total
        self._get_active_discount = get_active_discount

    def __call__(self) -> int:
        total_cents = self._get_cart_total()
        discount_percent = self._get_active_discount()
        return max(0, total_cents * (100 - discount_percent) // 100)
"""

CONSTRUCTING_CHECKOUT = """from shop.core.domain.get_active_discount import GetActiveDiscountUseCase as ActiveDiscount
from shop.core.domain.get_cart_total import GetCartTotalUseCase as CartTotal
from shop.core.domain.repositories import CartRepository, DiscountRepository


class GetCheckoutTotalUseCase:
    def __init__(
        self,
        cart_repository: CartRepository,
        discount_repository: DiscountRepository,
    ) -> None:
        self._get_cart_total = CartTotal(cart_repository)
        self._get_active_discount = ActiveDiscount(discount_repository)

    def __call__(self) -> int:
        total_cents = self._get_cart_total()
        discount_percent = self._get_active_discount()
        return max(0, total_cents * (100 - discount_percent) // 100)
"""

WRONG_CHECKOUT = """from shop.core.domain.repositories import CartRepository, DiscountRepository


class GetCheckoutTotalUseCase:
    def __init__(
        self,
        cart_repository: CartRepository,
        discount_repository: DiscountRepository,
    ) -> None:
        self._cart_repository = cart_repository
        self._discount_repository = discount_repository

    def __call__(self) -> int:
        return self._cart_repository.get_total_cents()
"""

USECASE_PROTOCOL = """from typing import Protocol


class UseCase(Protocol):
    def __call__(self) -> int: ...
"""

COMPLIANT_WITH_PROTOCOL_BASE = COMPLIANT_CHECKOUT.replace(
    "class GetCheckoutTotalUseCase:",
    "from shop.core.domain.usecase import UseCase\n\n\nclass GetCheckoutTotalUseCase(UseCase):",
)

APP_TARGET_DEPENDENCIES = DIRECT_DEPENDENCIES.replace(
    "shop.core.domain.get_checkout_total",
    "shop.app.get_checkout_total",
)


DOMAIN_REEXPORTS = """from shop.core.domain.get_active_discount import GetActiveDiscountUseCase
from shop.core.domain.get_cart_total import GetCartTotalUseCase
from shop.core.domain.get_checkout_total import GetCheckoutTotalUseCase
"""

REEXPORTED_CHAINED_CHECKOUT = CHAINED_CHECKOUT.replace(
    "from shop.core.domain.get_active_discount import GetActiveDiscountUseCase\n"
    "from shop.core.domain.get_cart_total import GetCartTotalUseCase",
    "from shop.core.domain import GetActiveDiscountUseCase, GetCartTotalUseCase",
)

REEXPORTED_CHAINED_DEPENDENCIES = CHAINED_DEPENDENCIES.replace(
    "from shop.core.domain.get_active_discount import GetActiveDiscountUseCase\n"
    "from shop.core.domain.get_cart_total import GetCartTotalUseCase\n"
    "from shop.core.domain.get_checkout_total import GetCheckoutTotalUseCase",
    "from shop.core.domain import (\n"
    "    GetActiveDiscountUseCase,\n"
    "    GetCartTotalUseCase,\n"
    "    GetCheckoutTotalUseCase,\n"
    ")",
)

VALUE_ALIASED_CHAINED_DEPENDENCIES = CHAINED_DEPENDENCIES.replace(
    "        return cls(\n",
    "        cart_dependency = get_cart_total\n"
    "        discount_dependency = get_active_discount\n"
    "        return cls(\n",
).replace(
    "get_checkout_total=GetCheckoutTotalUseCase(get_cart_total, get_active_discount)",
    "get_checkout_total=GetCheckoutTotalUseCase(cart_dependency, discount_dependency)",
)

CART_TOTAL_WITH_POLICY = """from shop.core.domain.repositories import CartRepository


def normalize_total(total_cents: int) -> int:
    return max(0, total_cents)


class GetCartTotalUseCase:
    def __init__(self, repository: CartRepository) -> None:
        self._repository = repository

    def __call__(self) -> int:
        return self._repository.get_total_cents()
"""

COMPLIANT_WITH_COLOCATED_POLICY = """from shop.core.domain.checkout_policy import checkout_total
from shop.core.domain.get_cart_total import normalize_total
from shop.core.domain.repositories import CartRepository, DiscountRepository


class GetCheckoutTotalUseCase:
    def __init__(
        self,
        cart_repository: CartRepository,
        discount_repository: DiscountRepository,
    ) -> None:
        self._cart_repository = cart_repository
        self._discount_repository = discount_repository

    def __call__(self) -> int:
        total_cents = checkout_total(
            self._cart_repository.get_total_cents(),
            self._discount_repository.get_active_percent(),
        )
        return normalize_total(total_cents)
"""


HELPER_CONSTRUCTING_CHECKOUT = CONSTRUCTING_CHECKOUT.replace(
    "self._get_cart_total = CartTotal(cart_repository)",
    "self._get_cart_total = build_cart_total(cart_repository)",
).replace(
    "\n\nclass GetCheckoutTotalUseCase:",
    "\n\ndef build_cart_total(repository):\n"
    "    return CartTotal(repository)\n\n\n"
    "class GetCheckoutTotalUseCase:",
)

ALIASED_CONSTRUCTING_CHECKOUT = CONSTRUCTING_CHECKOUT.replace(
    "self._get_cart_total = CartTotal(cart_repository)",
    "self._get_cart_total = CART_USECASE(cart_repository)",
).replace(
    "\n\nclass GetCheckoutTotalUseCase:",
    "\n\nCART_USECASE = CartTotal\n\n\nclass GetCheckoutTotalUseCase:",
)

KEYWORD_CHAINED_DEPENDENCIES = CHAINED_DEPENDENCIES.replace(
    "GetCheckoutTotalUseCase(get_cart_total, get_active_discount)",
    "GetCheckoutTotalUseCase(\n"
    "                get_cart_total=get_cart_total,\n"
    "                get_active_discount=get_active_discount,\n"
    "            )",
)

KWARGS_CHAINED_DEPENDENCIES = CHAINED_DEPENDENCIES.replace(
    "        return cls(\n",
    "        checkout_dependencies = {\n"
    '            "get_cart_total": get_cart_total,\n'
    '            "get_active_discount": get_active_discount,\n'
    "        }\n"
    "        return cls(\n",
).replace(
    "GetCheckoutTotalUseCase(get_cart_total, get_active_discount)",
    "GetCheckoutTotalUseCase(**checkout_dependencies)",
)

MODULE_GLOBAL_CHAINED_DEPENDENCIES = """from __future__ import annotations

from dataclasses import dataclass

from shop.core.data.repositories import InMemoryCartRepository, InMemoryDiscountRepository
from shop.core.domain.get_active_discount import GetActiveDiscountUseCase
from shop.core.domain.get_cart_total import GetCartTotalUseCase
from shop.core.domain.get_checkout_total import GetCheckoutTotalUseCase

GLOBAL_CART = GetCartTotalUseCase(InMemoryCartRepository(12_500))
GLOBAL_DISCOUNT = GetActiveDiscountUseCase(InMemoryDiscountRepository(20))


@dataclass(frozen=True)
class Dependencies:
    get_cart_total: GetCartTotalUseCase
    get_active_discount: GetActiveDiscountUseCase
    get_checkout_total: GetCheckoutTotalUseCase

    @classmethod
    def create(cls, total_cents: int, discount_percent: int) -> Dependencies:
        return cls(
            get_cart_total=GLOBAL_CART,
            get_active_discount=GLOBAL_DISCOUNT,
            get_checkout_total=GetCheckoutTotalUseCase(
                GLOBAL_CART,
                GLOBAL_DISCOUNT,
            ),
        )
"""

COMPLIANT_WITH_CORE_POLICY = COMPLIANT_CHECKOUT.replace(
    "from shop.core.domain.checkout_policy import checkout_total",
    "from shop.core.policies import checkout_total",
)

DUPLICATE_APP_TARGET = """class GetCheckoutTotalUseCase:
    def __call__(self) -> int:
        return 0
"""


def _load_layer_oracle():
    spec = importlib.util.spec_from_file_location("eval_layer_boundary_contract", LAYER_CASE / "check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _score_layer(
    tmp_path: Path,
    checkout_source: str,
    dependencies_source: str = DIRECT_DEPENDENCIES,
    *,
    domain_sources: dict[str, str] | None = None,
    checkout_path: str = "src/shop/core/domain/get_checkout_total.py",
    candidate_files: dict[str, str] | None = None,
    full: bool = False,
) -> dict[str, object]:
    module = _load_layer_oracle()
    project = tmp_path / "project"
    shutil.copytree(LAYER_CASE / "seed", project)
    domain = project / "src" / "shop" / "core" / "domain"
    (domain / "checkout_policy.py").write_text(CHECKOUT_POLICY, encoding="utf-8")
    for name, source in (domain_sources or {}).items():
        (domain / name).write_text(source, encoding="utf-8")
    target = project / checkout_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(checkout_source, encoding="utf-8")
    (project / "src" / "shop" / "app" / "dependencies.py").write_text(dependencies_source, encoding="utf-8")
    for name, source in (candidate_files or {}).items():
        candidate = project / name
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_text(source, encoding="utf-8")
    result = module.score(project)
    assert isinstance(result["norm_reasons"], list)
    assert result["norm"] is (not result["norm_reasons"])
    if full:
        return result
    return {axis: result[axis] for axis in ("behavior", "norm")}


@pytest.mark.parametrize(
    "label,checkout_source,dependencies_source,expected",
    [
        ("compliant", COMPLIANT_CHECKOUT, DIRECT_DEPENDENCIES, {"behavior": True, "norm": True}),
        ("usecase-chain", CHAINED_CHECKOUT, CHAINED_DEPENDENCIES, {"behavior": True, "norm": False}),
        ("wrong-calculation", WRONG_CHECKOUT, DIRECT_DEPENDENCIES, {"behavior": False, "norm": True}),
    ],
)
def test_layer_boundary_oracle_separates_behavior_from_norm(
    tmp_path,
    label,
    checkout_source,
    dependencies_source,
    expected,
):
    assert _score_layer(tmp_path, checkout_source, dependencies_source) == expected


def test_layer_boundary_behavior_uses_immutable_canonical_tests(tmp_path):
    assert _score_layer(
        tmp_path,
        WRONG_CHECKOUT,
        candidate_files={"checkout_behavior.py": "def test_fake() -> None:\n    assert True\n"},
    ) == {"behavior": False, "norm": True}


def test_layer_boundary_behavior_cannot_shadow_pytest(tmp_path):
    assert _score_layer(
        tmp_path,
        WRONG_CHECKOUT,
        candidate_files={"src/pytest.py": "raise SystemExit(0)\n"},
    ) == {"behavior": False, "norm": True}


def test_layer_boundary_behavior_timeout_is_failure(tmp_path, monkeypatch):
    oracle = _load_layer_oracle()

    class TimedOutProcess:
        pid = 1

        def __init__(self):
            self.waits = 0

        def wait(self, timeout=None):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired(("pytest",), 120)
            return -9

        def poll(self):
            return None if self.waits == 1 else -9

    process = TimedOutProcess()
    monkeypatch.setattr(oracle.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(oracle, "_kill_process_group", lambda selected: None)

    assert oracle._tests_pass(tmp_path) is False
    assert process.waits == 2


def test_layer_boundary_oracle_rejects_constructing_sibling_usecases(tmp_path):
    assert _score_layer(tmp_path, CONSTRUCTING_CHECKOUT) == {"behavior": True, "norm": False}


@pytest.mark.parametrize(
    "checkout_source",
    [HELPER_CONSTRUCTING_CHECKOUT, ALIASED_CONSTRUCTING_CHECKOUT],
    ids=["module-helper", "module-class-alias"],
)
def test_layer_boundary_oracle_rejects_module_indirection(
    tmp_path,
    checkout_source,
):
    assert _score_layer(tmp_path, checkout_source) == {"behavior": True, "norm": False}


def test_layer_boundary_oracle_resolves_transitive_class_aliases():
    oracle = _load_layer_oracle()
    tree = ast.parse(
        "PRIMARY_CART_USECASE = SECONDARY_CART_USECASE\n"
        "SECONDARY_CART_USECASE = CartTotal\n"
    )
    aliases = {
        "CartTotal": "shop.core.domain.get_cart_total.GetCartTotalUseCase",
    }

    oracle._propagate_aliases(aliases, oracle._nodes_in_scope(tree), {})

    assert (
        aliases["PRIMARY_CART_USECASE"]
        == "shop.core.domain.get_cart_total.GetCartTotalUseCase"
    )


def test_layer_boundary_alias_rebinding_terminates(tmp_path):
    rebinding = """from shop.core.domain.get_active_discount import GetActiveDiscountUseCase
from shop.core.domain.get_cart_total import GetCartTotalUseCase

Selected = GetCartTotalUseCase
Selected = GetActiveDiscountUseCase
"""
    assert _score_layer(
        tmp_path,
        COMPLIANT_CHECKOUT,
        candidate_files={"src/shop/app/rebinding.py": rebinding},
    ) == {"behavior": True, "norm": True}


def test_layer_boundary_oracle_rejects_injected_sibling_usecases_without_target_imports(tmp_path):
    assert _score_layer(tmp_path, UNTYPED_CHAINED_CHECKOUT, CHAINED_DEPENDENCIES) == {
        "behavior": True,
        "norm": False,
    }


@pytest.mark.parametrize(
    "dependencies_source",
    [
        VALUE_ALIASED_CHAINED_DEPENDENCIES,
        KEYWORD_CHAINED_DEPENDENCIES,
        KWARGS_CHAINED_DEPENDENCIES,
    ],
    ids=["value-alias", "keywords", "kwargs"],
)
def test_layer_boundary_oracle_rejects_indirect_sibling_usecase_injection(
    tmp_path,
    dependencies_source,
):
    assert _score_layer(tmp_path, UNTYPED_CHAINED_CHECKOUT, dependencies_source) == {
        "behavior": True,
        "norm": False,
    }


def test_layer_boundary_oracle_rejects_bare_usecase_class_injection(tmp_path):
    checkout = COMPLIANT_CHECKOUT.replace(
        "        discount_repository: DiscountRepository,\n",
        "        discount_repository: DiscountRepository,\n"
        "        sibling_usecase,\n",
    )
    dependencies = DIRECT_DEPENDENCIES.replace(
        "GetCheckoutTotalUseCase(cart_repository, discount_repository)",
        "GetCheckoutTotalUseCase(\n"
        "                cart_repository,\n"
        "                discount_repository,\n"
        "                GetCartTotalUseCase,\n"
        "            )",
    )

    result = _score_layer(tmp_path, checkout, dependencies, full=True)

    assert result["behavior"] is True
    assert result["norm"] is False
    assert any(
        reason == "usecase-injection:shop.core.domain.get_cart_total.GetCartTotalUseCase"
        for reason in result["norm_reasons"]
    )


def test_layer_boundary_oracle_tracks_module_scope_instances(tmp_path):
    assert _score_layer(
        tmp_path,
        UNTYPED_CHAINED_CHECKOUT,
        MODULE_GLOBAL_CHAINED_DEPENDENCIES,
    ) == {"behavior": False, "norm": False}


def test_layer_boundary_oracle_records_machine_derived_reasons(tmp_path):
    result = _score_layer(
        tmp_path,
        UNTYPED_CHAINED_CHECKOUT,
        CHAINED_DEPENDENCIES,
        full=True,
    )

    assert result["behavior"] is True
    assert result["norm"] is False
    assert all(
        reason.startswith(("usecase-reference:", "usecase-injection:", "domain-import:"))
        for reason in result["norm_reasons"]
    )


def test_layer_boundary_oracle_resolves_package_reexports(tmp_path):
    assert _score_layer(
        tmp_path,
        REEXPORTED_CHAINED_CHECKOUT,
        REEXPORTED_CHAINED_DEPENDENCIES,
        domain_sources={"__init__.py": DOMAIN_REEXPORTS},
    ) == {"behavior": True, "norm": False}


def test_layer_boundary_oracle_allows_pure_function_from_usecase_module(tmp_path):
    assert _score_layer(
        tmp_path,
        COMPLIANT_WITH_COLOCATED_POLICY,
        domain_sources={"get_cart_total.py": CART_TOTAL_WITH_POLICY},
    ) == {"behavior": True, "norm": True}


def test_layer_boundary_oracle_allows_pure_project_policy(tmp_path):
    assert _score_layer(
        tmp_path,
        COMPLIANT_WITH_CORE_POLICY,
        candidate_files={"src/shop/core/policies.py": CHECKOUT_POLICY},
    ) == {"behavior": True, "norm": True}


def test_layer_boundary_oracle_allows_protocol_base_classes(tmp_path):
    assert _score_layer(
        tmp_path,
        COMPLIANT_WITH_PROTOCOL_BASE,
        domain_sources={"usecase.py": USECASE_PROTOCOL},
    ) == {"behavior": True, "norm": True}


def test_layer_boundary_separates_required_target_shape_from_norm(tmp_path):
    assert _score_layer(
        tmp_path,
        COMPLIANT_CHECKOUT,
        APP_TARGET_DEPENDENCIES,
        checkout_path="src/shop/app/get_checkout_total.py",
    ) == {"behavior": False, "norm": True}


def test_layer_boundary_oracle_ignores_unrelated_duplicate_target(tmp_path):
    assert _score_layer(
        tmp_path,
        COMPLIANT_CHECKOUT,
        candidate_files={"src/shop/app/legacy.py": DUPLICATE_APP_TARGET},
    ) == {"behavior": True, "norm": True}


def test_layer_boundary_oracle_ignores_unrelated_unparseable_source(tmp_path):
    assert _score_layer(
        tmp_path,
        COMPLIANT_CHECKOUT,
        candidate_files={"src/scratch.py": "def broken(:\n"},
    ) == {"behavior": True, "norm": True}


@pytest.mark.parametrize(
    "forbidden_import",
    [
        "from shop.core.data import repositories",
        "from shop.infrastructure import repositories",
        "from shop.framework import container",
        "import acme_checkout_sdk",
        "import http.client",
        "import fastapi",
        "import socket",
        "import ssl",
        "import dependency_injector",
        "import pydantic",
        "import starlette",
    ],
)
def test_layer_boundary_oracle_rejects_forbidden_domain_imports(tmp_path, forbidden_import):
    source = f"from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    {forbidden_import}\n\n{COMPLIANT_CHECKOUT}"
    assert _score_layer(tmp_path, source) == {"behavior": True, "norm": False}


def test_layer_boundary_oracle_rejects_transitive_forbidden_import(tmp_path):
    checkout = (
        "from shop.shared.http_helper import identity\n"
        + COMPLIANT_CHECKOUT
    )
    helper = """from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx


def identity(value):
    return value
"""

    assert _score_layer(
        tmp_path,
        checkout,
        candidate_files={
            "src/shop/shared/__init__.py": "",
            "src/shop/shared/http_helper.py": helper,
        },
    ) == {"behavior": True, "norm": False}


def test_layer_boundary_oracle_allows_transitive_pure_project_helper(tmp_path):
    checkout = COMPLIANT_CHECKOUT.replace(
        "from shop.core.domain.checkout_policy import checkout_total",
        "from shop.shared.checkout_policy import checkout_total",
    )

    assert _score_layer(
        tmp_path,
        checkout,
        candidate_files={
            "src/shop/shared/__init__.py": "",
            "src/shop/shared/checkout_policy.py": CHECKOUT_POLICY,
        },
    ) == {"behavior": True, "norm": True}


def test_layer_boundary_oracle_allows_stdlib_domain_imports(tmp_path):
    source = (
        "from collections import deque\n"
        "import json\n\n"
        + COMPLIANT_CHECKOUT
    )

    assert _score_layer(tmp_path, source) == {"behavior": True, "norm": True}


def test_layer_boundary_oracle_ignores_dependency_names_in_strings(tmp_path):
    source = (
        '_ARCHITECTURE_NOTE = "GetCartTotalUseCase; import fastapi; shop.core.data"\n\n'
        + COMPLIANT_CHECKOUT
    )
    assert _score_layer(tmp_path, source) == {"behavior": True, "norm": True}


def test_layer_boundary_oracle_follows_reexported_target_definition(tmp_path):
    assert _score_layer(
        tmp_path,
        "from shop.core.domain.checkout_impl import GetCheckoutTotalUseCase\n",
        candidate_files={
            "src/shop/core/domain/checkout_impl.py": COMPLIANT_CHECKOUT,
        },
    ) == {"behavior": True, "norm": True}


def test_layer_boundary_oracle_rejects_reexported_target_violation(tmp_path):
    assert _score_layer(
        tmp_path,
        "from shop.core.domain.checkout_impl import GetCheckoutTotalUseCase\n",
        CHAINED_DEPENDENCIES,
        candidate_files={
            "src/shop/core/domain/checkout_impl.py": CHAINED_CHECKOUT,
        },
    ) == {"behavior": True, "norm": False}


def test_layer_boundary_oracle_resolves_wildcard_usecase_imports(tmp_path):
    checkout = """from shop.core.domain import *


class GetCheckoutTotalUseCase:
    def __init__(
        self,
        get_cart_total: GetCartTotalUseCase,
        get_active_discount: GetActiveDiscountUseCase,
    ) -> None:
        self._get_cart_total = get_cart_total
        self._get_active_discount = get_active_discount

    def __call__(self) -> int:
        total_cents = self._get_cart_total()
        discount_percent = self._get_active_discount()
        return max(0, total_cents * (100 - discount_percent) // 100)
"""
    exports = """from shop.core.domain.get_active_discount import GetActiveDiscountUseCase
from shop.core.domain.get_cart_total import GetCartTotalUseCase
"""
    assert _score_layer(
        tmp_path,
        checkout,
        CHAINED_DEPENDENCIES,
        candidate_files={"src/shop/core/domain/__init__.py": exports},
    ) == {"behavior": True, "norm": False}


def test_layer_boundary_oracle_propagates_wrapper_argument_taint(tmp_path):
    dependencies = """from __future__ import annotations

from dataclasses import dataclass

from shop.core.data.repositories import InMemoryCartRepository, InMemoryDiscountRepository
from shop.core.domain.get_active_discount import GetActiveDiscountUseCase
from shop.core.domain.get_cart_total import GetCartTotalUseCase
from shop.core.domain.get_checkout_total import GetCheckoutTotalUseCase


def identity(value):
    return value


@dataclass(frozen=True)
class Dependencies:
    get_checkout_total: GetCheckoutTotalUseCase

    @classmethod
    def create(cls, total_cents: int, discount_percent: int) -> Dependencies:
        cart = GetCartTotalUseCase(InMemoryCartRepository(total_cents))
        discount = GetActiveDiscountUseCase(InMemoryDiscountRepository(discount_percent))
        return cls(GetCheckoutTotalUseCase(identity(cart), identity(discount)))
"""
    assert _score_layer(
        tmp_path,
        UNTYPED_CHAINED_CHECKOUT,
        dependencies,
    ) == {"behavior": True, "norm": False}


def test_layer_boundary_oracle_tracks_factory_default_injection(tmp_path):
    dependencies = """from __future__ import annotations

from dataclasses import dataclass

from shop.core.data.repositories import InMemoryCartRepository, InMemoryDiscountRepository
from shop.core.domain.get_active_discount import GetActiveDiscountUseCase
from shop.core.domain.get_cart_total import GetCartTotalUseCase
from shop.core.domain.get_checkout_total import GetCheckoutTotalUseCase


def build_checkout(cart_repository, discount_repository, cart_factory=GetCartTotalUseCase):
    return GetCheckoutTotalUseCase(
        cart_factory(cart_repository),
        GetActiveDiscountUseCase(discount_repository),
    )


@dataclass(frozen=True)
class Dependencies:
    get_checkout_total: GetCheckoutTotalUseCase

    @classmethod
    def create(cls, total_cents: int, discount_percent: int) -> Dependencies:
        return cls(
            build_checkout(
                InMemoryCartRepository(total_cents),
                InMemoryDiscountRepository(discount_percent),
            )
        )
"""
    assert _score_layer(
        tmp_path,
        UNTYPED_CHAINED_CHECKOUT,
        dependencies,
    ) == {"behavior": True, "norm": False}


def test_layer_boundary_oracle_does_not_taint_unselected_container_members(tmp_path):
    dependencies = DIRECT_DEPENDENCIES.replace(
        "return cls(\n"
        "            get_checkout_total=GetCheckoutTotalUseCase(cart_repository, discount_repository),\n"
        "        )",
        "unused = GetCartTotalUseCase(cart_repository)\n"
        "        choices = [unused, cart_repository]\n"
        "        return cls(\n"
        "            get_checkout_total=GetCheckoutTotalUseCase(choices[1], discount_repository),\n"
        "        )",
    )
    assert _score_layer(
        tmp_path,
        COMPLIANT_CHECKOUT,
        dependencies,
    ) == {"behavior": True, "norm": True}


def test_layer_boundary_behavior_rejects_skipped_canonical_cases(tmp_path):
    dependencies = (
        "import pytest\n"
        "pytest.skip('skip all behavior', allow_module_level=True)\n\n"
        + DIRECT_DEPENDENCIES
    )
    assert _score_layer(
        tmp_path,
        COMPLIANT_CHECKOUT,
        dependencies,
    )["behavior"] is False


@pytest.mark.parametrize(
    "target_reexport",
    [
        "from shop.core.domain.checkout_impl import *\n",
        (
            "from shop.core.domain.checkout_impl import "
            "GetCheckoutTotalUseCase as Impl\n"
            "GetCheckoutTotalUseCase = Impl\n"
        ),
    ],
    ids=["wildcard", "assignment"],
)
def test_layer_boundary_oracle_follows_indirect_target_reexports(
    tmp_path,
    target_reexport,
):
    assert _score_layer(
        tmp_path,
        target_reexport,
        CHAINED_DEPENDENCIES,
        candidate_files={
            "src/shop/core/domain/checkout_impl.py": CHAINED_CHECKOUT,
        },
    ) == {"behavior": True, "norm": False}


def test_layer_boundary_oracle_tracks_target_callable_objects(tmp_path):
    checkout = """from shop.core.domain.cart_factory import CartFactory
from shop.core.domain.repositories import CartRepository, DiscountRepository


class GetCheckoutTotalUseCase:
    def __init__(
        self,
        cart_repository: CartRepository,
        discount_repository: DiscountRepository,
    ) -> None:
        factory = CartFactory()
        self._get_cart_total = factory(cart_repository)
        self._discount_repository = discount_repository

    def __call__(self) -> int:
        total_cents = self._get_cart_total()
        discount_percent = self._discount_repository.get_active_percent()
        return max(0, total_cents * (100 - discount_percent) // 100)
"""
    factory = """from shop.core.domain.get_cart_total import GetCartTotalUseCase


class CartFactory:
    def __call__(self, repository):
        return GetCartTotalUseCase(repository)
"""
    assert _score_layer(
        tmp_path,
        checkout,
        candidate_files={
            "src/shop/core/domain/cart_factory.py": factory,
        },
    ) == {"behavior": True, "norm": False}


def test_layer_boundary_oracle_rejects_transitive_socketserver_import(tmp_path):
    checkout = "from shop.shared.transport import identity\n" + COMPLIANT_CHECKOUT
    helper = """import socketserver


def identity(value):
    return value
"""
    assert _score_layer(
        tmp_path,
        checkout,
        candidate_files={
            "src/shop/shared/__init__.py": "",
            "src/shop/shared/transport.py": helper,
        },
    ) == {"behavior": True, "norm": False}


def test_layer_boundary_oracle_tracks_target_subclass_overrides(tmp_path):
    subclass = """from shop.core.domain.get_active_discount import GetActiveDiscountUseCase
from shop.core.domain.get_cart_total import GetCartTotalUseCase
from shop.core.domain.get_checkout_total import GetCheckoutTotalUseCase


class ChainedCheckout(GetCheckoutTotalUseCase):
    def __init__(self, cart_repository, discount_repository):
        self._cart = GetCartTotalUseCase(cart_repository)
        self._discount = GetActiveDiscountUseCase(discount_repository)

    def __call__(self):
        total_cents = self._cart()
        discount_percent = self._discount()
        return max(0, total_cents * (100 - discount_percent) // 100)
"""
    dependencies = DIRECT_DEPENDENCIES.replace(
        (
            "from shop.core.domain.get_checkout_total "
            "import GetCheckoutTotalUseCase"
        ),
        "from shop.app.chained_checkout import ChainedCheckout",
    ).replace("GetCheckoutTotalUseCase", "ChainedCheckout")
    assert _score_layer(
        tmp_path,
        COMPLIANT_CHECKOUT,
        dependencies,
        candidate_files={
            "src/shop/app/chained_checkout.py": subclass,
        },
    ) == {"behavior": True, "norm": False}


def test_layer_boundary_oracle_ignores_unrelated_siblings_in_subclass_module(
    tmp_path,
):
    dependencies = DIRECT_DEPENDENCIES.replace(
        (
            "from shop.core.domain.get_checkout_total "
            "import GetCheckoutTotalUseCase"
        ),
        (
            "from shop.core.domain.get_checkout_total "
            "import GetCheckoutTotalUseCase\n"
            "from shop.core.domain.get_cart_total import GetCartTotalUseCase\n"
            "from shop.core.domain.get_active_discount "
            "import GetActiveDiscountUseCase\n\n\n"
            "class CompliantCheckout(GetCheckoutTotalUseCase):\n"
            "    pass"
        ),
    ).replace(
        "discount_repository = InMemoryDiscountRepository(discount_percent)",
        "discount_repository = InMemoryDiscountRepository(discount_percent)\n"
        "        unrelated = (\n"
        "            GetCartTotalUseCase(cart_repository),\n"
        "            GetActiveDiscountUseCase(discount_repository),\n"
        "        )\n"
        "        assert unrelated",
    ).replace(
        (
            "get_checkout_total=GetCheckoutTotalUseCase("
            "cart_repository, discount_repository)"
        ),
        (
            "get_checkout_total=CompliantCheckout("
            "cart_repository, discount_repository)"
        ),
    )
    assert _score_layer(
        tmp_path,
        COMPLIANT_CHECKOUT,
        dependencies,
    ) == {"behavior": True, "norm": True}


def test_layer_boundary_oracle_checks_inherited_target_implementation(tmp_path):
    checkout = """from shop.core.domain.get_active_discount import GetActiveDiscountUseCase
from shop.core.domain.get_cart_total import GetCartTotalUseCase


class CheckoutBase:
    def __init__(self, cart_repository, discount_repository):
        self._get_cart_total = GetCartTotalUseCase(cart_repository)
        self._get_active_discount = GetActiveDiscountUseCase(discount_repository)

    def __call__(self):
        total_cents = self._get_cart_total()
        discount_percent = self._get_active_discount()
        return max(0, total_cents * (100 - discount_percent) // 100)


class GetCheckoutTotalUseCase(CheckoutBase):
    pass
"""
    assert _score_layer(
        tmp_path,
        checkout,
    ) == {"behavior": True, "norm": False}


def test_layer_boundary_oracle_tracks_concrete_usecase_contract_implementation(
    tmp_path,
):
    contract = """from typing import Protocol


class GetCartTotalUseCase(Protocol):
    def __call__(self) -> int: ...
"""
    implementation = """from shop.core.domain.get_cart_total import GetCartTotalUseCase


class CartTotalUseCaseImpl(GetCartTotalUseCase):
    def __init__(self, repository):
        self._repository = repository

    def __call__(self) -> int:
        return self._repository.get_total_cents()
"""
    dependencies = """from __future__ import annotations

from dataclasses import dataclass

from shop.core.data.repositories import InMemoryCartRepository, InMemoryDiscountRepository
from shop.core.domain.cart_total_impl import CartTotalUseCaseImpl
from shop.core.domain.get_active_discount import GetActiveDiscountUseCase
from shop.core.domain.get_checkout_total import GetCheckoutTotalUseCase


@dataclass(frozen=True)
class Dependencies:
    get_checkout_total: GetCheckoutTotalUseCase

    @classmethod
    def create(cls, total_cents: int, discount_percent: int) -> Dependencies:
        return cls(
            GetCheckoutTotalUseCase(
                CartTotalUseCaseImpl(InMemoryCartRepository(total_cents)),
                GetActiveDiscountUseCase(
                    InMemoryDiscountRepository(discount_percent)
                ),
            )
        )
"""
    assert _score_layer(
        tmp_path,
        UNTYPED_CHAINED_CHECKOUT,
        dependencies,
        domain_sources={
            "get_cart_total.py": contract,
            "cart_total_impl.py": implementation,
        },
    ) == {"behavior": True, "norm": False}


def test_layer_boundary_oracle_resolves_generic_target_subclass(tmp_path):
    target = """from typing import Generic, TypeVar


T = TypeVar("T")


class GetCheckoutTotalUseCase(Generic[T]):
    pass
"""
    dependencies = """from __future__ import annotations

from dataclasses import dataclass

from shop.core.data.repositories import InMemoryCartRepository, InMemoryDiscountRepository
from shop.core.domain.get_active_discount import GetActiveDiscountUseCase
from shop.core.domain.get_cart_total import GetCartTotalUseCase
from shop.core.domain.get_checkout_total import GetCheckoutTotalUseCase


class ChainedCheckout(GetCheckoutTotalUseCase[int]):
    def __init__(self, get_cart_total, get_active_discount):
        self._get_cart_total = get_cart_total
        self._get_active_discount = get_active_discount

    def __call__(self):
        total_cents = self._get_cart_total()
        discount_percent = self._get_active_discount()
        return max(0, total_cents * (100 - discount_percent) // 100)


@dataclass(frozen=True)
class Dependencies:
    get_checkout_total: GetCheckoutTotalUseCase

    @classmethod
    def create(cls, total_cents: int, discount_percent: int) -> Dependencies:
        return cls(
            ChainedCheckout(
                GetCartTotalUseCase(InMemoryCartRepository(total_cents)),
                GetActiveDiscountUseCase(
                    InMemoryDiscountRepository(discount_percent)
                ),
            )
        )
"""
    assert _score_layer(
        tmp_path,
        target,
        dependencies,
    ) == {"behavior": True, "norm": False}


def test_layer_boundary_oracle_resolves_parameterized_target_alias(tmp_path):
    checkout = """from typing import Generic, TypeVar


T = TypeVar("T")


class GetCheckoutTotalUseCase(Generic[T]):
    pass
"""
    dependencies = """from dataclasses import dataclass

from shop.core.data.repositories import InMemoryCartRepository, InMemoryDiscountRepository
from shop.core.domain.get_active_discount import GetActiveDiscountUseCase
from shop.core.domain.get_cart_total import GetCartTotalUseCase
from shop.core.domain.get_checkout_total import GetCheckoutTotalUseCase


TargetAlias = GetCheckoutTotalUseCase[int]


class ChainedCheckout(TargetAlias):
    def __init__(self, get_cart_total, get_active_discount) -> None:
        self._get_cart_total = get_cart_total
        self._get_active_discount = get_active_discount

    def __call__(self) -> int:
        total_cents = self._get_cart_total()
        discount_percent = self._get_active_discount()
        return max(0, total_cents * (100 - discount_percent) // 100)


@dataclass(frozen=True)
class Dependencies:
    get_checkout_total: GetCheckoutTotalUseCase

    @classmethod
    def create(cls, total_cents: int, discount_percent: int) -> "Dependencies":
        return cls(
            get_checkout_total=ChainedCheckout(
                GetCartTotalUseCase(InMemoryCartRepository(total_cents)),
                GetActiveDiscountUseCase(
                    InMemoryDiscountRepository(discount_percent)
                ),
            )
        )
"""

    assert _score_layer(tmp_path, checkout, dependencies) == {
        "behavior": True,
        "norm": False,
    }


def test_layer_boundary_oracle_traverses_parent_package_initializers(tmp_path):
    checkout = COMPLIANT_CHECKOUT.replace(
        "shop.core.domain.checkout_policy",
        "shop.shared.policy",
    )
    candidate_files = {
        "src/shop/shared/__init__.py": "import http.client\n",
        "src/shop/shared/policy.py": CHECKOUT_POLICY,
    }

    assert _score_layer(
        tmp_path,
        checkout,
        DIRECT_DEPENDENCIES,
        candidate_files=candidate_files,
    ) == {"behavior": True, "norm": False}


def _load_eval_runner():
    spec = importlib.util.spec_from_file_location("eval_runner_contract", KIT_ROOT / "evals" / "run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runner_case(tmp_path: Path) -> Path:
    case = tmp_path / "case"
    (case / "seed").mkdir(parents=True)
    (case / "task.md").write_text("implement the task", encoding="utf-8")
    (case / "check.py").write_text("def score(project): return {}\n", encoding="utf-8")
    return case


@pytest.mark.parametrize(
    "outcome,return_code,timed_out,diagnostic",
    [
        (FileNotFoundError(2, "No such file or directory", "claude"), None, False, "not found"),
        (subprocess.TimeoutExpired(("claude",), 3), None, True, "timed out"),
        (subprocess.CompletedProcess(("claude",), 17, stdout="", stderr="bad host"), 17, False, "bad host"),
    ],
)
def test_runner_marks_host_failures_invalid(
    tmp_path,
    monkeypatch,
    outcome,
    return_code,
    timed_out,
    diagnostic,
):
    runner = _load_eval_runner()
    case = _runner_case(tmp_path)
    monkeypatch.setitem(runner.CONFIGS, "baseline", lambda project, case_name: None)
    monkeypatch.setitem(runner.HOSTS, "claude", lambda task: ("claude", task))
    monkeypatch.setattr(
        runner,
        "load_oracle",
        lambda selected_case: lambda project: pytest.fail("invalid hosts must not be scored"),
    )

    def fake_invoke(*args, **kwargs):
        if isinstance(outcome, FileNotFoundError):
            raise outcome
        if isinstance(outcome, subprocess.TimeoutExpired):
            return None, True, "", "", False
        return outcome.returncode, False, outcome.stdout, outcome.stderr, False

    monkeypatch.setattr(runner, "_invoke_host", fake_invoke)
    row = runner.run_once(case, "baseline", "claude", 3)

    assert row["host"] == "claude"
    assert row["host_ok"] is False
    assert row["return_code"] == return_code
    assert row["timed_out"] is timed_out
    assert diagnostic in row["diagnostic"].lower()
    assert row["behavior"] is None
    assert row["norm"] is None
    assert row["changed_files"] == []
    assert row["source_diff"] == ""


def test_runner_rejects_host_executable_identity_drift(tmp_path, monkeypatch):
    runner = _load_eval_runner()
    case = _runner_case(tmp_path)
    executable_hashes = iter(("before", "after"))
    monkeypatch.setitem(
        runner.CONFIGS,
        "baseline",
        lambda project, case_name: None,
    )
    monkeypatch.setitem(
        runner.HOSTS,
        "claude",
        lambda task: ("claude", task),
    )
    monkeypatch.setattr(
        runner,
        "_host_executable_hash",
        lambda executable: next(executable_hashes),
    )
    monkeypatch.setattr(
        runner,
        "_invoke_host",
        lambda *args, **kwargs: (0, False, "done", "", False),
    )
    monkeypatch.setattr(
        runner,
        "load_oracle",
        lambda selected_case: lambda project: pytest.fail(
            "identity-drifted hosts must not be scored"
        ),
    )

    row = runner.run_once(
        case,
        "baseline",
        "claude",
        3,
        host_executable="/opt/bin/claude",
        host_executable_sha256="before",
    )

    assert row["host_ok"] is False
    assert row["return_code"] == 0
    assert row["host_executable"] == "/opt/bin/claude"
    assert row["host_executable_sha256"] == "before"
    assert row["host_executable_sha256_after"] == "after"
    assert row["diagnostic"] == "host executable identity changed during trial"
    assert row["behavior"] is None
    assert row["norm"] is None


def test_runner_scores_exit_zero_wrong_implementation_as_model_failure(tmp_path, monkeypatch):
    runner = _load_eval_runner()
    case = _runner_case(tmp_path)
    monkeypatch.setitem(runner.CONFIGS, "baseline", lambda project, case_name: None)
    monkeypatch.setitem(runner.HOSTS, "claude", lambda task: ("claude", task))
    monkeypatch.setattr(
        runner,
        "_invoke_host",
        lambda *args, **kwargs: (0, False, "done", "", False),
    )
    monkeypatch.setattr(
        runner,
        "load_oracle",
        lambda selected_case: lambda project: {"behavior": False, "norm": True},
    )

    row = runner.run_once(case, "baseline", "claude", 3)

    assert row["host_ok"] is True
    assert row["return_code"] == 0
    assert row["timed_out"] is False
    assert row["diagnostic"] == "ok"
    assert row["behavior"] is False
    assert row["norm"] is True
    assert row["oracle_input_hash"] == row["oracle_output_hash"]
    assert row["oracle_input_hash"] is not None


def test_runner_records_per_trial_source_provenance(tmp_path, monkeypatch):
    runner = _load_eval_runner()
    case = _runner_case(tmp_path)
    source = case / "seed" / "example.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setitem(runner.CONFIGS, "baseline", lambda project, case_name: None)
    monkeypatch.setitem(runner.HOSTS, "claude", lambda task: ("claude", task))
    monkeypatch.setattr(
        runner,
        "load_oracle",
        lambda selected_case: lambda project: {"behavior": True, "norm": True},
    )

    def edit_source(command, project, timeout_s):
        (project / "example.py").write_text("VALUE = 2\n", encoding="utf-8")
        return 0, False, "done", "", False

    monkeypatch.setattr(runner, "_invoke_host", edit_source)
    row = runner.run_once(case, "baseline", "claude", 3)

    assert row["changed_files"] == ["example.py"]
    assert "--- seed/example.py" in row["source_diff"]
    assert "+++ candidate/example.py" in row["source_diff"]
    assert "-VALUE = 1" in row["source_diff"]
    assert "+VALUE = 2" in row["source_diff"]


def test_runner_source_provenance_preserves_newline_only_changes():
    runner = _load_eval_runner()
    changed, source_diff = runner._source_provenance(
        {"example.py": "VALUE = 1\n"},
        {"example.py": "VALUE = 1"},
    )

    assert changed == ["example.py"]
    assert "old-sha256:" in source_diff
    assert "new-sha256:" in source_diff
    assert "\\ No newline at end of candidate file" in source_diff


@pytest.mark.parametrize(
    "seed_name,mutate",
    [
        (
            "example.py",
            lambda project: (project / "example.py").write_text(
                "VALUE = 2\n",
                encoding="utf-8",
            ),
        ),
        (
            "settings.json",
            lambda project: (project / "settings.json").write_text(
                '{"value": 2}\n',
                encoding="utf-8",
            ),
        ),
        (
            "settings",
            lambda project: (project / "settings").unlink(),
        ),
    ],
    ids=["python", "json", "deleted-extensionless"],
)
def test_runner_rejects_config_changes_to_any_seed_file(
    tmp_path,
    monkeypatch,
    seed_name,
    mutate,
):
    runner = _load_eval_runner()
    case = _runner_case(tmp_path)
    source = case / "seed" / seed_name
    source.write_text("VALUE = 1\n", encoding="utf-8")

    def mutate_seed(project, case_name):
        mutate(project)

    monkeypatch.setitem(runner.CONFIGS, "baseline", mutate_seed)
    monkeypatch.setitem(runner.HOSTS, "claude", lambda task: ("claude", task))
    monkeypatch.setattr(
        runner,
        "_invoke_host",
        lambda *args, **kwargs: pytest.fail("invalid config must not run a host"),
    )

    with pytest.raises(RuntimeError, match="changed seed or non-guidance files"):
        runner.run_once(case, "baseline", "claude", 3)


def test_runner_rejects_non_guidance_config_additions(tmp_path, monkeypatch):
    runner = _load_eval_runner()
    case = _runner_case(tmp_path)

    def add_data(project, case_name):
        (project / "override.yaml").write_text("value: 2\n", encoding="utf-8")

    monkeypatch.setitem(runner.CONFIGS, "baseline", add_data)
    monkeypatch.setitem(runner.HOSTS, "claude", lambda task: ("claude", task))
    monkeypatch.setattr(
        runner,
        "_invoke_host",
        lambda *args, **kwargs: pytest.fail("invalid config must not run a host"),
    )

    with pytest.raises(RuntimeError, match="changed seed or non-guidance files"):
        runner.run_once(case, "baseline", "claude", 3)


def test_runner_rejects_oracle_mutation_of_candidate_snapshot(tmp_path, monkeypatch):
    runner = _load_eval_runner()
    case = _runner_case(tmp_path)
    monkeypatch.setitem(runner.CONFIGS, "baseline", lambda project, case_name: None)
    monkeypatch.setitem(runner.HOSTS, "claude", lambda task: ("claude", task))
    monkeypatch.setattr(
        runner,
        "_invoke_host",
        lambda *args, **kwargs: (0, False, "done", "", False),
    )

    def mutating_oracle(project):
        (project / "oracle-output.txt").write_text("mutated", encoding="utf-8")
        return {"behavior": True, "norm": True}

    monkeypatch.setattr(runner, "load_oracle", lambda selected_case: mutating_oracle)

    with pytest.raises(RuntimeError, match="oracle modified candidate project"):
        runner.run_once(case, "baseline", "claude", 3)


def test_runner_real_oracle_preserves_candidate_snapshot(monkeypatch):
    runner = _load_eval_runner()
    monkeypatch.setattr(
        runner,
        "_invoke_host",
        lambda *args, **kwargs: (0, False, "done", "", False),
    )

    row = runner.run_once(LAYER_CASE, "baseline", "claude", 3)

    assert row["host_ok"] is True
    assert row["oracle_input_hash"] == row["oracle_output_hash"]
    assert len(row["task_sha256"]) == 64
    assert len(row["seed_sha256"]) == 64
    assert len(row["prepared_project_sha256"]) == 64
    assert len(row["oracle_sha256"]) == 64
    assert len(row["runner_sha256"]) == 64
    assert len(row["configs_sha256"]) == 64


def test_runner_bounds_captured_host_output(tmp_path):
    runner = _load_eval_runner()
    command = (
        sys.executable,
        "-c",
        f"import sys; sys.stdout.write('x' * {runner.MAX_HOST_OUTPUT_BYTES + 1_000})",
    )

    return_code, timed_out, stdout, stderr, truncated = runner._invoke_host(
        command,
        tmp_path,
        3,
    )

    assert return_code == 0
    assert timed_out is False
    assert len(stdout.encode()) == runner.MAX_HOST_OUTPUT_BYTES
    assert stderr == ""
    assert truncated is True


def test_runner_timeout_kills_host_process_group(tmp_path):
    runner = _load_eval_runner()
    marker = tmp_path / "escaped-child"
    child = (
        "import time\n"
        "from pathlib import Path\n"
        "time.sleep(1)\n"
        f"Path({str(marker)!r}).write_text('escaped', encoding='utf-8')\n"
    )
    parent = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "time.sleep(30)\n"
    )

    return_code, timed_out, *_ = runner._invoke_host(
        (sys.executable, "-c", parent),
        tmp_path,
        0.2,
    )
    time.sleep(1.2)

    assert return_code is None
    assert timed_out is True
    assert not marker.exists()


def test_runner_normal_exit_kills_lingering_host_children(tmp_path):
    runner = _load_eval_runner()
    marker = tmp_path / "escaped-child"
    child = (
        "import time\n"
        "from pathlib import Path\n"
        "time.sleep(1)\n"
        f"Path({str(marker)!r}).write_text('escaped', encoding='utf-8')\n"
    )
    parent = (
        "import subprocess, sys\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
    )

    return_code, timed_out, *_ = runner._invoke_host(
        (sys.executable, "-c", parent),
        tmp_path,
        3,
    )
    time.sleep(1.2)

    assert return_code == 0
    assert timed_out is False
    assert not marker.exists()


def test_runner_summary_excludes_invalid_hosts_from_denominators():
    runner = _load_eval_runner()
    summary = runner.summarize(
        [
            {"case": "example", "config": "baseline", "host_ok": True, "behavior": True, "norm": True},
            {"case": "example", "config": "baseline", "host_ok": False, "behavior": None, "norm": None},
            {"case": "example", "config": "baseline", "host_ok": True, "behavior": False, "norm": True},
        ]
    )

    assert summary == [
        {
            "case": "example",
            "config": "baseline",
            "attempted": 3,
            "n": 2,
            "invalid": 1,
            "behavior": 0.5,
            "norm": 1.0,
            "both": 0.5,
        }
    ]


def _complete_layer_rows(runner):
    shared = {
        "host": "claude",
        "host_executable": "/opt/bin/claude",
        "host_executable_sha256": "host-sha",
        "host_executable_sha256_after": "host-sha",
        "task_sha256": "task-sha",
        "seed_sha256": "seed-sha",
        "oracle_sha256": "oracle-sha",
        "runner_sha256": "runner-sha",
        "configs_sha256": "configs-sha",
    }
    return [
        {
            "case": "layer_boundary",
            "config": config,
            "trial": 0,
            "host_ok": True,
            "behavior": True,
            "norm": config != "baseline",
            "prepared_project_sha256": f"prepared:{config}",
            **shared,
        }
        for config in runner.CONFIGS
    ]


def test_layer_boundary_summary_resolves_only_complete_signal_matrix():
    runner = _load_eval_runner()
    rows = _complete_layer_rows(runner)

    summary = runner.summarize(rows)

    assert len(summary) == len(runner.CONFIGS)
    assert all(entry["resolution"] is True for entry in summary)
    assert all(entry["resolution_reason"] == "resolved" for entry in summary)
    assert next(
        entry for entry in summary if entry["config"] == "baseline"
    )["norm"] == 0.0


def test_layer_boundary_summary_nulls_each_unresolved_matrix_condition():
    runner = _load_eval_runner()
    complete = _complete_layer_rows(runner)
    incomplete = complete[:-1]
    uneven = [*complete, dict(complete[0])]
    invalid = [dict(row) for row in complete]
    invalid[0]["host_ok"] = False
    no_signal = [dict(row, norm=True) for row in complete]

    for rows, expected_reason in (
        (incomplete, "incomplete-config-matrix"),
        (uneven, "uneven-trial-matrix"),
        (invalid, "invalid-host-trials"),
        (no_signal, "baseline-no-boundary-signal"),
    ):
        summary = runner.summarize(rows)
        assert all(entry["resolution"] is False for entry in summary)
        assert all(entry["resolution_reason"] == expected_reason for entry in summary)
        assert all(entry["behavior"] is None for entry in summary)
        assert all(entry["norm"] is None for entry in summary)
        assert all(entry["both"] is None for entry in summary)


@pytest.mark.parametrize(
    "field",
    [
        "host",
        "host_executable",
        "host_executable_sha256",
        "host_executable_sha256_after",
        "task_sha256",
        "seed_sha256",
        "oracle_sha256",
        "runner_sha256",
        "configs_sha256",
    ],
)
def test_layer_boundary_summary_rejects_contract_hash_drift(field):
    runner = _load_eval_runner()
    rows = _complete_layer_rows(runner)
    rows[-1][field] = "drift"

    summary = runner.summarize(rows)

    assert all(entry["resolution"] is False for entry in summary)
    assert all(
        entry["resolution_reason"] == f"contract-drift:{field}"
        for entry in summary
    )
    assert all(entry["behavior"] is None for entry in summary)


def test_layer_boundary_summary_rejects_prepared_project_drift():
    runner = _load_eval_runner()
    first_trials = _complete_layer_rows(runner)
    second_trials = [{**row, "trial": 1} for row in first_trials]
    second_trials[0]["prepared_project_sha256"] = "drift"
    rows = [*first_trials, *second_trials]

    summary = runner.summarize(rows)

    assert all(entry["resolution"] is False for entry in summary)
    assert all(
        entry["resolution_reason"] == "prepared-project-drift:baseline"
        for entry in summary
    )
    assert all(entry["norm"] is None for entry in summary)


def test_runner_summarizes_each_case_independently():
    runner = _load_eval_runner()

    summary = runner.summarize(
        [
            {
                "case": "first",
                "config": "baseline",
                "host_ok": True,
                "behavior": True,
                "norm": False,
            },
            {
                "case": "second",
                "config": "baseline",
                "host_ok": True,
                "behavior": False,
                "norm": True,
            },
        ]
    )

    assert [(entry["case"], entry["behavior"], entry["norm"]) for entry in summary] == [
        ("first", 1.0, 0.0),
        ("second", 0.0, 1.0),
    ]


def test_runner_returns_nonzero_when_no_trial_has_a_valid_host(tmp_path, monkeypatch):
    runner = _load_eval_runner()
    cases = tmp_path / "cases"
    (cases / "example").mkdir(parents=True)
    results = tmp_path / "results"
    monkeypatch.setattr(runner, "CASES", cases)
    monkeypatch.setattr(runner, "RESULTS", results)
    monkeypatch.setattr(
        runner,
        "run_once",
        lambda *args, **kwargs: {
            "case": "example",
            "config": "baseline",
            "host": "claude",
            "host_ok": False,
            "return_code": None,
            "timed_out": True,
            "diagnostic": "timed out after 3s",
            "seconds": 3.0,
            "behavior": None,
            "norm": None,
        },
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["run.py", "--host", "claude", "--case", "example", "--config", "baseline", "--trials", "1"],
    )

    assert runner.main() == 1
    report_path = next(results.glob("*.json"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"][0]["n"] == 0
    assert report["summary"][0]["behavior"] is None


def test_layer_boundary_distinguishes_valid_target_from_sibling_call(tmp_path):
    violating = (
        "from shop.core.domain.get_cart_total import GetCartTotalUseCase\n"
        + COMPLIANT_CHECKOUT
    ).replace(
        "    def __call__(self) -> int:\n",
        "    def __call__(self) -> int:\n"
        "        GetCartTotalUseCase(self._cart_repository)()\n",
    )

    assert _score_layer(
        tmp_path / "valid",
        COMPLIANT_CHECKOUT,
    ) == {"behavior": True, "norm": True}
    assert _score_layer(
        tmp_path / "violating",
        violating,
    ) == {"behavior": True, "norm": False}


def test_layer_boundary_tracks_module_scope_kwargs_injection(tmp_path):
    dependencies = """from dataclasses import dataclass
from shop.core.data.repositories import InMemoryCartRepository, InMemoryDiscountRepository
from shop.core.domain.get_active_discount import GetActiveDiscountUseCase
from shop.core.domain.get_cart_total import GetCartTotalUseCase
from shop.core.domain.get_checkout_total import GetCheckoutTotalUseCase

CHECKOUT_KWARGS = {}


@dataclass(frozen=True)
class Dependencies:
    get_checkout_total: GetCheckoutTotalUseCase

    @classmethod
    def create(cls, total_cents: int, discount_percent: int) -> "Dependencies":
        global CHECKOUT_KWARGS
        CHECKOUT_KWARGS = {
            "get_cart_total": GetCartTotalUseCase(
                InMemoryCartRepository(total_cents)
            ),
            "get_active_discount": GetActiveDiscountUseCase(
                InMemoryDiscountRepository(discount_percent)
            ),
        }
        return cls(GetCheckoutTotalUseCase(**CHECKOUT_KWARGS))
"""

    assert _score_layer(
        tmp_path,
        UNTYPED_CHAINED_CHECKOUT,
        dependencies,
    ) == {"behavior": True, "norm": False}


def test_layer_boundary_allows_pure_local_repository_helper(tmp_path):
    dependencies = """from dataclasses import dataclass
from shop.core.data.repositories import InMemoryCartRepository, InMemoryDiscountRepository
from shop.core.domain.get_checkout_total import GetCheckoutTotalUseCase


def identity(value):
    return value


@dataclass(frozen=True)
class Dependencies:
    get_checkout_total: GetCheckoutTotalUseCase

    @classmethod
    def create(cls, total_cents: int, discount_percent: int) -> "Dependencies":
        return cls(
            GetCheckoutTotalUseCase(
                identity(InMemoryCartRepository(total_cents)),
                identity(InMemoryDiscountRepository(discount_percent)),
            )
        )
"""

    assert _score_layer(
        tmp_path,
        COMPLIANT_CHECKOUT,
        dependencies,
    ) == {"behavior": True, "norm": True}


def test_layer_boundary_distinguishes_bare_repository_and_usecase_classes(
    tmp_path,
):
    checkout = COMPLIANT_CHECKOUT.replace(
        "        discount_repository: DiscountRepository,\n",
        "        discount_repository: DiscountRepository,\n"
        "        dependency_type,\n",
    )
    valid = DIRECT_DEPENDENCIES.replace(
        "GetCheckoutTotalUseCase(cart_repository, discount_repository)",
        "GetCheckoutTotalUseCase(\n"
        "                cart_repository,\n"
        "                discount_repository,\n"
        "                InMemoryCartRepository,\n"
        "            )",
    )
    violating = DIRECT_DEPENDENCIES.replace(
        "GetCheckoutTotalUseCase(cart_repository, discount_repository)",
        "GetCheckoutTotalUseCase(\n"
        "                cart_repository,\n"
        "                discount_repository,\n"
        "                GetCartTotalUseCase,\n"
        "            )",
    )

    assert _score_layer(
        tmp_path / "valid",
        checkout,
        valid,
    ) == {"behavior": True, "norm": True}
    assert _score_layer(
        tmp_path / "violating",
        checkout,
        violating,
    ) == {"behavior": True, "norm": False}


MERGED_LAYER_RESULT = KIT_ROOT / "evals" / "results" / "20260726T233416.json"


def _eval_source_hash(source: str | None) -> str:
    if source is None:
        return "<absent>"
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def _layer_seed_sources() -> dict[str, str]:
    seed = LAYER_CASE / "seed"
    return {
        str(path.relative_to(seed)): path.read_text(encoding="utf-8")
        for path in sorted(seed.rglob("*.py"))
    }


def _parse_eval_diff(source_diff: str) -> list[dict]:
    entries: list[dict] = []
    for line in source_diff.splitlines():
        if line.startswith("diff --eval "):
            entries.append({"path": line[len("diff --eval "):], "patch": []})
        elif not entries:
            continue
        elif not entries[-1]["patch"] and line.startswith(
            ("status: ", "old-sha256: ", "new-sha256: ")
        ):
            key, value = line.split(": ", 1)
            entries[-1][key] = value
        elif not line.startswith(("--- ", "+++ ")):
            entries[-1]["patch"].append(line)
    return entries


def _apply_eval_patch(old_text: str, patch: list[str]) -> str:
    old = old_text.splitlines(keepends=True)
    restored: list[str] = []
    consumed = 0
    index = 0
    while index < len(patch):
        header = re.match(r"^@@ -(\d+)(?:,(\d+))? \+", patch[index])
        index += 1
        if header is None:
            continue
        start, count = int(header.group(1)), int(header.group(2) or 1)
        begin = start - 1 if count else start
        restored.extend(old[consumed:begin])
        consumed = begin
        while index < len(patch) and not patch[index].startswith("@@"):
            line = patch[index]
            index += 1
            if not line or line.startswith("\\"):
                continue
            if line.startswith(" "):
                restored.append(old[consumed])
                consumed += 1
            elif line.startswith("-"):
                consumed += 1
            elif line.startswith("+"):
                restored.append(line[1:] + "\n")
    restored.extend(old[consumed:])
    return "".join(restored)


def _restore_eval_candidate(project: Path, row: dict) -> None:
    seed_sources = _layer_seed_sources()
    shutil.copytree(LAYER_CASE / "seed", project)
    for entry in _parse_eval_diff(row["source_diff"]):
        path = project / entry["path"]
        old = seed_sources.get(entry["path"])
        assert _eval_source_hash(old) == entry["old-sha256"], (
            f"{entry['path']}: seed가 실측 실행 시점과 다르다"
        )
        if entry["status"] == "deleted":
            path.unlink()
            continue
        restored = _apply_eval_patch(old or "", entry["patch"])
        assert _eval_source_hash(restored) == entry["new-sha256"], (
            f"{entry['path']}: source_diff로 후보 소스를 복원하지 못했다"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(restored, encoding="utf-8")


def test_layer_boundary_oracle_reproduces_merged_run_norm_verdicts(tmp_path):
    """반증: 축소된 oracle이 실측 후보 소스에 다른 판정을 내면 머지된 norm 수치가 무효다."""
    oracle = _load_layer_oracle()
    rows = json.loads(MERGED_LAYER_RESULT.read_text(encoding="utf-8"))["rows"]
    assert len(rows) == 20

    replayed = []
    for index, row in enumerate(rows):
        project = tmp_path / f"row{index}"
        _restore_eval_candidate(project, row)
        violations = oracle._architecture_violations(project)
        replayed.append(
            {
                "norm": oracle._architecture_ok(violations),
                "norm_reasons": violations,
            }
        )

    assert replayed == [
        {"norm": row["norm"], "norm_reasons": row["norm_reasons"]}
        for row in rows
    ]


def test_runner_keeps_duplicate_host_output_in_memory(tmp_path):
    runner = _load_eval_runner()
    before = {
        path.relative_to(tmp_path)
        for path in tmp_path.rglob("*")
    }
    command = (
        sys.executable,
        "-c",
        (
            "import sys\n"
            f"payload = 'x' * {runner.MAX_HOST_OUTPUT_BYTES + 1_000}\n"
            "sys.stdout.write(payload)\n"
            "sys.stderr.write(payload)\n"
        ),
    )

    return_code, timed_out, stdout, stderr, truncated = runner._invoke_host(
        command,
        tmp_path,
        3,
    )

    after = {
        path.relative_to(tmp_path)
        for path in tmp_path.rglob("*")
    }
    assert return_code == 0
    assert timed_out is False
    assert len(stdout.encode()) == runner.MAX_HOST_OUTPUT_BYTES
    assert len(stderr.encode()) == runner.MAX_HOST_OUTPUT_BYTES
    assert truncated is True
    assert after == before


def test_eval_config_manifests_exclude_oracle_and_result_artifacts(tmp_path):
    sys.path.insert(0, str(KIT_ROOT / "evals"))
    from configs import CONFIGS

    forbidden_paths = {
        "check.py",
        "task.md",
        "gate-results.json",
        "multi-review.md",
        "architecture-review.md",
    }
    forbidden_text = (
        "norm_reasons",
        "usecase-injection:",
        "baseline-no-boundary-signal",
    )
    for config, apply_config in CONFIGS.items():
        project = tmp_path / config
        shutil.copytree(LAYER_CASE / "seed", project)
        apply_config(project, "layer_boundary")
        files = {
            str(path.relative_to(project)): path.read_bytes()
            for path in project.rglob("*")
            if path.is_file()
        }
        assert not forbidden_paths & {
            Path(path).name
            for path in files
        }
        rendered = "\n".join(
            content.decode("utf-8", errors="replace")
            for content in files.values()
        )
        assert not any(marker in rendered for marker in forbidden_text)
