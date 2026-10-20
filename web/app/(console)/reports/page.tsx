import { ReportsScreen } from "@/components/console/reports-screen";
import { getReport } from "@/lib/api";

export default async function ReportsPage() {
  const [daily, weekly, monthly] = await Promise.all([
    getReport("daily"),
    getReport("weekly"),
    getReport("monthly"),
  ]);
  return <ReportsScreen reports={{ daily, weekly, monthly }} />;
}
