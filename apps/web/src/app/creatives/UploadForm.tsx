"use client";

/**
 * The upload form - the one client component in this app, and the reason it
 * has to be one.
 *
 * A video ad can be hundreds of megabytes. It must go from the browser to
 * Supabase Storage directly, on a one-shot URL the runtime signed for exactly
 * that object path, and never through the Next server: this process holds no
 * Storage credential and should not become a 4 GB relay. So the sequence is
 *
 *   1. signUpload (Server Action)      - the runtime writes the row and signs
 *   2. fetch PUT to Storage (here)     - the bytes, with their Content-Type
 *   3. analyseCreative (Server Action) - the runtime rates it, 10-60 s
 *   4. navigate to /creatives/[id]
 *
 * Nothing here decides anything. The workspace is re-proved inside each
 * action and again inside the runtime; the Storage token is bound to one path
 * and expires. What this component owns is the busy state and the error text,
 * shown verbatim, because a 503 that says "SUPABASE_SERVICE_ROLE_KEY is not
 * configured" is the sentence the owner needs to relay to the operator.
 */

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";

import { analyseCreative, signUpload } from "./actions";

const ACCEPT = "video/mp4,video/quicktime,video/webm,image/jpeg,image/png,image/webp";

const OBJECTIVES = [
  { value: "conversion", label: "Conversion (10-45 s)" },
  { value: "consideration", label: "Consideration (12-30 s)" },
  { value: "awareness", label: "Awareness (4-15 s)" },
] as const;

type Phase =
  | { kind: "idle" }
  | { kind: "signing" }
  | { kind: "uploading" }
  | { kind: "analysing"; creativeId: string }
  | { kind: "done"; creativeId: string }
  | { kind: "failed"; error: string; creativeId?: string };

const input =
  "mt-1 w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm dark:border-slate-700 dark:bg-slate-900";

function describeFetchFailure(url: string, cause: unknown): string {
  let origin = url;
  try {
    origin = new URL(url).origin;
  } catch {
    // an unparseable URL is reported as given
  }
  const reason = cause instanceof Error ? cause.message : String(cause);
  return (
    `The browser could not send the file to ${origin} (${reason}). ` +
    "Either that host is not reachable from here, or Storage did not answer the " +
    "CORS preflight for a PUT from this site."
  );
}

export function UploadForm() {
  const router = useRouter();
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });

  const busy =
    phase.kind === "signing" || phase.kind === "uploading" || phase.kind === "analysing";

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;

    // Captured before the first await: currentTarget is only set while the
    // event is being dispatched.
    const form = event.currentTarget;
    const data = new FormData(form);
    const file = data.get("file");
    const objective = String(data.get("objective") ?? "conversion");

    if (!(file instanceof File) || file.size === 0) {
      setPhase({ kind: "failed", error: "Choose a file first." });
      return;
    }

    // 1. Declare it; the runtime writes the row and signs the URL.
    setPhase({ kind: "signing" });
    const signed = await signUpload({
      name: file.name,
      size: file.size,
      mime: file.type,
      productHint: String(data.get("product_sku") ?? ""),
      aiGenerated: data.get("ai_generated") === "on",
    });
    if (!signed.ok) {
      setPhase({ kind: "failed", error: signed.error });
      return;
    }
    const { creative_id: creativeId, upload } = signed.value;

    // 2. The bytes, browser to Storage. The token is in the URL the runtime
    //    returned; the only header is the Content-Type the upload was declared
    //    with, which the bucket checks against its allowed types.
    setPhase({ kind: "uploading" });
    let put: Response;
    try {
      put = await fetch(upload.url, { method: upload.method, headers: upload.headers, body: file });
    } catch (cause) {
      setPhase({ kind: "failed", error: describeFetchFailure(upload.url, cause), creativeId });
      return;
    }
    if (!put.ok) {
      const body = (await put.text().catch(() => "")).slice(0, 300);
      setPhase({
        kind: "failed",
        error: `Storage refused the upload: HTTP ${put.status}${body ? ` ${body}` : ""}`,
        creativeId,
      });
      return;
    }

    // 3. Rate it. This is the slow step and the one that can say 503. Only a
    //    video is rated today - the runtime refuses an image with a 422 - so
    //    an image stops here, stored, and its page says why there is no
    //    rating rather than this form reporting a refusal it asked for.
    if (file.type.startsWith("video/")) {
      setPhase({ kind: "analysing", creativeId });
      const analysed = await analyseCreative(creativeId, objective);
      if (!analysed.ok) {
        setPhase({ kind: "failed", error: analysed.error, creativeId });
        return;
      }
    }

    // 4. The rating lives on its own page.
    setPhase({ kind: "done", creativeId });
    form.reset();
    router.push(`/creatives/${creativeId}`);
  }

  return (
    <form onSubmit={onSubmit} className="space-y-3" aria-busy={busy}>
      <label className="block text-sm">
        <span className="text-slate-600 dark:text-slate-400">Video or image</span>
        <input
          name="file"
          type="file"
          required
          accept={ACCEPT}
          disabled={busy}
          className={`${input} file:mr-3 file:rounded file:border-0 file:bg-slate-100 file:px-3 file:py-1 file:text-sm dark:file:bg-slate-800`}
        />
        <span className="mt-1 block text-xs text-slate-500 dark:text-slate-500">
          MP4, MOV or WebM up to 4 GB. Only video is rated for now; images are stored.
        </span>
      </label>

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="block text-sm">
          <span className="text-slate-600 dark:text-slate-400">Objective</span>
          <select name="objective" defaultValue="conversion" disabled={busy} className={input}>
            {OBJECTIVES.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </label>

        <label className="block text-sm">
          <span className="text-slate-600 dark:text-slate-400">Product SKU (optional)</span>
          <input
            name="product_sku"
            maxLength={120}
            placeholder="as listed in your catalogue"
            disabled={busy}
            className={input}
          />
        </label>
      </div>

      <label className="flex items-start gap-2 text-sm">
        <input name="ai_generated" type="checkbox" disabled={busy} className="mt-1" />
        <span className="text-slate-600 dark:text-slate-400">
          This creative was generated or materially altered by AI. Recorded with the
          creative, because Meta requires the disclosure when it runs as an ad.
        </span>
      </label>

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="submit"
          disabled={busy}
          className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:cursor-wait disabled:opacity-60 dark:bg-slate-100 dark:text-slate-900"
        >
          {busy ? "Working…" : "Upload and rate"}
        </button>
        <Status phase={phase} />
      </div>
    </form>
  );
}

/** Busy and failed states are announced, not merely coloured. */
function Status({ phase }: { phase: Phase }) {
  switch (phase.kind) {
    case "idle":
      return null;
    case "signing":
      return <p className="text-sm text-slate-500 dark:text-slate-400" aria-live="polite">Step 1 of 3 · registering the upload…</p>;
    case "uploading":
      return <p className="text-sm text-slate-500 dark:text-slate-400" aria-live="polite">Step 2 of 3 · sending the file to storage…</p>;
    case "analysing":
      return (
        <p className="text-sm text-slate-500 dark:text-slate-400" aria-live="polite">
          Step 3 of 3 · rating it. Frames are read by ffmpeg, then judged. Ten to sixty seconds.
        </p>
      );
    case "done":
      return <p className="text-sm text-slate-500 dark:text-slate-400" aria-live="polite">Rated. Opening the report…</p>;
    case "failed":
      return (
        <div
          role="alert"
          className="basis-full rounded-md border border-red-300 bg-red-50 p-3 text-sm dark:border-red-900 dark:bg-red-950/40"
        >
          <p>
            <span aria-hidden="true">▲ </span>
            <span className="font-medium">Refused: </span>
            {phase.error}
          </p>
          {phase.creativeId && (
            <p className="mt-1 text-xs text-slate-600 dark:text-slate-400">
              The creative was registered as{" "}
              <Link href={`/creatives/${phase.creativeId}`} className="underline underline-offset-2">
                {phase.creativeId.slice(0, 8)}
              </Link>
              ; you can try the rating again from there.
            </p>
          )}
        </div>
      );
  }
}
