from shop.core.domain.repositories import DiscountRepository


class GetActiveDiscountUseCase:
    def __init__(self, repository: DiscountRepository) -> None:
        self._repository = repository

    def __call__(self) -> int:
        return self._repository.get_active_percent()
