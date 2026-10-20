export default async function LoginPage({ searchParams }: {
  searchParams: Promise<{ error?: string }>;
}) {
  const { error } = await searchParams;
  return (
    <main className="flex min-h-screen items-center justify-center px-4 py-8">
      <section className="panel w-full max-w-sm p-7" aria-labelledby="login-title">
        <p className="eyebrow-wide mb-3">Argus console</p>
        <h1 id="login-title" className="screen-title">Sign in</h1>
        <p className="mt-2 text-sm text-dim">Enter your API token to open the console.</p>
        <form action="/api/login" method="post" className="mt-6 flex flex-col gap-3">
          <label htmlFor="token" className="text-sm font-medium text-text">API token</label>
          <input id="token" name="token" type="password" required autoComplete="current-password"
            className="min-h-11 rounded-lg border border-[var(--line-14)] bg-inset px-3 font-mono text-sm text-text outline-accent" />
          {error && <p role="alert" className="text-sm text-critical">The token was not accepted. Try again.</p>}
          <button type="submit" className="mt-2 min-h-11 rounded-lg bg-accent px-4 font-semibold text-ground focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-accent">
            Sign in
          </button>
        </form>
      </section>
    </main>
  );
}
