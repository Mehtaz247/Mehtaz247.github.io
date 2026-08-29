# Requests requiring a human

Only things that genuinely cannot be done autonomously belong here — actions
that legally or technically require a person: creating accounts, entering
payment details, granting credentials. Everything else is the operator's job.

Keep this list short and specific. Delete items once done.

---

## Open

### 1. Google Search Console property (5 minutes, free)

**Why:** Search is the highest-value long-term channel in `strategy.json`, and
right now there is no way to see whether any of it is working — which queries
surface a post, which get clicked, whether pages are indexed at all. Without it,
decisions about what to write next are made blind.

**Exact steps:**

1. Go to <https://search.google.com/search-console>
2. Add a **URL prefix** property for `https://mehtaz247.github.io/`
3. Choose the **HTML tag** verification method and copy the `content` value
4. Paste it into `site/site.json` as `"google_site_verification": "<value>"`,
   or just drop it in an issue on the repo and the next cycle will wire it up

The sitemap is already generated and linked from `robots.txt`, so submission is
automatic once the property exists.

**Blocked because:** Account creation and domain verification require a signed-in
human. Nothing else about it does.

---

### 2. Nothing else

No other human action is needed. Hosting (GitHub Pages), scheduling (launchd),
and compute (the existing Claude Code subscription) are all in place and free.

**No spending has been incurred and none is planned.** If something ever appears
to require payment, the operator is instructed to stop and add a request here
rather than proceed.

---

## Optional, not requested

A custom domain (~$12/year) would improve the branding over
`mehtaz247.github.io` and make the site portable if hosting ever changes.
**This is not being asked for** — it is recorded only so the option is visible.
The site works fine without it, and the operator will not buy one.
