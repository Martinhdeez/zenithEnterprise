/**
 * Which model writes the answers, configured in the application rather than the environment.
 *
 * ADR 0003: an on-premise customer changes model without a redeploy, which is why this screen
 * exists at all and why the key it holds is stored encrypted rather than in a `.env` nobody
 * can reach from here.
 */

import { useEffect, useState } from "react";

import { llmConfig, saveLlmConfig, type LlmConfig } from "../api";
import { ApiError } from "@/shared/api/http";
import { FIELD } from "../fieldStyle";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";

export function LlmPanel({ token }: { token: string }) {
  const [config, setConfig] = useState<LlmConfig | null>(null);
  const [endpoint, setEndpoint] = useState("");
  const [model, setModel] = useState("");
  const [key, setKey] = useState("");
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    void llmConfig(token).then((loaded) => {
      setConfig(loaded);
      setEndpoint(loaded.endpoint_url);
      setModel(loaded.model_name);
    });
  }, [token]);

  if (!config) return null;

  return (
    <form
      onSubmit={async (event) => {
        event.preventDefault();
        setMessage(null);
        try {
          // `api_key` is omitted when the field was left blank, never sent as "". The
          // server reads omission as "keep the stored key" and "" as "clear it", and an
          // administrator editing a model name cannot re-enter a key they cannot read.
          const saved = await saveLlmConfig(token, {
            endpoint_url: endpoint,
            model_name: model,
            ...(key ? { api_key: key } : {}),
          });
          setConfig(saved);
          setKey("");
          setMessage("Saved.");
        } catch (caught) {
          setMessage(caught instanceof ApiError ? caught.message : "That could not be saved.");
        }
      }}
      className="space-y-4"
    >
      {!config.configured && (
        <p className="text-sm text-muted-foreground">
          Using this installation&rsquo;s default. Saving here overrides it for your organisation.
        </p>
      )}

      <div className="space-y-1.5">
        <Label htmlFor="llm-endpoint" className="text-sm text-foreground/80">
          Endpoint
        </Label>
        <Input
          id="llm-endpoint"
          value={endpoint}
          onChange={(event) => setEndpoint(event.target.value)}
          required
          className={`h-10 w-full ${FIELD}`}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="llm-model" className="text-sm text-foreground/80">
          Model
        </Label>
        <Input
          id="llm-model"
          value={model}
          onChange={(event) => setModel(event.target.value)}
          required
          className={`h-10 w-full ${FIELD}`}
        />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="llm-key" className="text-sm text-foreground/80">
          API key
        </Label>
        <Input
          id="llm-key"
          type="password"
          value={key}
          onChange={(event) => setKey(event.target.value)}
          placeholder={config.has_api_key ? "a key is stored — leave blank to keep it" : "none"}
          className={`h-10 w-full ${FIELD}`}
        />
      </div>

      <div className="flex items-center gap-3">
        <Button type="submit" className="h-10 bg-primary text-white hover:bg-primary/90">
          Save
        </Button>
        {message && <p className="text-sm text-muted-foreground">{message}</p>}
      </div>
    </form>
  );
}
