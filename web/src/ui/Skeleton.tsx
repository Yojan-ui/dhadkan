// What the page looks like before the first state arrives: the layout, in flat grey blocks.
export function BootSkeleton({ phone, connected }: { phone: boolean; connected: boolean }) {
  return (
    <div className={`app ${phone ? "is-phone" : ""}`} aria-busy="true">
      <header className="top">
        <div className="brand">
          <b>Dhadkan</b>
          {!phone && <span className="muted small">machine sound watch · 3 sheds · 12 machines</span>}
        </div>
        <div className="skel" style={{ width: 220, height: 16 }} />
        <div className="skel" style={{ width: 260, height: 28 }} />
      </header>
      <main className="stage">
        {phone ? (
          <div className="phone">
            {[0, 1, 2].map((f) => (
              <section key={f}>
                <div className="skel" style={{ width: 220, height: 18, marginBottom: 8 }} />
                {[0, 1, 2, 3].map((m) => (
                  <div key={m} className="skel" style={{ height: 56, marginBottom: 4 }} />
                ))}
              </section>
            ))}
          </div>
        ) : (
          <div className="skel-sheds">
            {[0, 1, 2].map((i) => (
              <div key={i} className="skel skel-shed" />
            ))}
          </div>
        )}
        <div className="empty" role="status">
          <p>{connected ? "Loading the cluster…" : "Connecting to the Dhadkan server…"}</p>
          {!connected && (
            <p className="small muted">
              If this stays, the server is not running. Start everything with{" "}
              <span className="mono">make demo</span>.
            </p>
          )}
        </div>
      </main>
    </div>
  );
}
