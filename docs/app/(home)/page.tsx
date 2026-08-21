import type { Metadata } from 'next';
import Link from 'next/link';
import { repositoryUrl, siteDescription } from '@/lib/site';

export const metadata: Metadata = {
  title: 'RunPod document worker building blocks',
  description: siteDescription,
  alternates: {
    canonical: '/',
  },
};

const installCommand =
  'runpod-doc-worker[s3] @ https://github.com/sergeyshmakov/runpod-doc-worker/archive/refs/tags/v0.1.0.tar.gz';

export default function HomePage() {
  return (
    <main className="relative flex flex-1 flex-col overflow-hidden">
      <div aria-hidden className="rdw-hero-grid pointer-events-none absolute inset-0 opacity-70" />
      <section className="relative mx-auto flex w-full max-w-6xl flex-1 flex-col justify-center px-6 py-20 sm:py-28">
        <p className="mb-5 w-fit rounded-full border bg-fd-card px-3 py-1 text-sm text-fd-muted-foreground">
          Python 3.10+ · v0.1.0 · pre-release
        </p>
        <h1 className="max-w-4xl text-balance text-4xl font-semibold tracking-tight sm:text-6xl">
          A shared harness for RunPod document workers
        </h1>
        <p className="mt-6 max-w-3xl text-pretty text-lg leading-8 text-fd-muted-foreground sm:text-xl">
          Resolve document inputs, check outbound targets, declare artifacts, package results, and emit
          structured diagnostics. Your worker still owns its engine, handler, public job schema, and deployment.
        </p>
        <div className="mt-9 flex flex-wrap gap-3">
          <Link
            href="/docs/"
            className="rounded-lg bg-fd-primary px-5 py-3 font-medium text-fd-primary-foreground transition-opacity hover:opacity-90"
          >
            Read the documentation
          </Link>
          <a
            href={repositoryUrl}
            rel="noreferrer noopener"
            target="_blank"
            className="rounded-lg border bg-fd-background px-5 py-3 font-medium transition-colors hover:bg-fd-accent"
          >
            View on GitHub
          </a>
        </div>

        <div className="mt-12 max-w-4xl rounded-xl border bg-fd-card/90 p-5 shadow-sm backdrop-blur">
          <div className="mb-3 flex items-center justify-between gap-4">
            <p className="font-medium">Pin the released tag</p>
            <a
              href={`${repositoryUrl}/releases/latest`}
              rel="noreferrer noopener"
              target="_blank"
              className="text-xs text-fd-muted-foreground underline-offset-4 hover:underline"
            >
              requirements.txt
            </a>
          </div>
          <pre className="overflow-x-auto text-sm leading-6 text-fd-muted-foreground">
            <code>{installCommand}</code>
          </pre>
        </div>

        <div className="mt-10 grid gap-4 md:grid-cols-3">
          <article className="rounded-xl border bg-fd-background/80 p-5">
            <h2 className="font-medium">Input transport</h2>
            <p className="mt-2 text-sm leading-6 text-fd-muted-foreground">
              Read URL, base64, or network-volume inputs with explicit limits and containment checks.
            </p>
          </article>
          <article className="rounded-xl border bg-fd-background/80 p-5">
            <h2 className="font-medium">Result packaging</h2>
            <p className="mt-2 text-sm leading-6 text-fd-muted-foreground">
              Return selected artifacts inline, as an archive, or through an optional S3 transport.
            </p>
          </article>
          <article className="rounded-xl border bg-fd-background/80 p-5">
            <h2 className="font-medium">Worker diagnostics</h2>
            <p className="mt-2 text-sm leading-6 text-fd-muted-foreground">
              Produce job-correlated logs and bounded GPU, cache, and filesystem diagnostics.
            </p>
          </article>
        </div>
      </section>
    </main>
  );
}
