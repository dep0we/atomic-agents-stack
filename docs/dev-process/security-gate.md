> **Not this repo's vulnerability-disclosure policy.** For "how to report a security bug in atomic-agents-stack," see [`/SECURITY.md`](../../SECURITY.md) at the repo root — that file is unrelated and untouched by this doc or by the dev-process-kit install. This file is the dev-process-kit's own **build-time security-review policy**: what the `arc-preflight.sh seccheck` gate enforces before a build/finish reaches pr-ready, and how the `/cso` + security-scanner + cross-family review layers work. Copied verbatim from `dev-process-kit/security.md` (v0.2.0) as reference documentation — it is not installed automatically by `install.sh`.

# Security — when a review is required, and how it runs

This is the full policy behind [`PLAYBOOK.md`](PLAYBOOK.md) Stage 8 (the Security gate). It answers two questions: **when** must a security review happen, and **how** does it run.

You are not a developer. Your job here is one thing: for each real finding, decide whether to **accept the risk** (knowingly) or **avoid it** (change the design). The AI does the reviewing and lays out consequences in plain language. The call is yours.

---

## When a security review is MANDATORY

Run the Stage 8 gate whenever the change touches **any** of these. Scan the list — if even one applies, the gate runs.

- **Authentication** — anything that proves *who* a user is (login, sessions, password reset, "stay signed in").
- **Authorization** — anything that decides *what* a user is allowed to do (roles, permissions, "can this person see/edit this?").
- **Secrets / credentials** — API keys, tokens, passwords — created, read, stored, logged, or moved.
- **Stored data** — especially personal or sensitive data. Includes database schemas, migrations (a change to the shape of stored data), and exports.
- **Money, billing, or quotas / rate limits** — anything that charges, counts spend, or caps usage.
- **Untrusted input** — anything a user, an uploaded file, or an external system sends in. Untrusted means "we didn't write it, so we can't assume it's well-behaved."
- **File paths / filesystem access** — reading or writing files based on a name or path that came from outside. (The classic bug here is *path traversal* — a sneaky path like `../../etc/passwd` that escapes the folder you meant to stay inside.)
- **A new external dependency or third-party integration** — a library, package, or outside service the project didn't use before.

**The rule: if in doubt, run it.** A security review is cheap; a breach is not.

**If the change touches none of the above**, the AI may *propose* skipping Stage 8 — but it must say so explicitly and ask first, never skip silently. That follows PLAYBOOK's governing rule. A skip looks like:

> *"This change only edits documentation — it touches no auth, secrets, data, money, or input. OK to skip Stage 8?"*

---

## How the gate runs — three layers

A security review is not one pass. It's three layers, because each catches a different class of problem.

### 1. `/cso` — Chief Security Officer mode

A structured, reasoning-led security review of *this specific change*. It thinks like an attacker about what the change exposes: what could go wrong, what assumptions it makes about its inputs, where trust boundaries sit. This is the judgment layer.

### 2. `security-review` skill / security-scanner pass

An automated scan of the change for:
- **Exposed secrets** — keys or passwords accidentally left in the code.
- **Dependency issues** — known-vulnerable versions of third-party libraries.
- **Common vulnerability classes** — the OWASP-style bugs. (*OWASP* is the standard catalog of common web vulnerabilities — things like *injection*, where untrusted input gets treated as a command, and *broken authentication*, where login can be bypassed.)

This is the mechanical layer — it catches the well-known, pattern-matchable mistakes fast.

### 3. Cross-family review

A **different-vendor** model (e.g. Codex, a non-Anthropic model) reads the change *specifically* for security. This is **load-bearing**, not optional polish — especially for path, filesystem, and injection bugs.

Why: a different model family has different blind spots. This kit's own track record proves it — on a security-sensitive change, **five same-family review rounds all missed a symlink-based path-escape** (a trick that uses a shortcut-file to break out of an allowed folder) that a **cross-family reviewer caught on the very first pass.** Same-family review, no matter how many rounds, shares the same blind spots. A second vendor doesn't.

---

## What you decide

For each **real** finding (the AI filters out noise and false alarms first), you get a plain-language choice:

- **Accept the risk** — you understand what could go wrong and judge it acceptable for now (with the reason written down, eyes open).
- **Avoid it** — change the design so the risk goes away.

The AI's job is to state, for each finding: *what the actual problem is, what happens if we do nothing, what fixing it costs.* It does **not** decide for you, and it does **not** bury a security trade-off in technical shorthand. If you can't tell from the write-up what's at stake, that's a bug in the write-up — ask for it again, plainer.

---

## Verify before claim

Security claims are never accepted on plausibility — they're verified.

- When a reviewer (human or AI) **asserts a vulnerability**, reproduce it before accepting the finding. A finding that can't be reproduced isn't a finding yet; it's a rumor. (Plenty of "vulnerabilities" turn out to be impossible in practice — don't redesign around a ghost.)
- When the AI **claims something is safe**, it must have *actually checked* — run the code, read the path, confirmed the boundary — not assumed. "This should be fine" is not a security clearance.

This costs a little time per finding and eliminates rumor-driven changes. It runs in **both directions**: prove the danger before you fix it, prove the safety before you ship it.

---

## The periodic audit (separate from the per-change gate)

The Stage 8 gate reviews *one change*. Some risks only show up when you look at the *whole system* — so a full `/cso` audit runs periodically, regardless of any single change:

- **Before a public launch** — before anyone outside can reach it.
- **After a big batch of changes** — when a lot has shifted since the last full look.
- **On a regular cadence** — e.g. quarterly, so nothing rots in the dark.

### Never commit secrets

This is absolute, and it spans both the per-change gate and the audit:

- **Use environment variables** for every secret — never hardcode a key, token, or password into the code.
- **Gitignore `.env`** — the file that holds your actual secrets stays out of version control. Use a `.env.example` (with fake placeholder values) to show what's needed.
- **If a secret ever lands in git history** — assume it's **compromised**. Two steps, both required: **rotate it** (generate a new one; treat the old as burned) *and* **scrub the history** (remove it from past commits). Deleting it from the latest version is not enough — git remembers everything unless you scrub it.

### AGENTS.md is sent to external AI providers — treat it as a public document

`AGENTS.md` is the cross-tool agent instructions file that Codex (and other external AI tools) read from your repo. Like `CLAUDE.md`, its content is sent to an AI provider whenever an agent runs — so neither file should ever hold secrets. What makes `AGENTS.md` its own security surface is that it's read by **third-party** tools (Codex sends it to OpenAI; other tools to their own providers) and, by the cross-tool convention, is a shared document many vendors and contributors may see. **Treat it as public:** assume anything in it leaves your machine and could be read by more than one provider.

The rule: **treat `AGENTS.md` as a public document.** Never put in it:

- API keys, tokens, or passwords (use placeholder references like "the key is in `.env` as `API_KEY`")
- Internal hostnames, IP addresses, or URLs you wouldn't share publicly
- Absolute filesystem paths (they expose your directory structure)
- Any credentials or information you'd rotate if leaked

The "never commit secrets" rule covers git history. This rule covers *what you write into the file at all*, regardless of whether it's committed. If you would not post it in a public GitHub repo, it does not belong in `AGENTS.md`.

---

## Red flags — stop and review NOW

If you catch yourself (or the AI) about to do any of these, the security gate runs *before* you go further — not after:

- "We're about to handle **login** / passwords / sessions."
- "We're **storing user data**" — especially anything personal.
- "We're taking **file uploads**" (or letting users supply file paths / names).
- "We added a **dependency I haven't heard of**."
- "This touches **money, billing, or spending caps**."
- "We're letting **an external system send us data**."
- "We're about to **expose a new public endpoint**" (a new door into the system from the outside).

Any one of these is the signal: pause, run Stage 8, then continue.
