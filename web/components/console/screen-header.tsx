import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export function ScreenHeader({
  title,
  subtitle,
  controls,
  className,
}: {
  title: ReactNode;
  subtitle: ReactNode;
  controls?: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn(
        "mb-6 flex flex-col gap-4 sm:flex-row sm:items-start sm:justify-between sm:gap-5",
        className,
      )}
    >
      <div>
        <h2 className="screen-title mb-[5px]">{title}</h2>
        <div className="text-[13px] text-dim">{subtitle}</div>
      </div>
      {controls ? <div className="flex items-center gap-[10px]">{controls}</div> : null}
    </div>
  );
}
