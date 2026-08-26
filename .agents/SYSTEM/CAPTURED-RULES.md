# Captured Rules - Pending Review

Rules automatically captured from conversations. Review and promote to permanent docs.

---

## Pending Rules

### 2026-08-23 16:43 - Workflow: Do not start services autonomously

**User said:**

> "不可以自己启动"

**Rule extracted:**

- **Type**: NEVER
- **Action**: Do not start or restart project services unless the user explicitly requests that action.
- **Context**: Development servers, background processes, and other project runtime services.
- **Category**: workflow

**Example:**

```text
Good: Update the configuration, then tell the user that they need to restart the service.
Bad: Start or restart the service automatically after changing configuration.
```

**Status**: PENDING_REVIEW

---

## Processed Rules
