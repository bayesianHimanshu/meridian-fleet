// A hash or digest shown short, with the full value on hover and a copy button.
import { useState } from "react";

export function shorten(value: string): string {
  const body = value.startsWith("sha256:") ? value.slice(7) : value;
  const prefix = value.startsWith("sha256:") ? "sha256:" : "";
  return body.length <= 16 ? value : `${prefix}${body.slice(0, 8)}...${body.slice(-6)}`;
}

export function Hash({ value, label }: { value: string | null | undefined; label: string }) {
  const [copied, setCopied] = useState(false);
  if (!value) {
    return <span className="muted">not recorded</span>;
  }
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
      window.setTimeout(() => {
        setCopied(false);
      }, 1500);
    } catch {
      setCopied(false);
    }
  };
  return (
    <span className="hash">
      <span title={value} data-full={value}>
        {shorten(value)}
      </span>
      <button
        type="button"
        className="btn small"
        aria-label={`Copy full ${label}`}
        onClick={() => {
          void copy();
        }}
      >
        {copied ? "Copied" : "Copy"}
      </button>
    </span>
  );
}
