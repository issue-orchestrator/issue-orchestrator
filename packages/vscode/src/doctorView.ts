import * as vscode from "vscode";

import type { DoctorReport, DoctorCheck } from "./types.js";

type DoctorViewOptions = {
  title?: string;
  errorMessage?: string;
};

let doctorPanel: vscode.WebviewPanel | null = null;

export function showDoctorPanel(
  report: DoctorReport,
  options: DoctorViewOptions = {}
): vscode.WebviewPanel {
  if (!doctorPanel) {
    doctorPanel = vscode.window.createWebviewPanel(
      "issueOrchestratorDoctor",
      options.title || "Issue Orchestrator Doctor",
      vscode.ViewColumn.Active,
      { enableScripts: true }
    );
    doctorPanel.onDidDispose(() => {
      doctorPanel = null;
    });
  } else {
    doctorPanel.title = options.title || "Issue Orchestrator Doctor";
    doctorPanel.reveal();
  }

  doctorPanel.webview.html = renderDoctorHtml(report, options.errorMessage);
  return doctorPanel;
}

export function updateDoctorPanel(
  report: DoctorReport,
  options: DoctorViewOptions = {}
): void {
  if (!doctorPanel) {
    showDoctorPanel(report, options);
    return;
  }
  doctorPanel.title = options.title || "Issue Orchestrator Doctor";
  doctorPanel.webview.html = renderDoctorHtml(report, options.errorMessage);
}

function renderDoctorHtml(report: DoctorReport, errorMessage?: string): string {
  const nonce = `${Date.now()}${Math.random()}`;
  const checks = Array.isArray(report.checks) ? report.checks : [];
  const overall = report.overall || "unknown";
  const overallClass = statusClass(overall);
  const summary = summarizeChecks(checks);

  return `<!DOCTYPE html>
  <html lang="en">
    <head>
      <meta charset="UTF-8" />
      <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${nonce}';" />
      <style>
        body { font-family: sans-serif; padding: 16px; }
        h1 { margin: 0 0 8px; }
        .summary { display: flex; gap: 12px; align-items: center; margin-bottom: 12px; }
        .status { padding: 4px 10px; border-radius: 12px; font-weight: 600; }
        .status.ok { background: #e6f4ea; color: #0f5132; }
        .status.warning { background: #fff4e5; color: #8a4b08; }
        .status.error { background: #fdecea; color: #842029; }
        .status.info { background: #e7f1ff; color: #0b5ed7; }
        .summary-item { font-size: 12px; color: #4b5563; }
        .card { border: 1px solid #e5e7eb; border-radius: 8px; padding: 10px; margin-bottom: 10px; }
        .card-header { display: flex; align-items: center; justify-content: space-between; }
        .card-title { font-weight: 600; }
        .detail { margin-top: 6px; color: #374151; white-space: pre-wrap; }
        .banner { background: #fef3c7; border: 1px solid #fcd34d; padding: 8px 10px; border-radius: 8px; margin-bottom: 12px; }
        button { margin-top: 12px; padding: 6px 12px; border-radius: 6px; border: 1px solid #d1d5db; background: #fff; cursor: pointer; }
      </style>
    </head>
    <body>
      <h1>Issue Orchestrator Doctor</h1>
      ${errorMessage ? `<div class="banner">${escapeHtml(errorMessage)}</div>` : ""}
      <div class="summary">
        <span class="status ${overallClass}">${overall.toUpperCase()}</span>
        <span class="summary-item">${summary.ok} ok</span>
        <span class="summary-item">${summary.warning} warnings</span>
        <span class="summary-item">${summary.error} errors</span>
      </div>
      <div>
        ${checks.map(renderCheck).join("")}
      </div>
      <button id="rerun">Re-run diagnostics</button>
      <script nonce="${nonce}">
        const vscode = acquireVsCodeApi();
        document.getElementById("rerun").addEventListener("click", () => {
          vscode.postMessage({ type: "rerun" });
        });
      </script>
    </body>
  </html>`;
}

function renderCheck(check: DoctorCheck): string {
  const status = statusClass(check.status || "info");
  return `<div class="card">
      <div class="card-header">
        <div class="card-title">${escapeHtml(check.name || "Check")}</div>
        <div class="status ${status}">${escapeHtml((check.status || "info").toUpperCase())}</div>
      </div>
      <div class="detail">${escapeHtml(check.detail || "")}</div>
    </div>`;
}

function statusClass(status: string): string {
  if (status === "error") {
    return "error";
  }
  if (status === "warning") {
    return "warning";
  }
  if (status === "ok") {
    return "ok";
  }
  return "info";
}

function summarizeChecks(checks: DoctorCheck[]): { ok: number; warning: number; error: number } {
  return checks.reduce(
    (acc, check) => {
      if (check.status === "error") {
        acc.error += 1;
      } else if (check.status === "warning") {
        acc.warning += 1;
      } else if (check.status === "ok") {
        acc.ok += 1;
      }
      return acc;
    },
    { ok: 0, warning: 0, error: 0 }
  );
}

function escapeHtml(input: string): string {
  return input
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}
