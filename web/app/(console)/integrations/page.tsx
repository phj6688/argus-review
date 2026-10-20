import { IntegrationsScreen } from "@/components/console/integrations-screen";
import { getIntegrations } from "@/lib/api";

export default async function IntegrationsPage() {
  const data = await getIntegrations();
  return <IntegrationsScreen data={data} />;
}
