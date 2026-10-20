import { ConfigScreen } from "@/components/console/config-screen";
import { getConfig } from "@/lib/api";

export default async function ConfigPage() {
  const config = await getConfig();
  return <ConfigScreen config={config} />;
}
