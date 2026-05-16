interface ApiOfflineStateProps {
  url?: string;
  message?: string;
}

// Rendered when the FastAPI service is unreachable. Tells the user how to
// start the backend; nothing on screen suggests that anything is broken
// in the app itself.
export function ApiOfflineState({ url, message }: ApiOfflineStateProps) {
  return (
    <main className="mx-auto max-w-[1280px] px-8 py-16">
      <p className="eyebrow mb-3">API offline</p>
      <h1 className="font-serif text-[32px] tracking-tightish text-ink-50">
        The local inference API is not responding.
      </h1>
      <p className="mt-4 max-w-[60ch] text-[13px] leading-relaxed text-ink-200">
        Neural Media reads predicted activations from a FastAPI service on{" "}
        <code className="font-mono text-ink-100">127.0.0.1:8000</code>. The
        frontend cannot reach it right now. Start the API and the mock sample
        outputs, then reload this page.
      </p>

      <div className="mt-10 grid gap-10 md:grid-cols-2">
        <section>
          <p className="eyebrow mb-2">1. Generate sample data</p>
          <pre className="font-mono text-[12px] text-ink-100 border border-line bg-surface/60 px-4 py-3 overflow-x-auto">
            <code>make sample</code>
          </pre>
          <p className="mt-3 max-w-[40ch] text-[12px] leading-relaxed text-ink-300">
            Builds the mock TRIBE outputs the API serves until real inference
            runs locally.
          </p>
        </section>
        <section>
          <p className="eyebrow mb-2">2. Start the API</p>
          <pre className="font-mono text-[12px] text-ink-100 border border-line bg-surface/60 px-4 py-3 overflow-x-auto">
            <code>make dev-api</code>
          </pre>
          <p className="mt-3 max-w-[40ch] text-[12px] leading-relaxed text-ink-300">
            Binds to <code className="font-mono">127.0.0.1:8000</code>. The
            web app proxies <code className="font-mono">/api/v1/*</code> there.
          </p>
        </section>
      </div>

      {(url || message) && (
        <div className="mt-12 border-t border-line pt-6 text-[11px] text-ink-400">
          <p className="eyebrow mb-2">Diagnostic</p>
          {url && (
            <p>
              <span className="text-ink-300">request: </span>
              <code className="font-mono">{url}</code>
            </p>
          )}
          {message && (
            <p className="mt-1">
              <span className="text-ink-300">reason: </span>
              <code className="font-mono">{message}</code>
            </p>
          )}
        </div>
      )}
    </main>
  );
}
