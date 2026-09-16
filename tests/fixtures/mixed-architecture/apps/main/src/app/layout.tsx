import type { ReactNode } from 'react';
import { locale } from '@/i18n/locale';

type LayoutProps = {
  children: ReactNode;
};

export default function Layout({ children }: LayoutProps) {
  return (
    <html lang={locale}>
      <body>{children}</body>
    </html>
  );
}
