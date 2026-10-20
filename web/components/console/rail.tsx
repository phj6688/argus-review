"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { type ComponentType, type SVGProps, useEffect, useState } from "react";
import {
  EyeMark,
  NavBenchmark,
  NavConfig,
  NavIntegrations,
  NavOverview,
  NavReports,
  NavReviews,
  NavTrigger,
} from "@/components/ui/icons";
import { getConfig } from "@/lib/api";
import { cn } from "@/lib/utils";

interface NavEntry {
  href: string;
  label: string;
  Icon: ComponentType<SVGProps<SVGSVGElement> & { size?: number }>;
  mobile: boolean;
}

const NAV: NavEntry[] = [
  { href: "/", label: "Overview", Icon: NavOverview, mobile: true },
  { href: "/trigger", label: "Trigger", Icon: NavTrigger, mobile: true },
  { href: "/reviews", label: "Reviews", Icon: NavReviews, mobile: true },
  { href: "/reports", label: "Reports", Icon: NavReports, mobile: true },
  { href: "/benchmark", label: "Benchmark", Icon: NavBenchmark, mobile: true },
  { href: "/config", label: "Config", Icon: NavConfig, mobile: true },
  { href: "/integrations", label: "Integrations", Icon: NavIntegrations, mobile: false },
];

function isActive(pathname: string, href: string): boolean {
  return href === "/" ? pathname === "/" : pathname.startsWith(href);
}

interface ActiveConfig {
  model: string;
  watched: number;
}

/* The configured deep-review model and watched-repo count, fetched once on mount.
   null until loaded or when the fetch fails: the rail then shows no readout
   rather than a made-up one. */
function useActiveConfig(): ActiveConfig | null {
  const [active, setActive] = useState<ActiveConfig | null>(null);
  useEffect(() => {
    let alive = true;
    getConfig()
      .then((c) => {
        if (!alive) return;
        const deep = c.models.find((m) => m.tier === "deep") ?? c.models[0];
        const watched = c.repos.filter((r) => r.watched).length;
        setActive({ model: deep?.model ?? "", watched });
      })
      .catch(() => {
        /* no readout on a fetch error */
      });
    return () => {
      alive = false;
    };
  }, []);
  return active;
}

export function Rail() {
  const pathname = usePathname();
  const active = useActiveConfig();
  return (
    <aside className="hidden w-[230px] shrink-0 flex-col gap-[5px] border-r border-[var(--line-07)] bg-rail px-4 py-6 md:flex">
      <div className="flex items-center gap-[11px] px-[10px] pt-1 pb-[22px]">
        <EyeMark size={26} />
        <div>
          <div className="text-[15px] font-semibold tracking-[0.04em] text-text">ARGUS</div>
          <div className="text-[10px] uppercase tracking-[0.14em] text-faint">code review</div>
        </div>
      </div>

      {NAV.map(({ href, label, Icon }) => {
        const active = isActive(pathname, href);
        return (
          <Link
            key={href}
            href={href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "relative flex items-center gap-3 rounded-[9px] px-3 py-[11px] text-sm font-medium transition-colors",
              active
                ? "bg-[var(--accent-10)] text-accent"
                : "text-dim hover:bg-[var(--bone-04)] hover:text-text",
            )}
          >
            {active && (
              <span className="absolute inset-y-[9px] left-0 w-[2px] rounded-[2px] bg-accent" />
            )}
            <Icon size={18} />
            {label}
          </Link>
        );
      })}

      <div className="flex-1" />

      <div className="border-t border-[var(--line-07)] px-3 pt-4 pb-1">
        <div className="mb-[10px] flex items-center gap-2">
          <span
            className="h-[7px] w-[7px] rounded-full bg-accent"
            style={{ boxShadow: "0 0 8px var(--accent-70)" }}
          />
          <span className="text-xs text-text">Reviewer online</span>
        </div>
        {active && (
          <>
            <div className="text-xs leading-[1.6] text-faint">
              {active.model ? `${active.model} · deep` : "model not set"}
            </div>
            <div className="mt-2 text-xs text-faint">{`${active.watched} repos watched`}</div>
          </>
        )}
      </div>
    </aside>
  );
}

export function MobileNav() {
  const pathname = usePathname();
  const items = NAV.filter((n) => n.mobile);
  return (
    <nav className="fixed inset-x-0 bottom-0 z-40 flex border-t border-[var(--line)] bg-rail px-[6px] pt-[6px] pb-[max(env(safe-area-inset-bottom),10px)] md:hidden">
      {items.map(({ href, label, Icon }) => {
        const active = isActive(pathname, href);
        return (
          <Link
            key={href}
            href={href}
            aria-current={active ? "page" : undefined}
            className={cn(
              "flex min-h-[44px] flex-1 flex-col items-center justify-center gap-1",
              active ? "text-accent" : "text-faint",
            )}
          >
            <Icon size={20} />
            <span className="text-[9.5px]">{label === "Benchmark" ? "Bench" : label}</span>
          </Link>
        );
      })}
    </nav>
  );
}
