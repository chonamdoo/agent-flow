import type { ComponentType } from 'react';
import type { OrderRepository } from '../../../core/domain/orders/OrderRepository.ts';

export type OrdersEntryDependencies = {
  orderRepository: OrderRepository;
};

export type OrdersEntry = ComponentType;

export type CreateOrdersEntry = (dependencies: OrdersEntryDependencies) => OrdersEntry;
