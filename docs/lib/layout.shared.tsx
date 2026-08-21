import type { BaseLayoutProps } from 'fumadocs-ui/layouts/shared';
import { repositoryUrl, siteName } from '@/lib/site';

export function baseOptions(): BaseLayoutProps {
  return {
    nav: {
      title: (
        <>
          <img aria-hidden="true" className="size-6" src="/icon.svg" />
          <span>{siteName}</span>
        </>
      ),
    },
    githubUrl: repositoryUrl,
    links: [
      {
        text: 'Documentation',
        url: '/docs/',
        active: 'nested-url',
      },
      {
        text: 'Releases',
        url: `${repositoryUrl}/releases`,
        external: true,
      },
    ],
  };
}
