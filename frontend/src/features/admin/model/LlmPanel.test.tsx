import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/shared/api/http";
import type { LlmConfig } from "../api";

const llmConfig = vi.fn();
const saveLlmConfig = vi.fn();

vi.mock("../api", async () => {
  const actual = await vi.importActual<Record<string, unknown>>("../api");
  return { ...actual, llmConfig: (...args: unknown[]) => llmConfig(...args), saveLlmConfig };
});

const { LlmPanel } = await import("./LlmPanel");
const config: LlmConfig = {
  endpoint_url: "http://localhost:11434/v1",
  model_name: "local-model",
  has_api_key: false,
  configured: true,
};

beforeEach(() => {
  llmConfig.mockReset();
  saveLlmConfig.mockReset();
});

describe("loading the answer model configuration", () => {
  it("renders permission refusal without an unhandled rejection or editable connector", async () => {
    llmConfig.mockRejectedValue(new ApiError(403, "forbidden", "this action requires 'llm_config.manage'"));

    render(<LlmPanel token="member" />);

    expect((await screen.findByRole("alert")).textContent).toBe("this action requires 'llm_config.manage'");
    expect(screen.queryByLabelText("Endpoint")).toBeNull();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("can retry a failed read and then display the actual connector", async () => {
    llmConfig.mockRejectedValueOnce(new Error("offline")).mockResolvedValueOnce(config);
    render(<LlmPanel token="admin" />);

    expect((await screen.findByRole("alert")).textContent).toBe("The request failed.");
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect((await screen.findByLabelText("Endpoint") as HTMLInputElement).value).toBe(config.endpoint_url);
    expect(llmConfig).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("does not render an earlier token's connector after the identity changes", async () => {
    let resolveEarlier!: (value: LlmConfig) => void;
    llmConfig.mockReturnValueOnce(new Promise<LlmConfig>((resolve) => { resolveEarlier = resolve; }))
      .mockRejectedValueOnce(new ApiError(403, "forbidden", "not permitted"));
    const view = render(<LlmPanel token="earlier-admin" />);
    await waitFor(() => expect(llmConfig).toHaveBeenCalledWith("earlier-admin"));
    view.rerender(<LlmPanel token="later-member" />);
    await screen.findByRole("alert");
    await act(async () => { resolveEarlier(config); });

    expect(screen.queryByLabelText("Endpoint")).toBeNull();
    expect(screen.getByRole("alert").textContent).toBe("not permitted");
  });

  it("does not replace a later token's connector with an earlier read failure", async () => {
    let rejectEarlier!: (reason: unknown) => void;
    llmConfig.mockReturnValueOnce(new Promise<LlmConfig>((_resolve, reject) => { rejectEarlier = reject; }))
      .mockResolvedValueOnce(config);
    const view = render(<LlmPanel token="earlier" />);
    await waitFor(() => expect(llmConfig).toHaveBeenCalledWith("earlier"));
    view.rerender(<LlmPanel token="later" />);
    await screen.findByLabelText("Endpoint");
    await act(async () => { rejectEarlier(new ApiError(403, "forbidden", "earlier refusal")); });

    expect(screen.queryByRole("alert")).toBeNull();
    expect((screen.getByLabelText("Endpoint") as HTMLInputElement).value).toBe(config.endpoint_url);
  });
});
