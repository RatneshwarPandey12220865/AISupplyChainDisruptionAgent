// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, apiUrl, authHeaders, AUTH_EVENT, parseSse, setApiKey, getApiKey } from "./api";

describe("parseSse", () => {
  it("parses complete events and keeps the partial tail", () => {
    const [events, rest] = parseSse('retry: 3000\n\nevent: change\ndata: {"at":"x"}\n\n: keepalive\n\nevent: ch');
    expect(events).toEqual([{ event: "change", data: '{"at":"x"}' }]);
    expect(rest).toBe("event: ch");
  });

  it("handles chunks split mid-event", () => {
    let [events, rest] = parseSse("event: change\nda");
    expect(events).toEqual([]);
    [events, rest] = parseSse(rest + "ta: 1\n\n");
    expect(events).toEqual([{ event: "change", data: "1" }]);
    expect(rest).toBe("");
  });
});

describe("api client", () => {
  beforeEach(() => { localStorage.clear(); delete window.__APP_CONFIG__; });
  afterEach(() => vi.restoreAllMocks());

  it("prefers the runtime config injected by the server layout", () => {
    window.__APP_CONFIG__ = { apiUrl: "https://api.example.com/" };
    expect(apiUrl()).toBe("https://api.example.com");
  });

  it("falls back to localhost when nothing is configured", () => {
    expect(apiUrl()).toBe("http://localhost:8000");
  });

  it("sends the stored API key", () => {
    expect(authHeaders()).toEqual({});
    setApiKey("secret-key");
    expect(getApiKey()).toBe("secret-key");
    expect(authHeaders({ A: "b" })).toEqual({ A: "b", "X-API-Key": "secret-key" });
  });

  it("raises an auth event and a typed error on 401", async () => {
    window.__APP_CONFIG__ = { apiUrl: "http://api" };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: "Missing or invalid API key" }), { status: 401 })));
    const seen = vi.fn();
    window.addEventListener(AUTH_EVENT, seen);
    await expect(api("/api/me")).rejects.toMatchObject({ status: 401, message: "Missing or invalid API key" });
    expect(seen).toHaveBeenCalled();
  });

  it("returns JSON and passes the key header", async () => {
    window.__APP_CONFIG__ = { apiUrl: "http://api" };
    setApiKey("k");
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: 1 }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    expect(await api("/api/dashboard")).toEqual({ ok: 1 });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("http://api/api/dashboard");
    expect((init as RequestInit).headers).toMatchObject({ "X-API-Key": "k" });
  });
});
