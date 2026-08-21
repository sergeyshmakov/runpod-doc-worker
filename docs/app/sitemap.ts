import type { MetadataRoute } from 'next';
import { source } from '@/lib/source';
import { absoluteUrl } from '@/lib/site';

export const dynamic = 'force-static';

export default function sitemap(): MetadataRoute.Sitemap {
  return [
    {
      url: absoluteUrl('/').toString(),
      changeFrequency: 'monthly',
      priority: 1,
    },
    ...source.getPages().map((page) => ({
      url: absoluteUrl(page.url).toString(),
      changeFrequency: 'monthly' as const,
      priority: page.slugs.length === 0 ? 0.9 : 0.7,
    })),
  ];
}
