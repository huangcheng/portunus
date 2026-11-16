import { tokenizeSetupHint } from "./hintTokens";

/** Render catalog `setup_hints` as provider-hint paragraphs with safe anchors. */
export function SetupHints({ hints }: { hints: string[] }) {
  if (!hints.length) return null;
  return (
    <>
      {hints.map((hint, i) => (
        <p className="provider-hint" key={`setup-hint-${i}`}>
          {tokenizeSetupHint(hint).map((seg, j) =>
            seg.kind === "link" ? (
              <a key={j} href={seg.href} target="_blank" rel="noreferrer">
                {seg.label}
              </a>
            ) : (
              <span key={j}>{seg.text}</span>
            ),
          )}
        </p>
      ))}
    </>
  );
}
