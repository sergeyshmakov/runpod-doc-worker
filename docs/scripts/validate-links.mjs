import path from 'node:path';
import { getTableOfContents } from 'fumadocs-core/content/toc';
import { getSlugs } from 'fumadocs-core/source';
import {
  printErrors,
  readFiles,
  scanURLs,
  validateFiles,
} from 'next-validate-link';

const docsFiles = await readFiles('content/docs/**/*.{md,mdx}');
const scanned = await scanURLs({
  pages: [path.join('docs', '[[...slug]]', 'page.tsx')],
  populate: {
    'docs/[[...slug]]': docsFiles.map((file) => ({
      value: getSlugs(
        path.relative('content/docs', file.path).split(path.sep).join('/'),
      ),
      hashes: getTableOfContents(file.content).map((item) => item.url.slice(1)),
    })),
  },
});

for (const [url, metadata] of scanned.urls) {
  if (url !== '/' && !url.endsWith('/')) {
    scanned.urls.set(`${url}/`, metadata);
  }
}

printErrors(
  await validateFiles(docsFiles, {
    scanned,
  }),
  true,
);
