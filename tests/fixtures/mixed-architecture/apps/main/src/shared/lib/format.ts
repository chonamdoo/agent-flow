import { locale } from '@/i18n/locale';

export function formatLabel(value: string): string {
  return `Main · ${value.trim().toLocaleUpperCase(locale)}`;
}
