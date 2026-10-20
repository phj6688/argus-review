"use client";

import * as RadixSwitch from "@radix-ui/react-switch";

export function Switch({
  checked,
  onCheckedChange,
  label,
}: {
  checked: boolean;
  onCheckedChange: (v: boolean) => void;
  label?: string;
}) {
  return (
    <RadixSwitch.Root
      checked={checked}
      onCheckedChange={onCheckedChange}
      aria-label={label}
      className="argus-switch"
      style={{
        width: 38,
        height: 22,
        borderRadius: 12,
        position: "relative",
        flex: "0 0 auto",
        cursor: "pointer",
        border: "none",
        padding: 0,
        background: checked ? "var(--accent-85)" : "var(--bone-10)",
        transition: "background .15s ease",
      }}
    >
      <RadixSwitch.Thumb
        style={{
          display: "block",
          width: 16,
          height: 16,
          borderRadius: "50%",
          background: checked ? "var(--color-ground)" : "var(--color-faint)",
          position: "absolute",
          top: 3,
          left: 0,
          transform: checked ? "translateX(19px)" : "translateX(3px)",
          transition: "transform .15s ease, background .15s ease",
        }}
      />
    </RadixSwitch.Root>
  );
}
