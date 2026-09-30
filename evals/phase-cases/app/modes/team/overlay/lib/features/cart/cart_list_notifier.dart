import 'cart_list_state.dart';

class CartListNotifier {
  final List<void Function()> _listeners = [];
  final Map<String, int> _quantities = {};
  CartListState _state = const CartEmpty();

  CartListState get state => _state;

  void addListener(void Function() listener) => _listeners.add(listener);

  void removeListener(void Function() listener) => _listeners.remove(listener);

  void add(String sku) {
    _quantities.update(sku, (quantity) => quantity + 1, ifAbsent: () => 1);
    _publish();
  }

  void remove(String sku) {
    _quantities.remove(sku);
    _publish();
  }

  void _publish() {
    _state = _quantities.isEmpty
        ? const CartEmpty()
        : CartFilled([for (final entry in _quantities.entries) CartLine(sku: entry.key, quantity: entry.value)]);
    for (final listener in List.of(_listeners)) {
      listener();
    }
  }
}
