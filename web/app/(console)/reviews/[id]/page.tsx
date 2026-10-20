import { notFound } from "next/navigation";
import { PrDetailScreen } from "@/components/console/pr-detail-screen";
import { getReview } from "@/lib/api";

export default async function ReviewDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const detail = await getReview(id);
  if (!detail) notFound();
  return <PrDetailScreen detail={detail} />;
}
