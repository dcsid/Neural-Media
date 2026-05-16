// SCAFFOLD STUB.
// Owner: frontend-dashboard worker. See docs/worker-briefs/frontend-dashboard.md.
// The Dashboard view is built here. The brain-viz worker plugs in the 3D
// cortical mesh via a component owned by that brief.

export default function Page() {
  return (
    <main className="mx-auto max-w-[1280px] px-8 py-10">
      <p className="eyebrow mb-3">Neural Media</p>
      <h1 className="font-serif text-[36px] tracking-tightish text-ink-50">
        Scaffold ready.
      </h1>
      <p className="mt-4 max-w-[60ch] text-[13px] text-ink-200">
        This page is intentionally empty. The frontend-dashboard worker
        replaces it with the dashboard view defined in{" "}
        <code className="font-mono text-ink-100">
          docs/worker-briefs/frontend-dashboard.md
        </code>
        .
      </p>
    </main>
  );
}
