import { notFound } from "next/navigation";
import { PrDetailScreen } from "@/components/console/pr-detail-screen";
import { isGitLabKey, keyHost } from "@/lib/forge";
import { getIntegrations, getReview } from "@/lib/api";

export default async function ReviewDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  const detail = await getReview(id);
  if (!detail) notFound();
  let gitlabBaseUrl: string | null = null;
  if (isGitLabKey(detail.repo)) {
    try {
      const gitlab = (await getIntegrations()).gitlab;
      if (gitlab && keyHost(detail.repo)?.toLowerCase() === gitlab.host.toLowerCase()) {
        gitlabBaseUrl = gitlab.baseUrl;
      }
    }
    catch { /* The review remains readable when settings are unavailable. */ }
  }
  return <PrDetailScreen detail={detail} gitlabBaseUrl={gitlabBaseUrl} />;
}
