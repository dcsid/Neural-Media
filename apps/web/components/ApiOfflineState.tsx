import { EmptyState, EmptyStateStep } from "./EmptyState";

interface ApiOfflineStateProps {
  url?: string;
  message?: string;
}

// Rendered when the FastAPI service is unreachable. Tells the user how to
// start the backend; nothing on screen suggests that anything is broken
// in the app itself.
export function ApiOfflineState({ url, message }: ApiOfflineStateProps) {
  return (
    <EmptyState
      eyebrow="API offline"
      headline="The local inference API is not responding."
      cta={
        <>
          <EmptyStateStep
            index={1}
            label="Generate sample data"
            command="make sample"
            hint="Builds the mock TRIBE outputs the API serves until real inference runs locally."
          />
          <EmptyStateStep
            index={2}
            label="Start the API"
            command="make dev-api"
            hint={
              <>
                Binds to <code className="font-mono">127.0.0.1:8000</code>. The
                web app proxies <code className="font-mono">/api/v1/*</code>{" "}
                there.
              </>
            }
          />
        </>
      }
      diagnostic={
        url || message ? (
          <>
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
          </>
        ) : null
      }
    >
      Neural Media reads predicted activations from a FastAPI service on{" "}
      <code className="font-mono text-ink-100">127.0.0.1:8000</code>. The
      frontend cannot reach it right now. Start the API and the mock sample
      outputs, then reload this page.
    </EmptyState>
  );
}
