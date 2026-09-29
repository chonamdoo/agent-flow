export class OrdersUnavailableError extends Error {
  constructor() {
    super('Orders are unavailable');
    this.name = 'OrdersUnavailableError';
  }
}
