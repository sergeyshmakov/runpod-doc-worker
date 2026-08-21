import type { Metadata } from 'next';
import { Provider } from '@/components/provider';
import { siteDescription, siteName, siteUrl } from '@/lib/site';
import './global.css';

export const metadata: Metadata = {
  metadataBase: new URL(siteUrl),
  title: {
    default: `${siteName} documentation`,
    template: `%s | ${siteName}`,
  },
  description: siteDescription,
  alternates: {
    canonical: '/',
  },
  openGraph: {
    type: 'website',
    siteName,
    title: `${siteName} documentation`,
    description: siteDescription,
    url: '/',
  },
  twitter: {
    card: 'summary',
    title: `${siteName} documentation`,
    description: siteDescription,
  },
};

export default function RootLayout({ children }: LayoutProps<'/'>) {
  return (
    <html lang="en" suppressHydrationWarning>
      <body className="flex min-h-screen flex-col antialiased">
        <Provider>{children}</Provider>
      </body>
    </html>
  );
}
