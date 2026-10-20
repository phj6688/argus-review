import { OverviewScreen } from "@/components/console/overview-screen";
import { getReviews, getStats } from "@/lib/api";

export default async function OverviewPage() {
  const [stats, { reviews, live }] = await Promise.all([getStats(), getReviews()]);
  return <OverviewScreen stats={stats} recent={reviews.slice(0, 6)} live={live} />;
}
