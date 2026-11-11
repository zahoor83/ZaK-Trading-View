import WorkspaceShell, {
  type WorkspaceSection,
} from "./workspace-shell";

interface WorkspaceSectionPageProps {
  activeSection: WorkspaceSection;
  eyebrow: string;
  title: string;
  description: string;
  status: string;
  message: string;
  capabilities: string[];
}

export default function WorkspaceSectionPage({
  activeSection,
  eyebrow,
  title,
  description,
  status,
  message,
  capabilities,
}: WorkspaceSectionPageProps) {
  return (
    <WorkspaceShell activeSection={activeSection}>
      <div className="dashboard-content">
        <section className="section-intro">
          <div className="eyebrow">{eyebrow}</div>
          <h1>{title}</h1>
          <p className="welcome-description">{description}</p>
        </section>

        <section className="section-status panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">Service status</div>
              <div className="panel-subtitle">
                Current verified application capability
              </div>
            </div>
            <span className="panel-badge">{status}</span>
          </div>

          <div className="section-empty" role="status">
            <div className="empty-chart-icon">⌁</div>
            <h2>Not available yet</h2>
            <p>{message}</p>
          </div>
        </section>

        <section className="section-capabilities">
          <h2>Workspace capabilities</h2>
          <div className="section-capability-grid">
            {capabilities.map((capability) => (
              <article className="metric-card" key={capability}>
                <div className="metric-heading">PLANNED CAPABILITY</div>
                <div className="capability-name">{capability}</div>
                <div className="metric-detail">Implementation pending</div>
              </article>
            ))}
          </div>
        </section>

        <footer className="dashboard-footer">
          <span>ZaK Trading View</span>
          <span>
            Signal research and simulated trading only · No real-order execution
          </span>
        </footer>
      </div>
    </WorkspaceShell>
  );
}