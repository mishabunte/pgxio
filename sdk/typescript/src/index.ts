export type TokenProvider = () => string | Promise<string>;
export type Endpoint = { input: unknown; output: unknown };
export type Contract = Record<string, Endpoint>;
export type Stream = { input: unknown };

export class PgxioError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string) {
    super(`pgxio: ${code} (${status})`);
    this.status = status;
    this.code = code;
  }
}

/** Tokens belong to callers. This client never stores a database credential. */
export class Pgxio<C extends Contract = Contract, S extends Record<string, Stream> = Record<string, Stream>> {
  private url: string;
  private token: TokenProvider;
  constructor(url: string, token: TokenProvider) {
    this.url = url.replace(/\/$/, "");
    this.token = token;
  }
  async call<K extends keyof C & string>(endpoint: K, input: C[K]["input"], signal?: AbortSignal): Promise<C[K]["output"]> {
    const response = await fetch(`${this.url}/v1/${encodeURIComponent(endpoint)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${await this.token()}` },
      body: JSON.stringify(input),
      signal: signal ?? AbortSignal.timeout(10_000),
    });
    const body = await response.json();
    if (!response.ok) throw new PgxioError(response.status, body?.error?.code ?? "request_failed");
    return body;
  }

  /** A notification is an invalidation, never a row. Fetch only after subscribed.
   * Reconnect always fetches a new snapshot. Writes are never retried here. */
  watch<K extends keyof S & string, T>(stream: K, params: S[K]["input"], options: {
    snapshot: (signal: AbortSignal) => Promise<T>;
    onData: (value: T) => void;
    onError?: (error: unknown) => void;
  }): () => void {
    let stopped = false;
    let ws: WebSocket | undefined;
    let retry = 250;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let generation = 0;
    let controller: AbortController | undefined;
    const connect = () => {
      if (stopped) return;
      const current = ++generation;
      controller = new AbortController();
      const signal = controller.signal;
      let dirty = false;
      let fetching = false;
      let subscribed = false;
      const refresh = async () => {
        dirty = true;
        if (fetching || !subscribed) return;
        fetching = true;
        try {
          while (dirty && !signal.aborted) {
            dirty = false;
            const value = await options.snapshot(signal);
            if (!signal.aborted && current === generation) options.onData(value);
          }
        } catch (error) {
          if (!signal.aborted) { options.onError?.(error); ws?.close(); }
        } finally { fetching = false; }
      };
      const url = new URL(`${this.url}/v1/events`);
      url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
      const socket = ws = new WebSocket(url, "pgxio.v1");
      socket.onopen = async () => {
        try {
          const token = await this.token();
          if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ type: "auth", token }));
        } catch (error) { options.onError?.(error); socket.close(); }
      };
      socket.onmessage = event => {
        try {
          const message = JSON.parse(String(event.data));
          if (message.type === "ready") socket.send(JSON.stringify({ type: "subscribe", id: "1", stream, params }));
          else if (message.type === "subscribed" && message.id === "1") {
            retry = 250; subscribed = true; void refresh();
          } else if (message.type === "changed" && message.id === "1") void refresh();
        } catch (error) { options.onError?.(error); socket.close(); }
      };
      socket.onclose = () => {
        controller?.abort();
        if (!stopped) {
          timer = setTimeout(connect, retry + Math.random() * retry / 4);
          retry = Math.min(retry * 2, 15_000);
        }
      };
      socket.onerror = () => options.onError?.(new Error("pgxio WebSocket connection failed"));
    };
    connect();
    return () => { stopped = true; generation++; clearTimeout(timer); controller?.abort(); ws?.close(); };
  }
}
