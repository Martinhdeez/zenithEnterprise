/**
 * A link the server will not show again.
 *
 * There is no outbound mail on an on-premise install, so an administrator passes this on
 * exactly the way they used to pass a password — and what changed is that this one is single
 * use and expires, so the same paste in the same chat window is worthless afterwards.
 *
 * Shared by inviting somebody and by resetting their password, because those differ only in
 * the sentence at the top. Duplicated, the two would drift: one would keep the expiry, the
 * other would lose it, and the half that matters is the one nobody would notice going.
 */

import { Button } from "@/components/ui/button";
import { absoluteLink, type Invitation } from "../api";
import { useT } from "@/shared/i18n/useT";

export function SingleUseLink({
  issued,
  headline,
  onDone,
}: {
  issued: Invitation;
  /** What this link is for, in one sentence. The rest of the panel is the same either way. */
  headline: string;
  onDone: () => void;
}) {
  const t = useT();
  const link = absoluteLink(issued.path);

  return (
    <div className="space-y-3 rounded-lg border border-zenith-amber/30 bg-zenith-amber/10 p-4 text-sm">
      <p className="font-medium text-foreground">{headline}</p>
      {/* Selectable and wrapped rather than truncated: the whole point is that it gets
          copied, and a link with an ellipsis in the middle cannot be. `navigator.clipboard`
          is also absent on an insecure origin, which an on-premise install often is. */}
      <code className="block break-all rounded bg-background px-2 py-1.5 font-mono text-xs text-foreground">
        {link}
      </code>
      <p className="text-muted-foreground">
        They choose their own password. The link works once and expires{" "}
        {new Date(issued.expires_at).toLocaleString(undefined, {
          day: "2-digit",
          month: "short",
          hour: "2-digit",
          minute: "2-digit",
        })}
        , so it stops being a way in if it is left in a chat window.
      </p>
      <div className="flex gap-2">
        <Button
          type="button"
          onClick={() => void navigator.clipboard?.writeText(link)}
          className="rounded-md"
        >{t("Copy link")}</Button>
        <Button
          type="button"
          variant="outline"
          onClick={onDone}
          className="border-zenith-amber/40 text-foreground hover:bg-zenith-amber/10"
        >{t("Done")}</Button>
      </div>
    </div>
  );
}
