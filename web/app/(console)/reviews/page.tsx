import { ReviewsScreen } from "@/components/console/reviews-screen";
import { getReviews } from "@/lib/api";

export default async function ReviewsPage() {
  const { reviews, live } = await getReviews();
  return <ReviewsScreen reviews={reviews} live={live} />;
}
