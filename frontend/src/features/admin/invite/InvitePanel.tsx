/**
 * Bringing somebody into the organisation.
 *
 * The link is returned once and is not recoverable, which is what shapes this screen: it
 * *replaces* the form rather than sitting beside it. An administrator who closes it without
 * copying has to invite again, and that is only cheap if nothing else is competing for their
 * attention at the moment it appears.
 */

import { useEffect, useState } from "react";

import { inviteUser, roles as fetchRoles, type Invitation, type Role } from "../api";
import { ApiError } from "@/shared/api/http";
import { SingleUseLink } from "./SingleUseLink";
import { FIELD } from "../fieldStyle";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useT } from "@/shared/i18n/useT";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export function InvitePanel({ token }: { token: string }) {
  const t = useT();
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
    // Replaces the form rather than sitting beside it: an administrator who closes it without
    // copying has to invite again, and that is only cheap if nothing else is competing for
    // their attention at the moment it appears.
    return (
      <SingleUseLink
        issued={issued}
        headline={`Send this link to ${issued.email}.`}
        onDone={() => setIssued(null)}
      />
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
        <Label htmlFor="invite-email" className="text-sm text-foreground/80">{t("Email")}</Label>
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
        <Label htmlFor="invite-role" className="text-sm text-foreground/80">{t("Role")}</Label>
        <Select value={roleId} onValueChange={setRoleId}>
          <SelectTrigger id="invite-role" className={`h-10 w-40 ${FIELD}`}>
            <SelectValue placeholder="none" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="none">{t("none")}</SelectItem>
            {available.map((role) => (
              <SelectItem key={role.id} value={role.id}>
                {role.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <Button type="submit" className="h-10 bg-primary text-white hover:bg-primary/90">{t("Invite")}</Button>

      {error && (
        <p role="alert" className="w-full text-sm text-destructive">
          {error}
        </p>
      )}
    </form>
  );
}
