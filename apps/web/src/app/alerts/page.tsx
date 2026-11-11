import WorkspaceSectionPage from "../../components/workspace-section-page";

export default function Page() {
  return (
    <WorkspaceSectionPage
      activeSection="alerts"
      eyebrow="MARKET NOTIFICATIONS"
      title="Alerts"
      description="Plan market-condition notifications for instruments and validated research signals."
      status="NOT CONFIGURED"
      message="Alert rules and notification delivery are not connected. No alert is claimed to be active."
      capabilities={[
      "Price-condition rules",
      "Indicator conditions",
      "Signal notifications",
      "Alert history"
      ]}
    />
  );
}