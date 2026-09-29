import 'dart:io';

import '../core/data/orders/http_orders_remote_source.dart';
import '../core/data/orders/order_repository_impl.dart';
import '../features/orders/presentation/orders_list_holder.dart';

OrdersListHolder createOrdersListHolder({required HttpClient httpClient, required Uri ordersUri}) =>
    OrdersListHolder(OrderRepositoryImpl(HttpOrdersRemoteSource(httpClient, ordersUri)));
