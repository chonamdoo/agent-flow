from shop.core.domain.repositories import CartRepository


class GetCartTotalUseCase:
    def __init__(self, repository: CartRepository) -> None:
        self._repository = repository

    def __call__(self) -> int:
        return self._repository.get_total_cents()
