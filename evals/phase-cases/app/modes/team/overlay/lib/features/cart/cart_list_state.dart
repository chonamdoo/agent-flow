class CartLine {
  const CartLine({required this.sku, required this.quantity});

  final String sku;
  final int quantity;
}

sealed class CartListState {
  const CartListState();
}

final class CartEmpty extends CartListState {
  const CartEmpty();
}

final class CartFilled extends CartListState {
  const CartFilled(this.lines);

  final List<CartLine> lines;
}
