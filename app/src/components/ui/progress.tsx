import { cn } from "@/lib/utils"

// Minimal shadcn-shaped Progress. Indeterminate when `value` is undefined,
// which is the case here: a grep across 1.5 GB has no measurable percentage.
export function Progress({
  value, className,
}: { value?: number; className?: string }) {
  return (
    <div
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={value}
      className={cn("relative h-1 w-full overflow-hidden rounded-full bg-muted",
                    className)}
    >
      <div
        className={cn("h-full bg-primary transition-all",
                      value === undefined && "animate-revive-indeterminate")}
        style={value === undefined ? undefined : { width: `${value}%` }}
      />
    </div>
  )
}
