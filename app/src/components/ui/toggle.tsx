import * as React from "react"
import { cn } from "@/lib/utils"

// Minimal shadcn-shaped Toggle: a pressed/unpressed icon button.
export function Toggle({
  pressed, onPressedChange, className, children, ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  pressed?: boolean
  onPressedChange?: (v: boolean) => void
}) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      data-state={pressed ? "on" : "off"}
      onClick={e => {
        // Secondary action inside a clickable card: never let it select the row.
        e.preventDefault()
        e.stopPropagation()
        onPressedChange?.(!pressed)
      }}
      className={cn(
        "inline-flex h-7 w-7 items-center justify-center rounded-md transition-colors",
        pressed ? "text-foreground" : "text-muted-foreground/40 hover:text-muted-foreground",
        className)}
      {...props}
    >
      {children}
    </button>
  )
}
