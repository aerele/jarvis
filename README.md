# Jarvis

**The AI teammate inside ERPNext.**

Your ERP already knows what happened. Jarvis helps your team ask what it means,
prepare what should happen next, and repeat good work without losing human
judgement.

**Ask. Verify. Approve. Repeat.**

## Features

### Ask your business in plain language

- Ask about cash, sales, purchases, stock, customers, suppliers, projects, or
  any other records you are allowed to see.
- Trace an answer back to the records, dates, and evidence behind it.
- Investigate a question, compare options, prepare a draft, and continue the
  work in one conversation.
- Speak a request or attach an image, PDF, spreadsheet, or business document
  when typing would leave out useful context.
- Name, star, search, and return to conversations without mixing unrelated
  decisions.

### Turn incoming documents into reviewed work

- Drop supplier bills, statements, price lists, and other source files into
  **File Box**.
- Let Jarvis read the source, prepare the corresponding work, and surface any
  uncertainty instead of quietly guessing.
- Use the **Approval Board** to inspect drafts, answer questions, approve the
  next step, or reject it with the source conversation still in reach.
- Preview spreadsheet imports before any rows are added.
- Export useful records, reports, and prepared documents when the work needs to
  leave the screen.

### Turn good judgement into team practice

- Add **Business Notes** for the terms, exceptions, and operating facts that
  make your company different.
- Use the **Wiki** to see what Jarvis knows, where it came from, who it applies
  to, and whether it needs correction.
- Save a business rule as a **Skill** so Jarvis follows it consistently next
  time.
- Save several steps as a **Macro**, run them again, or put them on a daily,
  weekly, or monthly schedule.
- Create a **Trigger** for work that should begin when an important ERPNext
  record changes.
- Install an **Agent** to check a defined area in the background and bring back
  findings or prepared work.
- Review suggested learning before it becomes a personal or shared rule.

### See the business and find the work faster

- Turn a question into a saved **Dashboard** and share the view with the right
  people.
- Search chats, dashboards, records, lists, and reports from one command
  palette.
- Filter, sort, and choose columns for busy work queues.
- Receive notifications when work finishes or needs a decision.
- Continue conversations, approvals, File Box work, and key business views from
  the mobile app.

### Keep people in control

- Jarvis follows each person's existing Frappe and ERPNext access. Asking
  Jarvis does not grant access to a record the person could not otherwise open.
- Reading and explanation can happen directly. Important or hard-to-reverse
  actions always wait for a person.
- Administrators may allow ordinary, reversible changes within a conversation;
  proposed work and its evidence remain visible for review.
- Connect a supported chat subscription you already pay for or an approved API
  key, choose the models available to the team, and set backups when needed.
- Review personal and team usage, limits, plan details, renewals, and activity
  from the workspace.

## How to Install

Jarvis supports Frappe 15 and 16.

### Self-hosted bench

Run these commands from your Frappe bench:

```bash
bench get-app https://github.com/Aerele-RnD/jarvis.git --branch beta
bench --site your-site.example install-app jarvis
```

### Frappe Cloud

On a private bench group:

1. Open **Bench Group > Apps**, choose **Add App**, and add
   `https://github.com/Aerele-RnD/jarvis.git` from the `beta` branch.
2. Deploy the bench update to your site.
3. Open **Site > Apps**, choose **Install App**, and install **Jarvis**.

After installation, sign in as a System Manager, open `/jarvis/onboarding`, and
follow the setup shown on screen.
