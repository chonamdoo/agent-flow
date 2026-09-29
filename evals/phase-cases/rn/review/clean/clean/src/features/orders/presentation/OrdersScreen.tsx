import { useEffect, useSyncExternalStore } from 'react';
import {
  AccessibilityInfo,
  ActivityIndicator,
  FlatList,
  Pressable,
  StyleSheet,
  Text,
  View,
  type ListRenderItemInfo,
} from 'react-native';
import type { OrderRowUiModel, OrdersUiState } from './OrdersUiState.ts';
import type { OrdersScreenStore } from './ordersScreenStore.ts';

export function OrdersScreen({ store }: { store: OrdersScreenStore }) {
  const state = useSyncExternalStore(store.subscribe, store.getState);
  const announcement = announcementFor(state);

  useEffect(() => {
    void store.load();
    return () => store.cancel();
  }, [store]);

  // Screen readers are not told when the loading indicator is replaced, so each load outcome is announced.
  useEffect(() => {
    if (announcement !== null) {
      AccessibilityInfo.announceForAccessibility(announcement);
    }
  }, [announcement]);

  switch (state.status) {
    case 'loading':
      return <ActivityIndicator accessibilityRole="progressbar" accessibilityLabel="Loading orders" />;
    case 'empty':
      return <Text>{state.message}</Text>;
    case 'error':
      return (
        <View>
          <Text accessibilityRole="alert">{state.message}</Text>
          <Pressable accessibilityRole="button" style={styles.retry} onPress={() => void store.load()}>
            <Text>Try again</Text>
          </Pressable>
        </View>
      );
    case 'content':
      return <FlatList data={state.rows} keyExtractor={rowKey} renderItem={renderRow} />;
  }
}

function announcementFor(state: OrdersUiState): string | null {
  switch (state.status) {
    case 'loading':
      return null;
    case 'content':
      return state.rows.length === 1 ? '1 order loaded' : `${state.rows.length} orders loaded`;
    case 'empty':
    case 'error':
      return state.message;
  }
}

const rowKey = (row: OrderRowUiModel) => row.id;

function renderRow({ item }: ListRenderItemInfo<OrderRowUiModel>) {
  return (
    <View accessible accessibilityLabel={`Order ${item.id}, ${item.totalLabel}, ${item.statusLabel}`}>
      <Text>{item.id}</Text>
      <Text>{item.totalLabel}</Text>
      <Text>{item.statusLabel}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  retry: { minHeight: 44, minWidth: 44, justifyContent: 'center' },
});
