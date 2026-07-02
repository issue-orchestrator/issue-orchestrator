import * as vscode from "vscode";

import type {
  Snapshot,
  IssueSummary,
  ActiveSession,
  PendingReview,
  HistoryEntry,
} from "./types.js";

import type { OrchestratorClient } from "./orchestratorClient.js";
import { isSnapshot } from "./validators.js";

const SECTION = {
  status: "Status",
  sessions: "Active Sessions",
  queue: "Queue",
  blocked: "Blocked",
  history: "History",
  reviews: "Reviews",
  diagnostics: "Diagnostics",
} as const;

class SectionItem extends vscode.TreeItem {
  constructor(public readonly key: string, label: string) {
    super(label, vscode.TreeItemCollapsibleState.Collapsed);
  }
}

class IssueItem extends vscode.TreeItem {
  constructor(
    public readonly issueNumber: number,
    label: string,
    description?: string,
    tooltip?: string,
    contextValue = "issue"
  ) {
    super(label, vscode.TreeItemCollapsibleState.None);
    this.description = description;
    this.tooltip = tooltip;
    this.contextValue = contextValue;
  }
}

class SessionItem extends vscode.TreeItem {
  constructor(
    public readonly issueNumber: number,
    label: string,
    description?: string,
    tooltip?: string
  ) {
    super(label, vscode.TreeItemCollapsibleState.None);
    this.description = description;
    this.tooltip = tooltip;
    this.contextValue = "session";
  }
}

class ReviewItem extends vscode.TreeItem {
  constructor(
    public readonly issueNumber: number,
    label: string,
    description?: string,
    tooltip?: string
  ) {
    super(label, vscode.TreeItemCollapsibleState.None);
    this.description = description;
    this.tooltip = tooltip;
    this.contextValue = "issue";
  }
}

class InfoItem extends vscode.TreeItem {
  constructor(label: string, description?: string) {
    super(label, vscode.TreeItemCollapsibleState.None);
    this.description = description;
  }
}

export class OrchestratorTreeDataProvider implements vscode.TreeDataProvider<vscode.TreeItem> {
  private snapshot: Snapshot | null = null;
  private refreshTimer: NodeJS.Timeout | null = null;
  private readonly _onDidChangeTreeData = new vscode.EventEmitter<vscode.TreeItem | undefined | null>();
  readonly onDidChangeTreeData = this._onDidChangeTreeData.event;

  constructor(
    private readonly client: OrchestratorClient,
    private readonly output: vscode.OutputChannel,
    private readonly statusBar: vscode.StatusBarItem,
    private readonly onSnapshot?: (snapshot: Snapshot) => void
  ) {}

  async refresh(): Promise<void> {
    try {
      const snapshot = await this.client.getSnapshot();
      if (!isSnapshot(snapshot)) {
        throw new Error("Invalid snapshot payload");
      }
      this.snapshot = snapshot;
      this.updateStatusBar();
      if (this.snapshot && this.onSnapshot) {
        this.onSnapshot(this.snapshot);
      }
      this._onDidChangeTreeData.fire(undefined);
    } catch (err) {
      this.output.appendLine(`Snapshot refresh failed: ${String(err)}`);
      this._onDidChangeTreeData.fire(undefined);
    }
  }

  getSnapshot(): Snapshot | null {
    return this.snapshot;
  }

  getRepo(): string | null {
    return this.snapshot?.info.repo ?? null;
  }

  startPolling(): void {
    const config = vscode.workspace.getConfiguration("issueOrchestrator");
    const interval = Math.max(3, config.get<number>("pollIntervalSeconds", 10));
    if (this.refreshTimer) {
      clearInterval(this.refreshTimer);
    }
    this.refreshTimer = setInterval(() => {
      void this.refresh();
    }, interval * 1000);
  }

  stopPolling(): void {
    if (this.refreshTimer) {
      clearInterval(this.refreshTimer);
      this.refreshTimer = null;
    }
  }

  getTreeItem(element: vscode.TreeItem): vscode.TreeItem {
    return element;
  }

  getChildren(element?: vscode.TreeItem): vscode.ProviderResult<vscode.TreeItem[]> {
    if (!this.snapshot) {
      return [new InfoItem("Loading orchestrator snapshot...")];
    }

    if (!element) {
      return [
        new SectionItem("status", SECTION.status),
        new SectionItem("sessions", `${SECTION.sessions} (${this.snapshot.status.active_sessions.length})`),
        new SectionItem("queue", `${SECTION.queue} (${this.snapshot.status.queue.length})`),
        new SectionItem("blocked", `${SECTION.blocked} (${this.snapshot.blocked.count})`),
        new SectionItem("history", `${SECTION.history} (${this.snapshot.history.count})`),
        new SectionItem("reviews", `${SECTION.reviews} (${this.snapshot.status.pending_reviews.length})`),
        new SectionItem("diagnostics", SECTION.diagnostics),
      ];
    }

    if (element instanceof SectionItem) {
      switch (element.key) {
        case "status":
          return this.buildStatusItems();
        case "sessions":
          return this.buildSessionItems(this.snapshot.status.active_sessions);
        case "queue":
          return this.buildIssueItems(this.snapshot.status.queue, "queue");
        case "blocked":
          return this.buildIssueItems(this.snapshot.blocked.blocked, "blocked");
        case "history":
          return this.buildHistoryItems(this.snapshot.history.history);
        case "reviews":
          return this.buildReviewItems(this.snapshot.status.pending_reviews);
        case "diagnostics":
          return this.buildDiagnosticsItems();
        default:
          return [];
      }
    }

    return [];
  }

  private buildStatusItems(): vscode.TreeItem[] {
    const items: vscode.TreeItem[] = [];
    items.push(new InfoItem("Paused", this.snapshot?.status.paused ? "Yes" : "No"));
    items.push(new InfoItem("Shutdown Requested", this.snapshot?.status.shutdown_requested ? "Yes" : "No"));
    items.push(new InfoItem("Active Sessions", String(this.snapshot?.status.active_sessions.length ?? 0)));
    items.push(new InfoItem("Queue Size", String(this.snapshot?.status.queue.length ?? 0)));
    items.push(new InfoItem("Completed Today", String(this.snapshot?.status.completed_today.length ?? 0)));
    if (this.snapshot?.info.repo) {
      items.push(new InfoItem("Repo", this.snapshot.info.repo));
    }
    if (this.snapshot?.info.commit_short) {
      items.push(new InfoItem("Commit", this.snapshot.info.commit_short));
    }
    return items;
  }

  private buildSessionItems(sessions: ActiveSession[]): vscode.TreeItem[] {
    return sessions.map((session) => {
      const label = `#${session.issue_number} ${session.title}`;
      const description = `${session.agent_type.replace("agent:", "")}, ${session.runtime_minutes}m`;
      return new SessionItem(session.issue_number, label, description, `Status: ${session.status}`);
    });
  }

  private buildIssueItems(issues: IssueSummary[], context: string): vscode.TreeItem[] {
    return issues.map((issue) => {
      const label = `#${issue.number} ${issue.title}`;
      const description = issue.agent_type ? issue.agent_type.replace("agent:", "") : context;
      const tooltip = issue.blocked_summary || issue.flow_stage || undefined;
      return new IssueItem(issue.number, label, description, tooltip, "issue");
    });
  }

  private buildHistoryItems(entries: HistoryEntry[]): vscode.TreeItem[] {
    return entries.map((entry) => {
      const label = `#${entry.issue_number} ${entry.title}`;
      const description = `${entry.status} (${entry.runtime_minutes}m)`;
      return new IssueItem(entry.issue_number, label, description, entry.status_reason ?? undefined, "issue");
    });
  }

  private buildReviewItems(reviews: PendingReview[]): vscode.TreeItem[] {
    return reviews.map((review) => {
      const label = `#${review.issue_number} PR #${review.pr_number}`;
      const description = review.branch_name;
      return new ReviewItem(review.issue_number, label, description, review.pr_url);
    });
  }

  private buildDiagnosticsItems(): vscode.TreeItem[] {
    const items: vscode.TreeItem[] = [];
    const dependencyCount = Object.keys(this.snapshot?.dependency_problems.problems ?? {}).length;
    items.push(new InfoItem("Dependency Problems", String(dependencyCount)));
    items.push(new InfoItem("Stale Issues", String(this.snapshot?.stale.count ?? 0)));
    items.push(new InfoItem("Excluded Issues", String(this.snapshot?.excluded.count ?? 0)));
    return items;
  }

  private updateStatusBar(): void {
    if (!this.snapshot) {
      this.statusBar.text = "Issue Orchestrator: idle";
      return;
    }
    const active = this.snapshot.status.active_sessions.length;
    const queued = this.snapshot.status.queue.length;
    const blocked = this.snapshot.blocked.count;
    const paused = this.snapshot.status.paused ? "paused" : "running";
    this.statusBar.text = `IO: ${paused} • ${active} active • ${queued} queued • ${blocked} blocked`;
  }
}
