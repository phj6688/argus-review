import { MobileNav, Rail } from "@/components/console/rail";
import { TipProvider } from "@/components/ui/tip";
import { connection } from "next/server";

export default async function ConsoleLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  await connection();
  return (
    <div className="flex min-h-screen bg-ground">
      <Rail />
      <main className="min-w-0 flex-1 px-4 pt-6 pb-24 md:px-[34px] md:pt-8 md:pb-10">
        {process.env.ARGUS_AUTH_MODE !== "proxy" && (
          <form action="/api/logout" method="post" className="mb-4 flex justify-end">
            <button type="submit" className="min-h-11 rounded-lg border border-[var(--line)] px-3 text-sm text-dim hover:text-text focus-visible:outline-accent">
              Sign out
            </button>
          </form>
        )}
        {children}
      </main>
      <MobileNav />
      <TipProvider />
    </div>
  );
}
