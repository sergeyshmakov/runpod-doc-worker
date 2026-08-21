export const siteName = 'runpod-doc-worker';
export const siteDescription =
  'Documentation for the engine-agnostic Python harness used to compose RunPod document-processing workers.';
export const siteUrl = 'https://rdw.shmakov.tools';
export const repositoryUrl = 'https://github.com/sergeyshmakov/runpod-doc-worker';

export function absoluteUrl(path: string): URL {
  const url = new URL(path, siteUrl);
  if (!url.pathname.endsWith('/') && !url.pathname.includes('.')) {
    url.pathname += '/';
  }
  return url;
}
