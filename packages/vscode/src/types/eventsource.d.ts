declare module "eventsource" {
  class EventSource {
    constructor(url: string | URL, initDict?: { headers?: Record<string, string> });
    close(): void;
    addEventListener(type: string, listener: (event: { data?: string }) => void): void;
    onerror?: (err: unknown) => void;
  }
  export default EventSource;
}
