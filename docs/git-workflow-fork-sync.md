# Working with a Forked Repository

How to keep your fork in sync with the original while working on your own changes.

---

## Setup: One-time only

### 1. Fork the original repo on GitHub

Go to the original repo page and click **Fork**. This creates `your-github-username/repo-name` under your account.

### 2. Clone your fork locally

```bash
git clone https://github.com/your-github-username/repo-name.git
cd repo-name
```

### 3. Add the original as an upstream remote

```bash
git remote add upstream https://github.com/original-owner/repo-name.git
```

You now have:
- `origin` → your fork (you push here)
- `upstream` → original repo (you pull from here)

---

## Day-to-Day Workflow

### Starting new work: sync first, then branch

**Step 1 — Switch to main and pull the latest from the original:**

```bash
git checkout main
git fetch upstream
git merge upstream/main
```

**Step 2 — Push the updated main to your fork:**

```bash
git push origin main
```

**Step 3 — Create a new branch for your work:**

```bash
git checkout -b my-feature-branch
```

Never work directly on `main`.

### Making changes

```bash
git add .
git commit -m "describe what I changed"
git push origin my-feature-branch
```

### Opening a Pull Request

On GitHub, go to **your fork** — GitHub will prompt you to create a PR targeting the original repo. Open it with a description of your changes.

---

## When Both the Original and Your Fork Have Changed

The original (`upstream`) and your fork (`origin`) have both received new commits. Here's how to reconcile:

### 1. Fetch the latest from the original

```bash
git fetch upstream
```

### 2. Merge upstream into your local main

```bash
git checkout main
git merge upstream/main
```

### 3. Resolve any conflicts

If the original changed the same lines you changed, Git will report conflicts. Open those files, find the conflict markers (`<<<<<<<`, `=======`, `>>>>>>>`), choose or combine the changes, then:

```bash
git add .
git commit -m "Resolve merge conflicts"
```

### 4. Push the merged result to your fork

```bash
git push origin main
```

### 5. Your feature branch stays intact

Your existing feature branches still contain your work. If they were based on an older `main`, you can either:
- Continue working as-is (your changes are preserved)
- Rebase your branch onto the new `main` if you need the original's latest code

Rebasing a feature branch onto the new main:

```bash
git checkout my-feature-branch
git rebase main
```

---

## Branching Strategy Summary

| Command | Where | Why |
|---|---|---|
| `git fetch upstream` | Local | Download original's latest commits |
| `git merge upstream/main` | Local main | Integrate original's changes |
| `git push origin main` | Your fork | Keep your fork up to date |
| `git checkout -b my-feature-branch` | Local | Start new work without touching main |
| `git push origin my-feature-branch` | Your fork | Ready for PR |

**Core rule: `main` is always a reflection of `upstream/main` + your merged-in changes. Feature branches are where your actual work lives. Never commit directly to `main`.**