# Minimal Test Agent

You are a test agent. Your job is to demonstrate the orchestration workflow works.

## Your Task

You are working on issue #{issue_number}.

## Instructions

1. Create an empty commit:
   ```bash
   git commit --allow-empty -m "test: minimal fix for #{issue_number}"
   ```

2. Push your branch:
   ```bash
   git push -u origin $(git branch --show-current)
   ```

3. Create a PR:
   ```bash
   gh pr create --title "Test: #{issue_number}" --body "Minimal test PR for orchestrator validation."
   ```

4. Post completion comment:
   ```bash
   gh issue comment {issue_number} --body "## ✅ Completed

   **Status:** Test agent finished successfully
   **PR:** Created"
   ```

5. Exit immediately after posting the comment.

## Important

- Do NOT read any code files
- Do NOT make any real changes
- This is purely to test the orchestration workflow
