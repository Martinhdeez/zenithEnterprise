/**
 * Bringing somebody into the organisation.
 *
 * The link is returned once and is not recoverable, which is what shapes this screen: it
 * *replaces* the form rather than sitting beside it. An administrator who closes it without
 * copying has to invite again, and that is only cheap if nothing else is competing for their
 * attention at the moment it appears.
 */

import { useEffect, useState } from "react";

import { absoluteLink, inviteUser, roles as fetchRoles, type Invitation, type Role } from "../api";
import { ApiError } from "@/shared/api/http";
import { FIELD } from "../fieldStyle";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export function InvitePanel({ token }: { token: string }) {
  const [email, setEmail] = useState("");
  // "none" rather than "" — Radix `Select.Item` refuses an empty-string value, since that's
  // reserved to mean "cleared" internally.
  const [roleId, setRoleId] = useState("none");
  const [available, setAvailable] = useState<Role[]>([]);
  const [issued, setIssued] = useState<Invitation | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    void fetchRoles(token)
      .then(setAvailable)
      .catch(() => setAvailable([]));
  }, [token]);

  if (issued) {
    // The link is returned once and is not recoverable, so this replaces the form rather
    // than sitting beside it — an administrator who closes it without copying has to invite
    // again, and that is only cheap if nothing else is competing for their attention.
    const link = absoluteLink(issued.path);
    return (
      <div className="space-y-3 rounded-lg border border-zenith-amber/30 bg-zenith-amber/10 p-4 text-sm">
        <p className="font-medium text-foreground">Send this link to {issued.email}.</p>
        {/* Selectable and wrapped rather than truncated: the whole point is that it gets
            copied, and a link with an ellipsis in the middle cannot be. */}
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
            onClick={() => void navigator.clipboard.writeText(link)}
            className="rounded-md"
          >
            Copy link
          </Button>
          <Button
          type="button"
          variant="outline"
          onClick={() => setIssued(null)}
          className="border-zenith-amber/40 text-foreground hover:bg-zenith-amber/10"
        >
          Done
          </Button>
        </div>
      </div>
    );
  }

  return (
    <form
      onSubmit={async (event) => {
        event.preventDefault();
        setError(null);
        try {
          setIssued(await inviteUser(token, email, roleId === "none" ? [] : [roleId]));
          setEmail("");
          setRoleId("none");
        } catch (caught) {
          // Shown verbatim: "that address is already a user here" is actionable, and a
          // generic failure is not.
          setError(caught instanceof ApiError ? caught.message : "The invitation failed.");
        }
      }}
      className="flex flex-wrap items-end gap-3"
    >
      <div className="min-w-56 flex-1 space-y-1.5">
        <Label htmlFor="invite-email" className="text-sm text-foreground/80">
          Email
        </Label>
        <Input
          id="invite-email"
          type="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          required
          className={`h-10 ${FIELD}`}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="invite-role" className="text-sm text-foreground/80">
          Role
        </Label>
        <Select value={roleId} onValueChange={setRoleId}>
          <SelectTrigger id="invite-role" className={`h-10 w-40 ${FIELD}`}>
            <SelectValue placeholder="none" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="none">none</SelectItem>
            {available.map((role) => (
              <SelectItem key={role.id} value={role.id}>
                {role.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <Button type="submit" className="h-10 bg-primary text-white hover:bg-primary/90">
        Invite
      </Button>

      {error && (
        <p role="alert" className="w-full text-sm text-destructive">
          {error}
        </p>
      )}
    </form>
  );
}
